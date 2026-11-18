"""Local Whisper transcription via ``faster-whisper``.

The model is loaded once and reused - loading per dictation adds seconds of
latency.  Models are downloaded on first use with visible progress and cached
in the per-OS user cache directory.  No audio ever leaves the machine.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

from .config import model_cache_dir
from .logs import get_logger

log = get_logger("transcribe")

VALID_MODELS = ("tiny", "base", "small", "medium")

#: Rough on-disk size per model, shown in the settings window.
MODEL_SIZES = {
    "tiny": "75 MB",
    "base": "145 MB",
    "small": "485 MB",
    "medium": "1.5 GB",
}


# --------------------------------------------------------------------------
# Device selection
# --------------------------------------------------------------------------

def cuda_available() -> bool:
    """True when an NVIDIA GPU that ctranslate2 can use is present."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return True
    except Exception:
        pass

    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
        except Exception:
            pass
    return False


def detect_device(preference: str = "auto") -> str:
    """Return ``"cuda"`` or ``"cpu"``.

    ``auto`` picks CUDA when an NVIDIA GPU is available.  On Apple Silicon
    CPU + int8 is the correct choice: faster-whisper has no Metal backend.
    """
    preference = (preference or "auto").lower()
    if preference in ("cpu", "cuda"):
        return preference
    if cuda_available():
        log.info("CUDA device detected")
        return "cuda"
    log.info("no CUDA device detected, using CPU")
    return "cpu"


def device_label(preference: str = "auto") -> str:
    return "CUDA" if detect_device(preference) == "cuda" else "CPU"


# --------------------------------------------------------------------------
# Model download
# --------------------------------------------------------------------------

def _repo_for(model_name: str) -> str:
    return "Systran/faster-whisper-%s" % model_name


def _snapshot_dir(cache_root: Path, model_name: str) -> Optional[Path]:
    """Find an already downloaded snapshot for *model_name*.

    ``huggingface_hub`` sanitises the repo id into a single directory name
    (``Systran/faster-whisper-base`` -> ``models--Systran--faster-whisper-base``),
    so that is the name to look for.  The unsanitised form is checked too, in
    case the model was fetched another way.
    """
    repo = _repo_for(model_name)
    candidates = [
        cache_root / ("models--" + repo.replace("/", "--")),
        cache_root / "models--Systran" / repo,
    ]
    for hub in candidates:
        snapshots = hub / "snapshots"
        if not snapshots.exists():
            continue
        dirs = [p for p in snapshots.iterdir() if p.is_dir()]
        if not dirs:
            continue
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for candidate in dirs:
            if (candidate / "model.bin").exists():
                return candidate
    return None


def ensure_model(model_name: str, cache_root: Optional[Path] = None) -> Path:
    """Make sure the model is on disk and return its local directory.

    Downloads on first use with a visible progress indicator.
    """
    cache_root = Path(cache_root) if cache_root else model_cache_dir()
    cache_root.mkdir(parents=True, exist_ok=True)

    existing = _snapshot_dir(cache_root, model_name)
    if existing is not None and (existing / "model.bin").exists():
        log.info("whisper model %r already cached at %s", model_name, existing)
        return existing

    repo = _repo_for(model_name)
    print(f"[model] downloading faster-whisper '{model_name}' - this happens once")
    print(f"[model] cache: {cache_root}")

    try:
        from huggingface_hub import snapshot_download

        path = snapshot_download(
            repo_id=repo,
            cache_dir=str(cache_root),
            allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*", "preprocessor_config.json"],
        )
        log.info("downloaded %s to %s", repo, path)
        return Path(path)
    except Exception as exc:
        log.warning("huggingface_hub download failed (%s), trying faster-whisper's own downloader", exc)

    try:
        from faster_whisper.utils import download_model

        path = download_model(model_name, cache_dir=str(cache_root))
        return Path(path)
    except Exception as exc:
        raise RuntimeError(
            "could not download the '%s' model: %s\n"
            "Check your internet connection, or pre-download the model into %s" % (model_name, exc, cache_root)
        ) from exc


def model_is_downloaded(model_name: str, cache_root: Optional[Path] = None) -> bool:
    cache_root = Path(cache_root) if cache_root else model_cache_dir()
    snapshot = _snapshot_dir(cache_root, model_name)
    return bool(snapshot and (snapshot / "model.bin").exists())


