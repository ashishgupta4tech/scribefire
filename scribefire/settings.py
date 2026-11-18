"""Cross-platform Tkinter settings window for Scribefire.

A single implementation that works the same on macOS and Windows - this is a
normal window, so it is allowed to take focus and no platform specific focus
tricks are needed.

Tkinter rules used here
-----------------------
* **One Tk interpreter per process.**  ``tk.Tk()`` is created at most once
  and kept in the module level ``_ROOT``.  The settings window is always a
  ``tk.Toplevel`` on top of that root.  If something else in the process
  already created a root (``tk._default_root``) it is adopted instead of
  creating a second interpreter.
* **The window lives on the Tk thread.**  ``show()`` must be called from the
  thread that owns the interpreter - on macOS that is the main thread.
  Calling it from anywhere else raises ``RuntimeError``.
* Network probes run on a worker thread and post their result back with
  ``root.after(0, ...)`` so the UI thread is never blocked.
"""

from __future__ import annotations

import copy
import json
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk

from .config import default_config, hotkey_choices
from .logs import get_logger

log = get_logger("settings")

# --------------------------------------------------------------------------
# Module level Tk root
# --------------------------------------------------------------------------

_ROOT: Optional[tk.Tk] = None
_ROOT_THREAD: Optional[threading.Thread] = None
_ROOT_LOCK = threading.RLock()


def _ensure_root(external: Optional[tk.Misc] = None) -> tk.Misc:
    """Return the Tk root this window should live on.

    *external* is the root supplied by the application.  It is used as-is:
    the caller owns it and drives its event loop.

    Without an external root the module creates (and keeps) one of its own.
    A root that belongs to a *different* thread is never adopted - driving
    another thread's Tk interpreter from here is exactly the kind of bug
    that hangs an app without an error.
    """
    global _ROOT, _ROOT_THREAD
    with _ROOT_LOCK:
        if external is not None:
            _ROOT = external  # type: ignore[assignment]
            _ROOT_THREAD = threading.current_thread()
            return _ROOT

        if _ROOT is not None and _ROOT_THREAD is threading.current_thread():
            return _ROOT

        existing = getattr(tk, "_default_root", None)
        if existing is not None and getattr(existing, "_scribefire_thread", None) == threading.get_ident():
            # A root created on this very thread - safe to reuse.
            _ROOT = existing  # type: ignore[assignment]
        else:
            root = tk.Tk()
            root.withdraw()
            try:
                root._scribefire_thread = threading.get_ident()
            except Exception:
                pass
            _ROOT = root
        _ROOT_THREAD = threading.current_thread()
        return _ROOT


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Cleanup tier selector: displayed label -> config value.
TIER_CHOICES: List[Tuple[str, str]] = [
    ("Automatic (failover)", "auto"),
    ("Groq only", "groq"),
    ("Ollama only", "ollama"),
    ("Rule-based only", "rule"),
]

#: Text shown by the "Test connection" button.
MSG_MODEL_NOT_FOUND = "model not found \u2014 check the model name in config"
MSG_UNAUTHORIZED = "unauthorized \u2014 check the API key"
MSG_NOT_REACHABLE = "not reachable"

#: How long the "Saved" confirmation stays visible.
SAVED_TOAST_MS = 2000

#: Config paths edited by this window and their defaults.
CONFIG_PATHS: Tuple[str, ...] = (
    "hotkey",
    "preroll_ms",
    "hud_enabled",
    "live_preview",
    "start_on_login",
    "microphone",
    "whisper_model",
    "cleanup.mode",
    "cleanup.groq_base_url",
    "cleanup.groq_model",
    "cleanup.ollama_base_url",
    "cleanup.ollama_model",
    "dictionary",
)


