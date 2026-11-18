"""Live transcription preview with Vosk.

This is the throwaway preview engine.  ``faster-whisper`` still produces the
text that is actually pasted; the Vosk output is only ever shown on the HUD
and is never injected.

Vosk runs on its own worker thread.  It must never block the audio
callback, the Whisper path or the UI redraw - when it falls behind, frames
are dropped rather than queued, because a stale preview is worse than a
sparse one.
"""

from __future__ import annotations

import json
import queue
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import urllib.request

from .config import model_cache_dir
from .logs import get_logger

log = get_logger("preview")

DEFAULT_VOSK_MODEL = "vosk-model-small-en-us-0.15"
VOSK_BASE_URL = "https://alphacephei.com/vosk/models"
TARGET_RATE = 16000

#: How many audio chunks may wait for the Vosk worker before frames are
#: dropped.  20 ms chunks -> ~160 ms of slack.
QUEUE_SIZE = 8


# --------------------------------------------------------------------------
# Model download
# --------------------------------------------------------------------------

def _download_with_progress(url: str, destination: Path, label: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".part")
    print("[preview] downloading %s" % label)
    with urllib.request.urlopen(url, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        last_percent = -5
        with open(temp, "wb") as fh:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                if total:
                    percent = int(downloaded * 100 / total)
                    if percent >= last_percent + 5:
                        last_percent = percent
                        print("[preview]   %s %d%%" % (label, percent))
    temp.replace(destination)


def ensure_vosk_model(model_name: str = DEFAULT_VOSK_MODEL, cache_root: Optional[Path] = None) -> Path:
    """Download the Vosk model on first use and return its directory."""
    cache_root = Path(cache_root) if cache_root else model_cache_dir()
    target = cache_root / model_name
    if target.exists() and any(target.rglob("final.mdl")):
        log.info("vosk model already present at %s", target)
        return target

    cache_root.mkdir(parents=True, exist_ok=True)
    archive = cache_root / (model_name + ".zip")
    url = "%s/%s.zip" % (VOSK_BASE_URL, model_name)
    try:
        _download_with_progress(url, archive, model_name)
    except Exception as exc:
        raise RuntimeError(
            "could not download the Vosk model '%s': %s\n"
            "The preview needs the model downloaded once; check your connection." % (model_name, exc)
        ) from exc

    print("[preview] extracting %s" % model_name)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(cache_root)
    try:
        archive.unlink()
    except OSError:
        pass

    if not target.exists():
        raise RuntimeError("the Vosk model archive did not contain '%s'" % model_name)
    return target


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class PreviewEngine:
    """Streams 16 kHz mono audio into Vosk and reports partial results."""

    def __init__(
        self,
        model_name: str = DEFAULT_VOSK_MODEL,
        cache_root: Optional[Path] = None,
        on_partial: Optional[Callable[[str], None]] = None,
        on_final: Optional[Callable[[str], None]] = None,
        logger=None,
    ):
        self.model_name = model_name
        self.cache_root = Path(cache_root) if cache_root else model_cache_dir()
        self.on_partial = on_partial
        self.on_final = on_final
        self.log = logger or log

        self._model = None
        self._recognizer = None
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=QUEUE_SIZE)
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._dropped = 0
        self._lock = threading.RLock()

    # -- properties ----------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def dropped_frames(self) -> int:
        return self._dropped

    # -- lifecycle -----------------------------------------------------

    def load(self) -> bool:
        """Download (if needed) and load the Vosk model."""
        if self._model is not None:
            return True
        try:
            import vosk  # noqa: F401
        except Exception as exc:
            self.log.error("vosk is not installed: %s", exc)
            return False
        try:
            vosk.SetLogLevel(-1)
        except Exception:
            pass

        try:
            path = ensure_vosk_model(self.model_name, self.cache_root)
        except Exception as exc:
            self.log.error("vosk model unavailable: %s", exc)
            print("[preview] %s" % exc)
            return False

        try:
            from vosk import Model

            self._model = Model(str(path))
            self.log.info("vosk model '%s' ready", self.model_name)
            return True
        except Exception as exc:
            self.log.error("could not load the vosk model: %s", exc)
            return False

    def start(self) -> bool:
        """Start the worker thread.  Safe to call repeatedly."""
        if not self.load():
            return False
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return True
            self._stop.clear()
            self._worker = threading.Thread(target=self._run, name="scribefire-vosk", daemon=True)
            self._worker.start()
            return True

    def stop(self) -> str:
        """Stop the worker and return the final Vosk text (which is discarded)."""
        with self._lock:
            worker = self._worker
            self._worker = None
        self._stop.set()
        if worker is not None:
            worker.join(timeout=2.0)
        try:
            self._drain()
        except Exception:
            pass
        final = self._final_text()
        self._recognizer = None
        if self.on_final is not None:
            try:
                self.on_final(final)
            except Exception:
                self.log.exception("preview final callback failed")
        return final

    def close(self) -> None:
        self.stop()
        self._model = None

    # -- recording -----------------------------------------------------

    def begin(self) -> None:
        """Prepare a fresh recogniser for a new dictation."""
        if self._model is None:
            return
        try:
            from vosk import KaldiRecognizer

            with self._lock:
                self._recognizer = KaldiRecognizer(self._model, TARGET_RATE)
        except Exception as exc:
            self.log.error("could not create a Vosk recogniser: %s", exc)
            self._recognizer = None

    def feed(self, pcm: np.ndarray) -> None:
        """Queue a 16 kHz mono chunk.  Never blocks; drops frames when late."""
        if pcm is None or self._model is None:
            return
        try:
            data = np.asarray(pcm, dtype=np.float32).tobytes()
        except Exception:
            return
        if not data:
            return
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            # Drop the OLDEST frame: a stale preview is worse than a gap.
            try:
                self._queue.get_nowait()
                self._dropped += 1
                self._queue.put_nowait(data)
            except Exception:
                pass

    # -- worker --------------------------------------------------------

    def _drain(self) -> None:
        while True:
            try:
                data = self._queue.get_nowait()
            except queue.Empty:
                return
            self._accept(data)

    def _accept(self, data: bytes) -> None:
        with self._lock:
            recognizer = self._recognizer
        if recognizer is None:
            return
        try:
            finished = recognizer.AcceptWaveform(data)
            if finished:
                payload = json.loads(recognizer.Result() or "{}")
                text = (payload.get("text") or "").strip()
                if text:
                    self._emit(text)
            else:
                payload = json.loads(recognizer.PartialResult() or "{}")
                text = (payload.get("partial") or "").strip()
                if text:
                    self._emit(text)
        except Exception:
            self.log.exception("vosk worker failed to process a chunk")

    def _emit(self, text: str) -> None:
        if self.on_partial is None:
            return
        try:
            self.on_partial(text)
        except Exception:
            self.log.exception("preview partial callback failed")

    def _final_text(self) -> str:
        with self._lock:
            recognizer = self._recognizer
        if recognizer is None:
            return ""
        try:
            payload = json.loads(recognizer.FinalResult() or "{}")
            return (payload.get("text") or "").strip()
        except Exception:
            return ""

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self._accept(data)