# --------------------------------------------------------------------------
# Transcriber
# --------------------------------------------------------------------------

class Transcriber:
    """Wraps a single loaded ``faster-whisper`` model."""

    def __init__(
        self,
        model_name: str = "base",
        device: str = "auto",
        compute_type: str = "int8",
        cache_root: Optional[Path] = None,
        logger=None,
    ):
        self.model_name = model_name
        self.compute_type = compute_type
        self.cache_root = Path(cache_root) if cache_root else model_cache_dir()
        self.log = logger or log
        self._model = None
        self._device = detect_device(device)
        self._lock = threading.RLock()

    # -- properties ----------------------------------------------------

    @property
    def device(self) -> str:
        return self._device

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    # -- loading -------------------------------------------------------

    def load(self, progress: Optional[Callable[[str], None]] = None) -> bool:
        """Download (if needed) and load the model.  Safe to call twice."""
        with self._lock:
            if self._model is not None:
                return True
            try:
                path = ensure_model(self.model_name, self.cache_root)
            except Exception as exc:
                self.log.error("model download failed: %s", exc)
                if progress:
                    progress(f"model download failed: {exc}")
                return False

            try:
                from faster_whisper import WhisperModel
            except Exception as exc:
                self.log.error("faster-whisper is not installed: %s", exc)
                if progress:
                    progress(f"faster-whisper is not installed: {exc}")
                return False

            try:
                if progress:
                    progress(f"loading '{self.model_name}' on {self._device.upper()}...")
                self._model = WhisperModel(
                    str(path),
                    device=self._device,
                    compute_type=self.compute_type,
                    local_files_only=True,
                )
            except Exception as exc:
                # A CUDA failure must not be fatal - fall back to the CPU.
                if self._device == "cuda":
                    self.log.warning("CUDA init failed (%s), falling back to CPU", exc)
                    self._device = "cpu"
                    try:
                        self._model = WhisperModel(
                            str(path),
                            device="cpu",
                            compute_type=self.compute_type,
                            local_files_only=True,
                        )
                    except Exception as exc2:
                        self.log.error("could not load the model on the CPU either: %s", exc2)
                        if progress:
                            progress(f"could not load the model: {exc2}")
                        return False
                else:
                    self.log.error("could not load the model: %s", exc)
                    if progress:
                        progress(f"could not load the model: { exc}")
                    return False

            self.log.info("whisper model '%s' ready on %s", self.model_name, self._device)
            if progress:
                progress(f"model '{self.model_name}' ready on {self._device.upper()}")
            return True

    def unload(self) -> None:
        with self._lock:
            self._model = None

    def set_model(self, model_name: str, device: Optional[str] = None) -> None:
        """Switch models; the new model is loaded on the next dictation."""
        with self._lock:
            if model_name != self.model_name or (device and device != self._device):
                self.model_name = model_name
                if device:
                    self._device = detect_device(device)
                self._model = None

    # -- transcription -------------------------------------------------

    def transcribe(self, audio: np.ndarray, language: Optional[str] = None) -> str:
        """Transcribe 16 kHz mono float32 audio into text."""
        with self._lock:
            model = self._model
        if model is None:
            raise RuntimeError("the transcription model is not loaded")
        if audio is None or audio.size == 0:
            return ""

        try:
            return self._transcribe_with(model, audio, language)
        except Exception as exc:
            if self._device != "cuda":
                raise
            # A GPU without the CUDA runtime libraries loads fine and then
            # fails on the first inference.  Fall back to the CPU instead of
            # taking the whole app down.
            self.log.warning("CUDA inference failed (%s) - falling back to the CPU", exc)
            print("[transcribe] CUDA failed (%s) - falling back to the CPU" % exc)
            self._device = "cpu"
            with self._lock:
                self._model = None
            if not self.load():
                raise RuntimeError("the transcription model could not be loaded on the CPU") from exc
            with self._lock:
                model = self._model
            return self._transcribe_with(model, audio, language)

    def _transcribe_with(self, model, audio: np.ndarray, language: Optional[str]) -> str:
        """Run one transcription with a given model."""
        segments, _info = model.transcribe(
            np.asarray(audio, dtype=np.float32),
            language=language,
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
            word_timestamps=False,
        )

        parts: List[str] = []
        for segment in segments:
            text = (getattr(segment, "text", "") or "").strip()
            if text:
                parts.append(text)
        return " ".join(parts).strip()