def _dig(data: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path out of a nested dict."""
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return copy.deepcopy(node)


class _PingError(Exception):
    """Raised by the connection probe with a user readable message."""


def _http_get_json(url: str, timeout: float, headers: Optional[Dict[str, str]] = None) -> Dict:
    """GET *url* and parse the JSON body using the standard library only."""
    request = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _PingError(MSG_MODEL_NOT_FOUND) from exc
        if exc.code in (401, 403):
            raise _PingError(MSG_UNAUTHORIZED) from exc
        raise _PingError("HTTP %s" % exc.code) from exc
    except urllib.error.URLError as exc:
        raise _PingError("%s (%s)" % (MSG_NOT_REACHABLE, exc.reason)) from exc
    except Exception as exc:
        raise _PingError("%s (%s)" % (MSG_NOT_REACHABLE, exc)) from exc

    try:
        data = json.loads(raw)
    except Exception as exc:
        raise _PingError("invalid JSON from %s" % url) from exc
    if not isinstance(data, dict):
        raise _PingError("unexpected response from %s" % url)
    return data


class SettingsWindow:
    """The Scribefire settings window.

    Parameters
    ----------
    controller:
        The application object.  It must provide ``config_snapshot()``,
        ``apply_settings(values)``, ``get_api_key()``, ``set_api_key(key)``,
        ``status_lines()`` and ``reload_config()``.
    logger:
        Optional logger; defaults to ``logging.getLogger("scribefire.settings")``.
    root:
        Optional Tk root supplied by the application.  The settings window
        then lives on the application's UI thread and event loop instead of
        creating an interpreter of its own.
    """

    def __init__(self, controller: Any, logger: Any = None, root: Optional[tk.Misc] = None):
        self._controller = controller
        self._log = logger or get_logger("settings")
        self._lock = threading.RLock()
        self._external_root = root

        self._dirty = False
        self._baseline: Dict[str, Any] = {}
        self._baseline_api_key = ""
        self._device_preference = "auto"

        self._win: Optional[tk.Toplevel] = None
        self._root: Optional[tk.Misc] = None

        self._variables: Dict[str, tk.Variable] = {}
        self._widgets: Dict[str, tk.Widget] = {}

        self._mic_choices: List[Tuple[str, Any]] = [("System default", None)]
        self._tier_choices: List[Tuple[str, str]] = list(TIER_CHOICES)

        self._test_results: List[str] = []
        self._toast_id: Optional[str] = None

        # Public handles so a caller (or a test) can drive the window.
        self.save_button: Optional[ttk.Button] = None
        self.cancel_button: Optional[ttk.Button] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_dirty(self) -> bool:
        """True when the form differs from the values it was opened with."""
        with self._lock:
            return self._dirty

    @property
    def variables(self) -> Dict[str, tk.Variable]:
        """The Tk variables backing the form.

        Keyed by config path; ``api_key`` is the keychain field (it is not a
        config path - it never reaches the config file).
        """
        return self._variables

    @property
    def widgets(self) -> Dict[str, tk.Widget]:
        """The widgets of the form, keyed by config path."""
        return self._widgets

    def show(self) -> None:
        """Build (or re-focus) the window.

        Must be called on the Tk thread - the main thread on macOS.
        """
        if (
            self._external_root is None
            and threading.current_thread() is not threading.main_thread()
            and _ROOT is None
        ):
            raise RuntimeError(
                "SettingsWindow.show() must be called on the main thread"
            )

        root = _ensure_root(self._external_root)
        with _ROOT_LOCK:
            if _ROOT_THREAD is not threading.current_thread():
                raise RuntimeError(
                    "SettingsWindow.show() must be called on the thread that owns "
                    "the Tk interpreter (%s)" % (_ROOT_THREAD.name if _ROOT_THREAD else "?")
                )
        self._root = root

        if self._win is not None:
            try:
                if self._win.winfo_exists():
                    self._win.deiconify()
                    self._win.lift()
                    self._win.focus_set()
                    return
            except tk.TclError:
                pass
            self._win = None

        self._load_baseline()
        self._build()
        self._wire_traces()
        self._populate(self._baseline)
        self._refresh_dirty()
        self._refresh_footer()

        self._win.deiconify()
        self._win.lift()
        try:
            self._win.focus_set()
        except tk.TclError:
            pass

    def close(self) -> None:
        """Close the window, prompting when there are unsaved changes."""
        if self._win is None:
            return
        if self.is_dirty:
            choice = self._ask_save_discard_cancel()
            if choice == "cancel":
                return
            if choice == "save":
                self._save(close_after=True)
                return
            self._revert()
        self._destroy()

    def save(self) -> None:
        """Apply the current form values.  The window stays open."""
        self._save(close_after=False)

    # ------------------------------------------------------------------
    # Baseline handling
    # ------------------------------------------------------------------

    def _load_baseline(self) -> None:
        """Read the current configuration and remember it as the baseline."""
        try:
            snapshot = self._controller.config_snapshot()
        except Exception:
            self._log.exception("could not read the configuration")
            snapshot = {}

        defaults = default_config()
        values: Dict[str, Any] = {}
        for path in CONFIG_PATHS:
            value = _dig(snapshot, path, _dig(defaults, path))
            values[path] = value

        self._baseline = values
        self._baseline["microphone"] = self._normalize_microphone(values.get("microphone"))
        self._baseline["dictionary"] = self._normalize_dictionary(values.get("dictionary"))
        self._device_preference = str(_dig(snapshot, "device_type", "auto") or "auto")

        try:
            self._baseline_api_key = str(self._controller.get_api_key() or "")
        except Exception:
            self._log.exception("could not read the API key")
            self._baseline_api_key = ""

    @staticmethod
    def _normalize_dictionary(value: Any) -> List[List[str]]:
        """Coerce the dictionary config into a list of ``[wrong, correct]``."""
        rows: List[List[str]] = []
        for item in value or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                rows.append([str(item[0]), str(item[1])])
        return rows

    def _normalize_microphone(self, value: Any) -> Any:
        """Map a configured microphone onto a device id where possible."""
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        text = str(value)
        for device in self._input_devices():
            if str(device.get("name", "")).strip() == text.strip():
                return device.get("id")
        return text

    @staticmethod
    def _input_devices() -> List[Dict[str, Any]]:
        """The available input devices (empty when PortAudio is missing)."""
        try:
            from .audio import list_input_devices

            return [dict(device) for device in list_input_devices()]
        except Exception:
            log.exception("could not list the input devices")
            return []

    # ------------------------------------------------------------------
    # Building the UI
    # ------------------------------------------------------------------

    def _build(self) -> None:
        win = tk.Toplevel(self._root)
        self._win = win
        win.title("Scribefire Settings")
        win.geometry("700x640")
        win.minsize(620, 560)
        win.protocol("WM_DELETE_WINDOW", self._on_close_requested)
        win.bind("<Escape>", self._on_close_requested)
        win.bind("<Control-s>", self._on_save_shortcut)
        win.bind("<Command-s>", self._on_save_shortcut)

        notebook = ttk.Notebook(win)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        self._build_general(ttk.Frame(notebook))
        self._build_transcription(ttk.Frame(notebook))
        self._build_cleanup(ttk.Frame(notebook))
        self._build_dictionary(ttk.Frame(notebook))

        notebook.add(self._general_frame, text="General")
        notebook.add(self._transcription_frame, text="Transcription")
        notebook.add(self._cleanup_frame, text="Cleanup")
        notebook.add(self._dictionary_frame, text="Dictionary")

        self._build_footer(ttk.Frame(win))

    # -- general -------------------------------------------------------

    def _build_general(self, frame: ttk.Frame) -> None:
        self._general_frame = frame
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)

        hotkey_var = tk.StringVar(value="alt_r")
        self._variables["hotkey"] = hotkey_var
        combo = ttk.Combobox(
            frame,
            textvariable=hotkey_var,
            values=hotkey_choices(),
            state="readonly",
            width=24,
        )
        self._widgets["hotkey"] = combo
        ttk.Label(frame, text="Push-to-talk key:").grid(row=0, column=0, sticky="w", padx=10, pady=8)
        combo.grid(row=0, column=1, sticky="w", padx=10, pady=8)
        ttk.Label(
            frame,
            text="alt_r is Right Alt on Windows and Right Option (⌥) on macOS.",
            foreground="#666666",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=10)

        preroll_var = tk.IntVar(value=500)
        self._variables["preroll_ms"] = preroll_var
        preroll_label = tk.StringVar(value="500 ms")
        self._preroll_label_var = preroll_label
        ttk.Label(frame, text="Pre-roll:").grid(row=2, column=0, sticky="w", padx=10, pady=8)
        scale = ttk.Scale(
            frame,
            from_=0,
            to=1000,
            orient="horizontal",
            command=self._on_preroll_changed,
        )
        self._widgets["preroll_ms"] = scale
        scale.grid(row=2, column=1, sticky="ew", padx=10, pady=8)
        ttk.Label(frame, textvariable=preroll_label, width=10).grid(row=2, column=2, sticky="w", padx=10)

        hud_var = tk.BooleanVar(value=True)
        self._variables["hud_enabled"] = hud_var
        hud = ttk.Checkbutton(frame, text="Show the heads-up display while dictating", variable=hud_var)
        self._widgets["hud_enabled"] = hud
        hud.grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=4)

        preview_var = tk.BooleanVar(value=False)
        self._variables["live_preview"] = preview_var
        preview = ttk.Checkbutton(frame, text="Live preview of the transcript", variable=preview_var)
        self._widgets["live_preview"] = preview
        preview.grid(row=4, column=0, columnspan=3, sticky="w", padx=10, pady=4)

        login_var = tk.BooleanVar(value=False)
        self._variables["start_on_login"] = login_var
        login = ttk.Checkbutton(frame, text="Start Scribefire when I log in", variable=login_var)
        self._widgets["start_on_login"] = login
        login.grid(row=5, column=0, columnspan=3, sticky="w", padx=10, pady=4)

    def _on_preroll_changed(self, raw: Any) -> None:
        try:
            value = int(round(float(raw)))
        except (TypeError, ValueError):
            return
        self._variables["preroll_ms"].set(value)
        self._preroll_label_var.set("%d ms" % value)

    # -- transcription -------------------------------------------------

    def _build_transcription(self, frame: ttk.Frame) -> None:
        self._transcription_frame = frame
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)

        mic_var = tk.StringVar(value="System default")
        self._variables["microphone"] = mic_var
        mic_combo = ttk.Combobox(frame, textvariable=mic_var, state="readonly", width=42)
        self._widgets["microphone"] = mic_combo
        ttk.Label(frame, text="Microphone:").grid(row=0, column=0, sticky="w", padx=10, pady=8)
        mic_combo.grid(row=0, column=1, sticky="w", padx=10, pady=8)
        ttk.Button(frame, text="Refresh", command=self._refresh_microphones).grid(
            row=0, column=2, sticky="w", padx=10, pady=8
        )

        model_var = tk.StringVar(value="base")
        self._variables["whisper_model"] = model_var
        model_combo = ttk.Combobox(
            frame,
            textvariable=model_var,
            values=["tiny", "base", "small", "medium"],
            state="readonly",
            width=16,
        )
        self._widgets["whisper_model"] = model_combo
        ttk.Label(frame, text="Whisper model:").grid(row=1, column=0, sticky="w", padx=10, pady=8)
        model_combo.grid(row=1, column=1, sticky="w", padx=10, pady=8)
        model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)

        self._model_info_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self._model_info_var, foreground="#444444").grid(
            row=2, column=1, columnspan=2, sticky="w", padx=10
        )

        self._device_var = tk.StringVar(value="Device: detecting...")
        ttk.Label(frame, textvariable=self._device_var, foreground="#444444").grid(
            row=3, column=1, columnspan=2, sticky="w", padx=10
        )

        self._model_note_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self._model_note_var, foreground="#8a6d3b").grid(
            row=4, column=1, columnspan=2, sticky="w", padx=10
        )

        self._refresh_model_info()
        self._refresh_device_label()

    def _on_model_selected(self, _event: Any = None) -> None:
        self._refresh_model_info()
        self._model_note_var.set("Model will load on next dictation")

    def _refresh_model_info(self) -> None:
        """Show the size and the download state of the selected model."""
        model = self._variables["whisper_model"].get()
        sizes: Dict[str, str] = {}
        downloaded = False
        try:
            from . import transcribe

            sizes = dict(transcribe.MODEL_SIZES)
            downloaded = bool(transcribe.model_is_downloaded(model))
        except Exception:
            log.exception("could not read the model information")
        size = sizes.get(model, "?")
        if downloaded:
            self._model_info_var.set("%s - %s - downloaded" % (model, size))
        else:
            self._model_info_var.set("%s - %s - not downloaded yet" % (model, size))

    def _refresh_device_label(self) -> None:
        """Fill in the CPU/CUDA indicator without blocking the UI thread."""
        preference = self._device_preference

        def worker() -> None:
            try:
                from .transcribe import device_label

                label = device_label(preference)
            except Exception:
                log.exception("could not detect the compute device")
                label = "CPU"
            try:
                self._root.after(0, lambda: self._device_var.set("Device: %s (read only)" % label))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # -- cleanup -------------------------------------------------------

    def _build_cleanup(self, frame: ttk.Frame) -> None:
        self._cleanup_frame = frame
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)

        tier_var = tk.StringVar(value=TIER_CHOICES[0][0])
        self._variables["cleanup.mode"] = tier_var
        tier_combo = ttk.Combobox(
            frame,
            textvariable=tier_var,
            values=[label for label, _ in TIER_CHOICES],
            state="readonly",
            width=24,
        )
        self._widgets["cleanup.mode"] = tier_combo
        ttk.Label(frame, text="Cleanup tier:").grid(row=0, column=0, sticky="w", padx=10, pady=6)
        tier_combo.grid(row=0, column=1, sticky="w", padx=10, pady=6)

        for row, (path, label) in enumerate(
            (
                ("cleanup.groq_base_url", "Groq base URL:"),
                ("cleanup.groq_model", "Groq model:"),
                ("cleanup.ollama_base_url", "Ollama base URL:"),
                ("cleanup.ollama_model", "Ollama model:"),
            ),
            start=1,
        ):
            var = tk.StringVar(value="")
            self._variables[path] = var
            entry = ttk.Entry(frame, textvariable=var, width=46)
            self._widgets[path] = entry
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=6)
            entry.grid(row=row, column=1, sticky="w", padx=10, pady=6)

        key_var = tk.StringVar(value="")
        self._api_key_var = key_var
        self._variables["api_key"] = key_var
        key_entry = ttk.Entry(frame, textvariable=key_var, width=46, show="*")
        self._widgets["api_key"] = key_entry
        ttk.Label(frame, text="Groq API key:").grid(row=5, column=0, sticky="w", padx=10, pady=6)
        key_entry.grid(row=5, column=1, sticky="w", padx=10, pady=6)

        show_var = tk.BooleanVar(value=False)
        self._show_key_var = show_var
        show_check = ttk.Checkbutton(
            frame,
            text="Show",
            variable=show_var,
            command=self._toggle_key_visibility,
        )
        self._widgets["show_api_key"] = show_check
        show_check.grid(row=5, column=2, sticky="w", padx=10, pady=6)

        ttk.Label(
            frame,
            text="The API key is stored in the OS keychain, never in the config file.",
            foreground="#666666",
        ).grid(row=6, column=0, columnspan=3, sticky="w", padx=10)

        self._test_result_var = tk.StringVar(value="")
        ttk.Button(frame, text="Test connection", command=self._on_test_connection).grid(
            row=7, column=0, sticky="w", padx=10, pady=8
        )
        ttk.Label(frame, textvariable=self._test_result_var, foreground="#333333").grid(
            row=7, column=1, columnspan=2, sticky="w", padx=10, pady=8
        )

    def _toggle_key_visibility(self) -> None:
        entry = self._widgets.get("api_key")
        if entry is None:
            return
        try:
            entry.configure(show="" if self._show_key_var.get() else "*")
        except tk.TclError:
            pass

    # -- dictionary ----------------------------------------------------

    def _build_dictionary(self, frame: ttk.Frame) -> None:
        self._dictionary_frame = frame
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        tree = ttk.Treeview(frame, columns=("wrong", "correct"), show="headings", height=12)
        tree.heading("wrong", text="Heard as")
        tree.heading("correct", text="Replace with")
        tree.column("wrong", width=220, anchor="w")
        tree.column("correct", width=220, anchor="w")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=(10, 4))
        tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._dict_tree = tree

        editor = ttk.Frame(frame)
        editor.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        editor.columnconfigure(1, weight=1)
        editor.columnconfigure(3, weight=1)

        self._dict_wrong_var = tk.StringVar(value="")
        self._dict_correct_var = tk.StringVar(value="")
        ttk.Label(editor, text="Heard as:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(editor, textvariable=self._dict_wrong_var, width=24).grid(
            row=0, column=1, sticky="w", padx=(0, 10)
        )
        ttk.Label(editor, text="Replace with:").grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Entry(editor, textvariable=self._dict_correct_var, width=24).grid(
            row=0, column=3, sticky="w", padx=(0, 10)
        )

        buttons = ttk.Frame(editor)
        buttons.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(buttons, text="Add", command=self._on_dictionary_add).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Update", command=self._on_dictionary_update).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Delete", command=self._on_dictionary_delete).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Clear", command=self._on_dictionary_clear).pack(side="left")

    def _on_tree_select(self, _event: Any = None) -> None:
        selection = self._selected_row()
        if selection is None:
            return
        wrong, correct = selection
        self._dict_wrong_var.set(wrong)
        self._dict_correct_var.set(correct)

    def _selected_row(self) -> Optional[Tuple[str, str]]:
        selection = self._dict_tree.selection()
        if not selection:
            return None
        values = self._dict_tree.item(selection[0], "values")
        if not values:
            return None
        return str(values[0]), str(values[1])

    def _on_dictionary_add(self) -> None:
        wrong = self._dict_wrong_var.get().strip()
        correct = self._dict_correct_var.get().strip()
        if not wrong or not correct:
            self._log.info("dictionary row ignored: both columns are required")
            return
        self._dict_tree.insert("", "end", values=[wrong, correct])
        self._on_dictionary_clear()
        self._refresh_dirty()

    def _on_dictionary_update(self) -> None:
        selection = self._dict_tree.selection()
        if not selection:
            self._log.info("no dictionary row selected")
            return
        wrong = self._dict_wrong_var.get().strip()
        correct = self._dict_correct_var.get().strip()
        if not wrong or not correct:
            return
        self._dict_tree.item(selection[0], values=[wrong, correct])
        self._on_dictionary_clear()
        self._refresh_dirty()

    def _on_dictionary_delete(self) -> None:
        selection = self._dict_tree.selection()
        if not selection:
            self._log.info("no dictionary row selected")
            return
        self._dict_tree.delete(selection[0])
        self._on_dictionary_clear()
        self._refresh_dirty()

    def _on_dictionary_clear(self) -> None:
        self._dict_wrong_var.set("")
        self._dict_correct_var.set("")
        try:
            self._dict_tree.selection_remove(self._dict_tree.selection())
        except tk.TclError:
            pass

    # -- footer --------------------------------------------------------

    def _build_footer(self, frame: ttk.Frame) -> None:
        frame.pack(fill="x", padx=10, pady=(4, 10))
        frame.columnconfigure(0, weight=1)

        self._stats_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self._stats_var, foreground="#444444").grid(
            row=0, column=0, sticky="w"
        )

        bottom = ttk.Frame(frame)
        bottom.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        self._dirty_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self._dirty_var, foreground="#b06000").pack(side="left")

        self._toast_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self._toast_var, foreground="#1a7f37").pack(side="left", padx=(12, 0))

        ttk.Button(bottom, text="Reload", command=self._on_reload).pack(side="right")
        self.cancel_button = ttk.Button(bottom, text="Cancel", command=self._on_cancel)
        self.cancel_button.pack(side="right", padx=(0, 6))
        self.save_button = ttk.Button(bottom, text="Save", command=self._on_save)
        self.save_button.pack(side="right", padx=(0, 6))

    # ------------------------------------------------------------------
    # Populating the form
    # ------------------------------------------------------------------

    def _refresh_microphones(self) -> None:
        """Rebuild the microphone list, keeping the current selection."""
        current = self._collect().get("microphone")
        self._mic_choices = self._build_mic_choices(current)
        self._widgets["microphone"].configure(values=[label for label, _ in self._mic_choices])
        self._set_microphone(current)

    def _build_mic_choices(self, current: Any) -> List[Tuple[str, Any]]:
        choices: List[Tuple[str, Any]] = [("System default", None)]
        for device in self._input_devices():
            name = str(device.get("name", "")).strip() or "Device %s" % device.get("id")
            label = "%s (default)" % name if device.get("is_default") else name
            choices.append((label, device.get("id")))
        if current is not None and not any(stored == current for _, stored in choices):
            choices.append((str(current), current))
        return choices

    def _set_microphone(self, value: Any) -> None:
        for label, stored in self._mic_choices:
            if stored == value:
                self._variables["microphone"].set(label)
                return
        self._variables["microphone"].set(self._mic_choices[0][0] if self._mic_choices else "")

    def _populate(self, values: Dict[str, Any]) -> None:
        """Fill every field from *values* (a flat dotted-path mapping)."""
        self._variables["hotkey"].set(str(values.get("hotkey", "alt_r")))
        self._variables["whisper_model"].set(str(values.get("whisper_model", "base")))

        preroll = values.get("preroll_ms", 500)
        try:
            preroll = max(0, min(1000, int(preroll)))
        except (TypeError, ValueError):
            preroll = 500
        self._variables["preroll_ms"].set(preroll)
        try:
            self._widgets["preroll_ms"].set(preroll)
        except tk.TclError:
            pass
        self._preroll_label_var.set("%d ms" % preroll)

        self._variables["hud_enabled"].set(bool(values.get("hud_enabled", True)))
        self._variables["live_preview"].set(bool(values.get("live_preview", False)))
        self._variables["start_on_login"].set(bool(values.get("start_on_login", False)))

        self._mic_choices = self._build_mic_choices(values.get("microphone"))
        self._widgets["microphone"].configure(values=[label for label, _ in self._mic_choices])
        self._set_microphone(values.get("microphone"))

        mode = str(values.get("cleanup.mode", "auto"))
        label = TIER_CHOICES[0][0]
        for candidate, stored in TIER_CHOICES:
            if stored == mode:
                label = candidate
                break
        self._variables["cleanup.mode"].set(label)

        for path in (
            "cleanup.groq_base_url",
            "cleanup.groq_model",
            "cleanup.ollama_base_url",
            "cleanup.ollama_model",
        ):
            self._variables[path].set(str(values.get(path, "") or ""))

        self._api_key_var.set(self._baseline_api_key)

        self._dict_tree.delete(*self._dict_tree.get_children())
        for wrong, correct in self._normalize_dictionary(values.get("dictionary")):
            self._dict_tree.insert("", "end", values=[wrong, correct])

        self._refresh_model_info()
        self._model_note_var.set("")

    # ------------------------------------------------------------------
    # Reading the form
    # ------------------------------------------------------------------

    def _collect(self) -> Dict[str, Any]:
        """Return the current form values as a flat dotted-path mapping."""
        values: Dict[str, Any] = {
            "hotkey": self._variables["hotkey"].get(),
            "preroll_ms": int(self._variables["preroll_ms"].get() or 0),
            "hud_enabled": bool(self._variables["hud_enabled"].get()),
            "live_preview": bool(self._variables["live_preview"].get()),
            "start_on_login": bool(self._variables["start_on_login"].get()),
            "whisper_model": self._variables["whisper_model"].get(),
            "cleanup.mode": self._tier_value(),
        }
        for path in (
            "cleanup.groq_base_url",
            "cleanup.groq_model",
            "cleanup.ollama_base_url",
            "cleanup.ollama_model",
        ):
            values[path] = self._variables[path].get()

        label = self._variables["microphone"].get()
        values["microphone"] = None
        for candidate, stored in self._mic_choices:
            if candidate == label:
                values["microphone"] = stored
                break

        rows: List[List[str]] = []
        try:
            children = self._dict_tree.get_children()
        except tk.TclError:
            # The window was closed underneath us.
            children = ()
        for item_id in children:
            try:
                row = self._dict_tree.item(item_id, "values")
            except tk.TclError:
                continue
            if row and len(row) >= 2:
                rows.append([str(row[0]), str(row[1])])
        values["dictionary"] = rows
        return values

    def _tier_value(self) -> str:
        label = self._variables["cleanup.mode"].get()
        for candidate, stored in TIER_CHOICES:
            if candidate == label:
                return stored
        return "auto"

    # ------------------------------------------------------------------
    # Dirty tracking
    # ------------------------------------------------------------------

    def _compute_dirty(self) -> bool:
        current = self._collect()
        for path, value in current.items():
            if self._baseline.get(path) != value:
                return True
        if self._api_key_var.get() != self._baseline_api_key:
            return True
        return False

    def _refresh_dirty(self) -> None:
        """Recompute the dirty flag and update the footer accordingly."""
        try:
            dirty = self._compute_dirty()
        except Exception:
            self._log.exception("could not work out whether the form is dirty")
            dirty = False

        with self._lock:
            self._dirty = dirty

        self._dirty_var.set("Unsaved changes" if dirty else "")
        state = "normal" if dirty else "disabled"
        for button in (self.save_button, self.cancel_button):
            if button is not None:
                try:
                    button.configure(state=state)
                except tk.TclError:
                    pass

    def _wire_traces(self) -> None:
        """Make every field mark the form dirty when it changes."""
        watched = dict(self._variables)
        for name, variable in watched.items():
            try:
                variable.trace_add("write", lambda *_args, _n=name: self._refresh_dirty())
            except Exception:
                self._log.exception("could not watch the field %s", name)

    def _refresh_footer(self) -> None:
        """Refresh the word count lines shown in the footer."""
        try:
            lines = self._controller.status_lines() or []
        except Exception:
            self._log.exception("could not read the word counts")
            lines = []
        self._stats_var.set("\n".join(str(line) for line in lines))

    # ------------------------------------------------------------------
    # Saving / cancelling
    # ------------------------------------------------------------------

    def _on_save(self, _event: Any = None) -> None:
        self._save(close_after=False)

    def _on_save_shortcut(self, _event: Any = None) -> str:
        if self.is_dirty:
            self._save(close_after=False)
        return "break"

    def _save(self, close_after: bool = False) -> None:
        """Apply the form values and clear the dirty flag."""
        values = self._collect()
        changed = {
            path: value
            for path, value in values.items()
            if self._baseline.get(path) != value
        }
        key = self._api_key_var.get()
        key_changed = key != self._baseline_api_key

        try:
            if key_changed:
                self._controller.set_api_key(key)
            self._controller.apply_settings(changed)
        except Exception:
            self._log.exception("could not save the settings")
            self._toast_var.set("Save failed - see the log")
            return

        with self._lock:
            self._baseline = dict(values)
            self._baseline_api_key = key
            self._dirty = False

        self._refresh_dirty()
        self._refresh_footer()
        self._show_toast("Saved")
        if close_after:
            self._destroy()

    def _show_toast(self, text: str) -> None:
        self._toast_var.set(text)
        if self._toast_id is not None:
            try:
                self._root.after_cancel(self._toast_id)
            except Exception:
                pass
            self._toast_id = None
        try:
            self._toast_id = self._root.after(SAVED_TOAST_MS, self._clear_toast)
        except Exception:
            self._toast_id = None

    def _clear_toast(self) -> None:
        self._toast_id = None
        self._toast_var.set("")

    def _on_cancel(self, _event: Any = None) -> None:
        """Revert every field to the values loaded at open and close."""
        self._revert()
        self._destroy()

    def _revert(self) -> None:
        self._populate(dict(self._baseline))
        self._api_key_var.set(self._baseline_api_key)
        self._refresh_dirty()

    def _on_reload(self) -> None:
        """Re-read the configuration from disk and drop the local edits."""
        try:
            snapshot = self._controller.reload_config() or {}
        except Exception:
            self._log.exception("could not reload the configuration")
            self._toast_var.set("Reload failed - see the log")
            return

        defaults = default_config()
        values: Dict[str, Any] = {}
        for path in CONFIG_PATHS:
            values[path] = _dig(snapshot, path, _dig(defaults, path))
        values["microphone"] = self._normalize_microphone(values.get("microphone"))
        values["dictionary"] = self._normalize_dictionary(values.get("dictionary"))

        with self._lock:
            self._baseline = values
        self._populate(values)
        self._refresh_dirty()
        self._refresh_footer()

    # ------------------------------------------------------------------
    # Closing
    # ------------------------------------------------------------------

    def _on_close_requested(self, _event: Any = None) -> str:
        if self.is_dirty:
            choice = self._ask_save_discard_cancel()
            if choice == "cancel":
                return "break"
            if choice == "save":
                self._save(close_after=True)
                return "break"
            self._revert()
        self._destroy()
        return "break"

    def _ask_save_discard_cancel(self) -> str:
        """Ask the user what to do with the unsaved changes.

        Returns ``"save"``, ``"discard"`` or ``"cancel"``.
        """
        parent = self._win
        dialog = tk.Toplevel(parent)
        dialog.title("Unsaved changes")
        dialog.transient(parent)
        dialog.resizable(False, False)
        result: Dict[str, str] = {"choice": "cancel"}

        ttk.Label(
            dialog,
            text="There are unsaved changes.\nWhat would you like to do?",
            justify="left",
            padding=14,
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        def choose(choice: str) -> None:
            result["choice"] = choice
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=(14, 0, 14, 14))
        buttons.grid(row=1, column=0, columnspan=3, sticky="e")
        ttk.Button(buttons, text="Save", command=lambda: choose("save")).pack(side="left", padx=4)
        ttk.Button(buttons, text="Discard", command=lambda: choose("discard")).pack(side="left", padx=4)
        ttk.Button(buttons, text="Cancel", command=lambda: choose("cancel")).pack(side="left", padx=4)

        try:
            dialog.grab_set()
            dialog.focus_set()
        except tk.TclError:
            pass
        try:
            parent.wait_window(dialog)
        except tk.TclError:
            pass
        return result["choice"]

    def _destroy(self) -> None:
        win = self._win
        self._win = None
        if win is None:
            return
        try:
            win.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    def _on_test_connection(self) -> None:
        """Ping the configured endpoint using the values currently typed."""
        mode = self._tier_value()
        providers: List[str] = []
        if mode in ("groq", "auto"):
            providers.append("groq")
        if mode in ("ollama", "auto"):
            providers.append("ollama")

        if not providers:
            self._test_results = ["Rule-based cleanup needs no connection."]
            self._test_result_var.set(self._test_results[0])
            return

        self._test_results = []
        self._test_result_var.set("Testing...")
        for provider in providers:
            params = self._test_params(provider)
            thread = threading.Thread(
                target=self._ping_worker,
                args=(provider, params),
                daemon=True,
            )
            thread.start()

    def _test_params(self, provider: str) -> Dict[str, Any]:
        """Snapshot the fields the probe needs (read on the UI thread)."""
        values = self._collect()
        if provider == "groq":
            return {
                "base": values.get("cleanup.groq_base_url", ""),
                "model": values.get("cleanup.groq_model", ""),
                "key": self._api_key_var.get(),
                "timeout": 5.0,
            }
        return {
            "base": values.get("cleanup.ollama_base_url", ""),
            "model": values.get("cleanup.ollama_model", ""),
            "timeout": 5.0,
        }

    def _ping_worker(self, provider: str, params: Dict[str, Any]) -> None:
        try:
            text = self._ping(provider, params)
        except _PingError as exc:
            text = "%s: %s" % (provider.capitalize(), exc)
        except Exception as exc:  # pragma: no cover - defensive
            self._log.exception("the connection test failed")
            text = "%s: %s" % (provider.capitalize(), exc)
        try:
            self._root.after(0, lambda t=text: self._append_test_result(t))
        except Exception:
            pass

    def _ping(self, provider: str, params: Dict[str, Any]) -> str:
        base = str(params.get("base", "") or "").strip()
        if not base:
            raise _PingError("no base URL configured")
        model = str(params.get("model", "") or "").strip()
        timeout = float(params.get("timeout", 5.0))

        if provider == "groq":
            url = base.rstrip("/") + "/models"
            headers = {}
            key = str(params.get("key", "") or "")
            if key:
                headers["Authorization"] = "Bearer %s" % key
            data = _http_get_json(url, timeout, headers)
            names = [
                str(item.get("id"))
                for item in data.get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
        else:
            url = base.rstrip("/") + "/api/tags"
            data = _http_get_json(url, timeout)
            names = [
                str(item.get("name"))
                for item in data.get("models", [])
                if isinstance(item, dict) and item.get("name")
            ]

        if model and model not in names:
            return "%s: reachable - %d model(s), configured model %r not in the list" % (
                provider.capitalize(),
                len(names),
                model,
            )
        return "%s: reachable - %d model(s): %s" % (
            provider.capitalize(),
            len(names),
            ", ".join(names[:8]) or "none",
        )

    def _append_test_result(self, text: str) -> None:
        self._test_results.append(text)
        self._test_result_var.set("\n".join(self._test_results))
