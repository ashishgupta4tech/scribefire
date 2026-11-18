"""Microphone capture.

Design notes (these were the bugs that mattered):

*   The input stream is opened **once** at startup and stays open for the
    whole session.  Opening it on key-press loses the first word on
    Bluetooth headsets, which need 300-800 ms to switch from music mode to
    call mode.
*   A continuous **rolling ring buffer** keeps the last 1.5 s of audio.  When
    the hotkey is pressed the recording starts with the previous ~500 ms
    already in the buffer, so speech that began before the keypress survives.
*   The audio callback only does cheap work: downmix, resample, append.  It
    never blocks and never spawns a thread per buffer (that reorders audio).
*   Everything is resampled to 16 kHz mono float32 - what Whisper and Vosk
    expect - using a streaming FIR + linear interpolator so the result is
    identical to a single-shot resample.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import Deque, Dict, List, Optional

import numpy as np

from .logs import get_logger, guarded

log = get_logger("audio")

TARGET_RATE = 16000
DEFAULT_RING_MS = 1500
DEFAULT_PREROLL_MS = 500
MIN_DURATION_MS = 300
RMS_WINDOW_MS = 20
RMS_BUFFER_SIZE = 80


# --------------------------------------------------------------------------
# Device helpers
# --------------------------------------------------------------------------

def list_input_devices() -> List[Dict[str, object]]:
    """Return the available input devices as plain dictionaries."""
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover - depends on PortAudio
        log.error("sounddevice is not available: %s", exc)
        return []

    devices: List[Dict[str, object]] = []
    try:
        default = sd.default.device
    except Exception:
        default = None

    try:
        raw = sd.query_devices()
    except Exception as exc:
        log.error("could not query audio devices: %s", exc)
        return []

    for index, info in enumerate(raw):
        if int(info.get("max_input_channels", 0)) <= 0:
            continue
        devices.append(
            {
                "id": index,
                "name": str(info.get("name", f"Device {index}")),
                "channels": int(info.get("max_input_channels", 0)),
                "default_rate": float(info.get("default_samplerate", 48000)),
                "is_default": default is not None and index == default[0],
            }
        )
    return devices


def resolve_device(device) -> Optional[int]:
    """Resolve a device id, device name or ``None`` to a sounddevice index."""
    if device is None or device == "":
        return None
    try:
        return int(device)
    except (TypeError, ValueError):
        pass
    for info in list_input_devices():
        if str(info["name"]) == str(device):
            return int(info["id"])  # type: ignore[arg-type]
    return None


# --------------------------------------------------------------------------
# Streaming resampler
# --------------------------------------------------------------------------

def _fir_lowpass(numtaps: int, cutoff_hz: float, fs: float) -> np.ndarray:
    """Windowed-sinc low-pass FIR taps (no SciPy dependency)."""
    numtaps = max(8, int(numtaps) | 1)  # odd length
    n = np.arange(numtaps, dtype=np.float64)
    centre = (numtaps - 1) / 2.0
    normalised_cutoff = cutoff_hz / fs
    h = np.sinc(2.0 * normalised_cutoff * (n - centre)) * (2.0 * normalised_cutoff)
    h *= np.hamming(numtaps)
    total = h.sum()
    if total != 0:
        h /= total
    return h.astype(np.float32)


class StreamingResampler:
    """Stateful resampler: native rate float32 mono -> 16 kHz float32 mono.

    A windowed-sinc low-pass runs first (anti-aliasing), then linear
    interpolation places the output samples on an exact time grid.  Filter
    state is carried across chunks so the output is identical to resampling
    the whole signal at once.
    """

    def __init__(self, in_rate: int, out_rate: int = TARGET_RATE, numtaps: int = 48):
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self.ratio = self.in_rate / float(self.out_rate)
        # Keep the filter roughly the same duration in time across rates.
        taps = max(9, int(round(numtaps * max(1.0, self.ratio))))
        self._h = _fir_lowpass(taps, 0.45 * self.out_rate, self.in_rate)
        self._numtaps = len(self._h)
        self._tail = np.zeros(0, dtype=np.float32)
        self._ybuf = np.zeros(0, dtype=np.float32)
        self._y_base = 0            # global index of _ybuf[0]
        self._input_done = 0        # total input samples consumed
        self._next_pos = 0.0        # global filtered index of the next output
        self._out_count = 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return np.zeros(0, dtype=np.float32)

        buf = np.concatenate([self._tail, x])
        filtered = np.convolve(buf, self._h, mode="valid")
        base = self._input_done - len(self._tail)
        self._input_done += x.size
        self._tail = buf[-(self._numtaps - 1):] if self._numtaps > 1 else np.zeros(0, np.float32)

        if filtered.size:
            self._ybuf = np.concatenate([self._ybuf, filtered])
        last_available = self._y_base + self._ybuf.size - 1

        out_chunks: List[np.ndarray] = []
        produced = 0
        while self._next_pos + 1.0 <= last_available:
            offset = self._next_pos - self._y_base
            i0 = int(math.floor(offset))
            frac = offset - i0
            if i0 < 0:
                # Not enough history yet (start of stream).
                self._next_pos += self.ratio
                produced += 1
                continue
            if i0 + 1 >= self._ybuf.size:
                break
            value = self._ybuf[i0] * (1.0 - frac) + self._ybuf[i0 + 1] * frac
            out_chunks.append(float(value))
            self._next_pos += self.ratio
            produced += 1

        if produced:
            self._out_count += produced
            # Trim the buffer we no longer need.
            keep_from = max(0, int(self._next_pos) - self._y_base - 2)
            if keep_from > 0:
                self._ybuf = self._ybuf[keep_from:]
                self._y_base += keep_from

        if not out_chunks:
            return np.zeros(0, dtype=np.float32)
        return np.array(out_chunks, dtype=np.float32)

    def flush(self) -> np.ndarray:
        """Pad the tail and return whatever samples are still pending."""
        if self._tail.size == 0:
            return np.zeros(0, dtype=np.float32)
        pad = np.zeros(self._numtaps - 1, dtype=np.float32)
        saved = self._tail
        try:
            self._tail = np.concatenate([saved, pad])
            return self.process(np.zeros(0, dtype=np.float32))
        finally:
            self._tail = saved


# --------------------------------------------------------------------------
# Recorder
# --------------------------------------------------------------------------

class AudioRecorder:
    """Owns the microphone stream and the pre-roll ring buffer."""

    def __init__(
        self,
        device=None,
        target_rate: int = TARGET_RATE,
        preroll_ms: int = DEFAULT_PREROLL_MS,
        ring_ms: int = DEFAULT_RING_MS,
        min_duration_ms: int = MIN_DURATION_MS,
        rms_buffer_size: int = RMS_BUFFER_SIZE,
        on_rms=None,
        on_pcm=None,
    ):
        self.target_rate = target_rate
        self.preroll_ms = max(0, int(preroll_ms))
        self.ring_ms = max(self.preroll_ms, int(ring_ms))
        self.min_duration_ms = int(min_duration_ms)
        self.on_rms = on_rms
        self.on_pcm = on_pcm

        self._device_spec = device
        self._device_index = resolve_device(device)

        self._stream = None
        self._resampler: Optional[StreamingResampler] = None
        self.native_rate = 48000

        self._lock = threading.RLock()
        self._ring: Deque[np.ndarray] = deque()
        self._ring_len = 0
        self._recording = False
        self._rec_chunks: List[np.ndarray] = []

        self._rms_pending = np.zeros(0, dtype=np.float32)
        self._rms_window = max(1, int(self.target_rate * RMS_WINDOW_MS / 1000))
        self.rms_buffer: Deque[float] = deque(maxlen=rms_buffer_size)

    # -- properties ----------------------------------------------------

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def rms_samples(self) -> List[float]:
        return list(self.rms_buffer)

    def device_name(self) -> str:
        index = self._device_index
        if index is None:
            try:
                import sounddevice as sd

                default = sd.default.device
                index = default[0] if default else None
            except Exception:
                index = None
        if index is None:
            return "system default"
        for info in list_input_devices():
            if info["id"] == index:
                return str(info["name"])
        return "system default"

    # -- stream lifecycle ----------------------------------------------

    def start(self) -> bool:
        """Open the input stream.  It stays open for the whole session."""
        try:
            import sounddevice as sd
        except Exception as exc:
            log.error("sounddevice is not available: %s", exc)
            return False

        index = self._device_index
        if index is None:
            try:
                default = sd.default.device
                index = default[0] if default else None
            except Exception:
                index = None

        try:
            info = sd.query_devices(index, "input")
            self.native_rate = int(round(float(info.get("default_samplerate", 48000))))
        except Exception:
            self.native_rate = 48000

        self._resampler = StreamingResampler(self.native_rate, self.target_rate)

        channels = 1
        try:
            self._stream = sd.InputStream(
                device=index,
                channels=channels,
                samplerate=self.native_rate,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            log.info(
                "microphone stream open (device=%s, rate=%d, pre-roll=%dms)",
                self.device_name(),
                self.native_rate,
                self.preroll_ms,
            )
            return True
        except Exception as exc:
            log.error("could not open microphone (%s), retrying with all channels", exc)
            try:
                max_channels = int(sd.query_devices(index, "input").get("max_input_channels", 1))
            except Exception:
                max_channels = 1
            try:
                self._stream = sd.InputStream(
                    device=index,
                    channels=max(1, max_channels),
                    samplerate=self.native_rate,
                    dtype="float32",
                    callback=self._callback,
                )
                self._stream.start()
                log.info("microphone stream open with %d channels", max(1, max_channels))
                return True
            except Exception as exc2:
                log.error("could not open microphone: %s", exc2)
                self._stream = None
                return False

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def set_device(self, device) -> None:
        """Switch input device; the stream is reopened."""
        with self._lock:
            self._device_spec = device
            self._device_index = resolve_device(device)
        was_running = self._stream is not None
        if was_running:
            self.stop()
        if was_running:
            self.start()

    # -- recording -----------------------------------------------------

    def begin_recording(self) -> None:
        """Start a recording, seeded with the pre-roll already in the ring."""
        with self._lock:
            self._rec_chunks = []
            preroll = self._take_tail(self._pre_roll_samples())
            if preroll.size:
                self._rec_chunks.append(preroll)
            self._recording = True

    def end_recording(self) -> Optional[np.ndarray]:
        """Stop recording and return the 16 kHz mono float32 samples.

        Returns ``None`` when the recording was shorter than
        ``min_duration_ms`` (an accidental key tap).
        """
        with self._lock:
            self._recording = False
            chunks = self._rec_chunks
            self._rec_chunks = []

        if not chunks:
            return None
        audio = np.concatenate(chunks).astype(np.float32)
        duration_ms = (audio.size / float(self.target_rate)) * 1000.0
        if duration_ms < self.min_duration_ms:
            log.info("recording discarded (%.0fms < %dms)", duration_ms, self.min_duration_ms)
            return None
        return audio

    def duration_ms(self, audio: np.ndarray) -> float:
        return (audio.size / float(self.target_rate)) * 1000.0

    # -- ring buffer ---------------------------------------------------

    def _ring_capacity(self) -> int:
        return int(self.native_rate * self.ring_ms / 1000)

    def _pre_roll_samples(self) -> int:
        return int(self.native_rate * self.preroll_ms / 1000)

    def _push_ring(self, chunk: np.ndarray) -> None:
        if chunk.size == 0:
            return
        self._ring.append(chunk)
        self._ring_len += chunk.size
        capacity = self._ring_capacity()
        while self._ring_len > capacity and len(self._ring) > 1:
            oldest = self._ring.popleft()
            self._ring_len -= oldest.size

    def _take_tail(self, count: int) -> np.ndarray:
        """Return up to *count* samples from the end of the ring buffer."""
        if count <= 0 or not self._ring:
            return np.zeros(0, dtype=np.float32)
        pieces: List[np.ndarray] = []
        remaining = count
        for chunk in reversed(self._ring):
            if chunk.size >= remaining:
                pieces.append(chunk[-remaining:])
                remaining = 0
                break
            pieces.append(chunk)
            remaining -= chunk.size
            if remaining <= 0:
                break
        if remaining > 0:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(reversed(pieces)).astype(np.float32)

    # -- callback ------------------------------------------------------

    @guarded("audio")
    def _callback(self, indata, frames, time_info, status):  # noqa: ANN001
        if status:
            log.info("audio status: %s", status)
        data = np.asarray(indata, dtype=np.float32)
        if data.ndim > 1:
            mono = data.mean(axis=1)
        else:
            mono = data

        resampler = self._resampler
        pcm = resampler.process(mono) if resampler is not None else mono

        with self._lock:
            self._push_ring(pcm)
            if self._recording:
                self._rec_chunks.append(pcm)

        self._push_rms(pcm)

        # Forward the same 16 kHz stream to the preview engine.  The preview
        # never opens a second input stream - it reuses this audio.
        if self.on_pcm is not None:
            try:
                self.on_pcm(pcm)
            except Exception:
                pass

    def _push_rms(self, pcm: np.ndarray) -> None:
        if pcm.size == 0:
            return
        pending = np.concatenate([self._rms_pending, pcm])
        offset = 0
        window = self._rms_window
        while pending.size - offset >= window:
            block = pending[offset:offset + window]
            rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
            self.rms_buffer.append(rms)
            if self.on_rms is not None:
                try:
                    self.on_rms(rms)
                except Exception:
                    pass
            offset += window
        self._rms_pending = pending[offset:]
