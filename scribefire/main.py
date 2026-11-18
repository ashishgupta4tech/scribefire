"""Scribefire - push-to-talk dictation.

Runs the state machine::

    idle -> recording -> processing -> idle

and wires the pieces together: a global hotkey, a microphone that stays open,
local Whisper transcription, three-tier text cleanup, a personal dictionary
and clipboard injection at the cursor.
"""

from __future__ import annotations

import argparse
import platform
import queue
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from . import autostart, hud, keychain
from .audio import AudioRecorder, list_input_devices
from .cleanup import CleanupPipeline, describe_tiers
from .config import Config
from .dictionary import PersonalDictionary
from .hotkey import HotkeyListener, check_macos_permissions, default_hotkey_label
from .inject import TextInjector
from .logs import get_logger, setup_logging
from .preview import PreviewEngine
from .transcribe import Transcriber, detect_device, device_label

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_PROCESSING = "processing"

IS_MACOS = platform.system() == "Darwin"

log = get_logger("app")


def _accepts_root(factory) -> bool:
    """True when *factory* takes a ``root`` keyword argument."""
    try:
        import inspect

        return "root" in inspect.signature(factory).parameters
    except Exception:
        return False


def configure_macos_app():
    """Put the macOS app into accessory mode.

    An accessory app has no Dock icon and never becomes active, which is what
    keeps the HUD from stealing focus.  Must run on the main thread, before
    the application finishes launching.
    """
    try:
        import AppKit  # type: ignore

        app = AppKit.NSApplication.sharedApplication()
        policy = getattr(AppKit, "NSApplicationActivationPolicyAccessory", None)
        if policy is not None:
            app.setActivationPolicy_(policy)
            log.info("macOS activation policy set to Accessory")
        return app
    except Exception as exc:
        log.warning("could not configure the macOS activation policy: %s", exc)
        return None


# --------------------------------------------------------------------------
# UI dispatcher
# --------------------------------------------------------------------------

class UiDispatcher:
    """Serialises UI work onto the thread that owns the UI toolkit.

    *   macOS: the main thread owns AppKit (and, when the tray is enabled,
        pystray).  A repeating ``NSTimer`` on the main run loop drains the
        queue, so work always lands on the main thread.
    *   Windows: a dedicated thread owns the Tk interpreter and pumps it.
    """

    def __init__(self):
        self._queue: "queue.Queue" = queue.Queue()
        self._root = None
        self._root_ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._timer = None
        self._lock = threading.RLock()
        self._settings_window = None

    @property
    def root(self):
        """The shared Tk root (Windows).  ``None`` on macOS."""
        return self._root

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if IS_MACOS:
            self._start_macos_pump()
        else:
            self._thread = threading.Thread(target=self._run_tk, name="scribefire-ui", daemon=True)
            self._thread.start()
            # Wait for the Tk root so the overlay and the settings window can
            # share this single interpreter.
            self._root_ready.wait(timeout=5.0)

    def _start_macos_pump(self) -> None:
        try:
            import AppKit  # type: ignore

            def _tick(_timer) -> None:
                self.drain()
                self._pump_tk()

            self._timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                0.03, True, _tick
            )
            # NSTimer keeps a weak reference to the block; hold it.
            self._timer_retain = self._timer
            log.info("main-thread UI pump started")
        except Exception as exc:
            log.error("could not start the main-thread UI pump: %s", exc)

    def _run_tk(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            try:
                root._scribefire_thread = threading.get_ident()
            except Exception:
                pass
            with self._lock:
                self._root = root
            self._root_ready.set()

            def _tick() -> None:
                self.drain()
                try:
                    root.after(20, _tick)
                except Exception:
                    pass

            root.after(20, _tick)
            root.mainloop()
        except Exception as exc:
            log.error("the Tk UI thread stopped: %s", exc)
            self._root_ready.set()
        finally:
            # Tear the interpreter down on the thread that created it.
            try:
                root.destroy()
            except Exception:
                pass

    # -- work ----------------------------------------------------------

    def submit(self, fn, *args, **kwargs) -> None:
        """Run *fn* on the UI thread as soon as possible."""
        self._queue.put((fn, args, kwargs))

    def drain(self) -> None:
        while True:
            try:
                fn, args, kwargs = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn(*args, **kwargs)
            except Exception:
                log.exception("UI task failed: %s", getattr(fn, "__name__", fn))

    def _pump_tk(self) -> None:
        """Let a Tk window created on the main thread redraw (macOS)."""
        settings = getattr(self, "_settings_window", None)
        root = None
        if settings is not None:
            root = getattr(settings, "root", None) or getattr(settings, "_root", None)
        if root is None:
            return
        try:
            root.update()
        except Exception:
            pass

    def register_settings_window(self, window) -> None:
        self._settings_window = window

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                try:
                    self._timer.invalidate()
                except Exception:
                    pass
                self._timer = None
            if self._root is not None:
                try:
                    self._root.quit()
                except Exception:
                    pass
                self._root = None


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class ScribefireApp:
    """The application controller and state machine."""

    def __init__(self, config: Config, no_tray: bool = False):
        self.config = config
        self.no_tray = no_tray
        self.log = get_logger("app")

        self._state = STATE_IDLE
        self._state_lock = threading.RLock()
        self._transition_at = time.perf_counter()
        self._jobs: "queue.Queue" = queue.Queue()
        self._stop_event = threading.Event()
        self._dictation_count = 0

        self.muted = bool(config.muted)

        self.recorder = AudioRecorder(
            device=config.microphone,
            preroll_ms=config.preroll_ms,
            on_pcm=self._on_pcm,
        )
        self.transcriber = Transcriber(
            model_name=config.whisper_model,
            device=config.device_type,
            compute_type=config.compute_type,
        )
        self.injector = TextInjector(config)
        self.cleanup = CleanupPipeline(config)
        self.dictionary = PersonalDictionary(config.dictionary)

        self.preview: Optional[PreviewEngine] = None
        self.hud = None
        self.tray = None
        self.settings = None
        self.hotkey: Optional[HotkeyListener] = None
        self.ui = UiDispatcher()

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def _set_state(self, new_state: str) -> None:
        with self._state_lock:
            old = self._state
            if old == new_state:
                return
            self._state = new_state
            now = time.perf_counter()
            elapsed = now - self._transition_at
            self._transition_at = now
        self.log.info("state %s -> %s (%.3fs in the previous state)", old, new_state, elapsed)
        if self.tray is not None:
            try:
                self.tray.update_state(new_state)
                self.tray.update_status(self.status_text())
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def start(self) -> None:
        self.log.info(
            "Scribefire starting - hotkey=%s (%s), hud=%s, live_preview=%s, model=%s, device=%s",
            self.config.hotkey,
            default_hotkey_label(),
            self.config.hud_enabled,
            self.config.live_preview,
            self.config.whisper_model,
            device_label(self.config.device_type),
        )

        if IS_MACOS:
            check_macos_permissions()
            configure_macos_app()

        threading.Thread(target=self._startup_checks, name="scribefire-checks", daemon=True).start()

        self.ui.start()
        self._start_hud()

        if not self.recorder.start():
            print("[audio] no microphone is available - dictation will not work")
            self.log.error("the microphone could not be opened")

        self._start_worker()
        self._start_hud_ticker()
        self._start_hotkey()
        self._sync_preview()

    def _startup_checks(self) -> None:
        """Report the status of the cleanup providers."""
        try:
            report = self.cleanup.check_providers()
            self.log.info("cleanup providers:\n%s", describe_tiers(report))
            print("[startup] cleanup providers:\n%s" % describe_tiers(report))
        except Exception:
            self.log.exception("the startup provider check failed")

    def _start_hotkey(self) -> None:
        self.hotkey = HotkeyListener(
            self.config.hotkey,
            self.on_hotkey_press,
            self.on_hotkey_release,
            logger=get_logger("hotkey"),
        )
        if not self.hotkey.start():
            print("[hotkey] the hotkey listener could not be started")

    def _start_worker(self) -> None:
        thread = threading.Thread(target=self._process_loop, name="scribefire-worker", daemon=True)
        thread.start()

    def _start_hud_ticker(self) -> None:
        """Push the rolling RMS buffer to the HUD at 30 fps."""
        from .hud_common import REDRAW_FPS

        def _tick() -> None:
            while not self._stop_event.is_set():
                if self.state == STATE_RECORDING and self.hud is not None:
                    self._hud_call("set_rms", self.recorder.rms_samples)
                time.sleep(1.0 / REDRAW_FPS)

        threading.Thread(target=_tick, name="scribefire-hud-ticker", daemon=True).start()

    def _start_hud(self) -> None:
        """Create the overlay.

        The constructors of both backends are thread agnostic: the macOS
        backend builds its panel on the main thread when it is shown, and
        the Windows backend owns a private Tk thread.  So the overlay can
        be built from wherever - but it is built here, once.
        """
        if not self.config.hud_enabled:
            return
        self.hud = hud.create_hud(self.config, logger=get_logger("hud"))
        if self.hud is None:
            self.log.warning("the HUD is unavailable on this platform")

    def _hud_call(self, name: str, *args) -> None:
        """Call a HUD method.

        Both backends are safe to call from any thread: the macOS one hops
        to the main thread itself, the Windows one posts to its private Tk
        thread.
        """
        overlay = self.hud
        if overlay is None:
            return
        method = getattr(overlay, name, None)
        if method is None:
            return
        try:
            method(*args)
        except Exception:
            self.log.exception("HUD call %s failed", name)

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _sync_preview(self) -> None:
        """Start or stop the preview engine to match the config toggle."""
        if not self.config.live_preview:
            if self.preview is not None:
                try:
                    self.preview.close()
                except Exception:
                    pass
                self.preview = None
            return

        if self.preview is not None:
            return

        def _load() -> None:
            engine = PreviewEngine(
                on_partial=self._on_preview_partial,
                on_final=self._on_preview_partial,
                logger=get_logger("preview"),
            )
            if not engine.load():
                return
            if not engine.start():
                return
            self.preview = engine
            self.log.info("live preview is ready")

        threading.Thread(target=_load, name="scribefire-preview", daemon=True).start()

    def _on_pcm(self, pcm) -> None:
        """Forward the 16 kHz stream to the preview engine."""
        preview = self.preview
        if preview is None or not self.config.live_preview:
            return
        try:
            preview.feed(pcm)
        except Exception:
            pass

    def _on_preview_partial(self, text: str) -> None:
        if not text:
            return
        self._hud_call("set_preview_text", text)

    # ------------------------------------------------------------------
    # Hotkey callbacks
    # ------------------------------------------------------------------

    def on_hotkey_press(self) -> None:
        with self._state_lock:
            if self.muted:
                self.log.info("hotkey ignored - muted")
                return
            if self._state != STATE_IDLE:
                return
            self._set_state(STATE_RECORDING)

        self.recorder.begin_recording()

        preview = self.preview
        if preview is not None:
            try:
                preview.begin()
            except Exception:
                pass

        if self.hud is not None:
            self._hud_call("show")
            self._hud_call("set_state", "listening")
            self._hud_call("clear_preview")

    def on_hotkey_release(self) -> None:
        with self._state_lock:
            if self._state != STATE_RECORDING:
                return
            self._set_state(STATE_PROCESSING)

        audio = self.recorder.end_recording()

        preview = self.preview
        if preview is not None:
            try:
                preview.stop()  # the final Vosk result is discarded
            except Exception:
                pass

        if self.hud is not None:
            self._hud_call("set_state", "processing")

        if audio is None or audio.size == 0:
            self.log.info("nothing was recorded")
            self._finish()
            return

        self._jobs.put(audio)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def _process_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                audio = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(audio)
            except Exception:
                self.log.exception("the dictation pipeline failed")
                self._finish()

    def _process(self, audio) -> None:
        started = time.perf_counter()

        if not self.transcriber.is_loaded:
            self.transcriber.load(progress=lambda message: print("[model] %s" % message))

        raw = self.transcriber.transcribe(audio)
        transcribed_at = time.perf_counter()
        self.log.info("transcribed in %.2fs: %r", transcribed_at - started, raw)

        if not raw:
            self.log.info("the transcription was empty")
            self._finish()
            return

        cleaned, tier = self.cleanup.run(raw)
        cleaned_at = time.perf_counter()

        final, applied = self.dictionary.apply(cleaned)
        self.log.info(
            "cleanup tier=%s in %.2fs; dictionary applied %d replacement(s)",
            tier,
            cleaned_at - transcribed_at,
            len(applied),
        )
        self.log.info("dictation: raw=%r -> final=%r", raw, final)

        self.injector.inject(final)

        words = len(final.split())
        self.config.add_words(words)
        self._dictation_count += 1
        if self._dictation_count % 10 == 0:
            try:
                self.config.save()
            except Exception:
                pass

        self._finish(total=time.perf_counter() - started)

    def _finish(self, total: Optional[float] = None) -> None:
        self._hud_call("set_state", "done")
        if total is not None:
            self.log.info("dictation finished in %.2fs", total)
        self._set_state(STATE_IDLE)

    # ------------------------------------------------------------------
    # Controller interface (used by the tray and the settings window)
    # ------------------------------------------------------------------

    def status_text(self) -> str:
        if self.muted:
            return "Muted"
        return {
            STATE_IDLE: "Ready",
            STATE_RECORDING: "Recording...",
            STATE_PROCESSING: "Processing...",
        }.get(self.state, "Ready")

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        self.config.muted = self.muted
        try:
            self.config.save()
        except Exception:
            pass
        self.log.info("mute toggled: %s", self.muted)
        if self.tray is not None:
            try:
                self.tray.update_status(self.status_text())
            except Exception:
                pass

    def open_settings(self) -> None:
        def _open() -> None:
            from .settings import SettingsWindow

            if self.settings is None:
                if _accepts_root(SettingsWindow):
                    self.settings = SettingsWindow(
                        self, logger=get_logger("settings"), root=self.ui.root
                    )
                else:
                    self.settings = SettingsWindow(self, logger=get_logger("settings"))
            self.ui.register_settings_window(self.settings)
            self.settings.show()

        self.ui.submit(_open)

    def config_snapshot(self) -> Dict[str, Any]:
        return self.config.snapshot()

    def reload_config(self) -> Dict[str, Any]:
        self.config = Config.load(self.config.path)
        self.dictionary.set_pairs(self.config.dictionary)
        return self.config.snapshot()

    def get_api_key(self) -> str:
        return keychain.get_api_key()

    def set_api_key(self, value: str) -> None:
        keychain.set_api_key(value)

    def status_lines(self) -> List[str]:
        stats = self.config.stats or {}
        return [
            "Words today: %d" % int(stats.get("words_today", 0)),
            "Words total: %d" % int(stats.get("words_total", 0)),
        ]

    def apply_settings(self, values: Dict[str, Any]) -> None:
        """Persist a dotted-path mapping and apply it live where possible."""
        if not values:
            return
        self.config.update(values)
        try:
            self.config.save()
        except Exception as exc:
            self.log.error("could not save the config: %s", exc)

        # Hotkey: restart the listener.
        if "hotkey" in values and self.hotkey is not None:
            self.hotkey.stop()
            self._start_hotkey()

        if "preroll_ms" in values:
            self.recorder.preroll_ms = self.config.preroll_ms

        if "microphone" in values:
            self.recorder.set_device(self.config.microphone)

        if "whisper_model" in values:
            # The model reloads on the next dictation rather than blocking here.
            self.transcriber.set_model(self.config.whisper_model, self.config.device_type)

        if "dictionary" in values:
            self.dictionary.set_pairs(self.config.dictionary)

        if "live_preview" in values:
            self._sync_preview()

        if "start_on_login" in values:
            autostart.set_start_on_login(bool(self.config.start_on_login))

        self.log.info("applied settings: %s", ", ".join(sorted(values)))

    def quit(self) -> None:
        self.log.info("shutting down")
        self._stop_event.set()
        try:
            self.config.save()
        except Exception:
            pass
        if self.hotkey is not None:
            self.hotkey.stop()
        try:
            self.recorder.stop()
        except Exception:
            pass
        if self.preview is not None:
            try:
                self.preview.close()
            except Exception:
                pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.ui.stop()
        sys.exit(0)

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def run_tray(self) -> None:
        """Run the tray icon.  On macOS this must be the main thread."""
        from .icons import state_icon
        from .tray import TrayIcon

        self.tray = TrayIcon(self, icon_provider=state_icon, no_tray=False)
        try:
            self.tray.run()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop_event.set()

    def run_headless(self) -> None:
        """Run without a tray icon (debugging / headless machines)."""
        print("[scribefire] running without a tray icon - press Ctrl+C to quit")
        try:
            while not self._stop_event.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("\n[scribefire] quitting")
        finally:
            self._stop_event.set()

    def run_checks(self) -> int:
        """Run the startup checks and exit."""
        print("Scribefire %s" % platform.platform())
        print("config:  %s" % self.config.path)
        print("log:     %s" % __import__("scribefire.logs", fromlist=["log_path"]).log_path())
        print("hotkey:  %s" % self.config.hotkey)
        print("device:  %s" % device_label(self.config.device_type))
        print("model:   %s" % self.config.whisper_model)
        print("\ninput devices:")
        for info in list_input_devices():
            print("  [%s] %s%s" % (info["id"], info["name"], " (default)" if info["is_default"] else ""))
        print("\ncleanup providers:")
        print(describe_tiers(self.cleanup.check_providers()))
        return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scribefire",
        description="Push-to-talk dictation for macOS and Windows. Local Whisper, no subscription.",
    )
    parser.add_argument("--no-tray", action="store_true", help="run without a system tray icon")
    parser.add_argument("--config", default=None, help="path to the config file")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--check", action="store_true", help="run the startup checks and exit")
    args = parser.parse_args(argv)

    setup_logging("DEBUG" if args.debug else "INFO")

    config = Config.load(args.config)
    app = ScribefireApp(config, no_tray=args.no_tray)

    if args.check:
        return app.run_checks()

    app.start()

    if args.no_tray:
        app.run_headless()
    else:
        app.run_tray()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
