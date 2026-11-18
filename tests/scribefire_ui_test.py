"""Integration test for the tray icon and the settings window."""
import sys
import threading
import time
import tkinter as tk

REPO = r"C:\Users\Tv-VI\Desktop\minimax - ZERO-FLOW"
sys.path.insert(0, REPO)

from scribefire.settings import SettingsWindow
from scribefire.tray import TrayIcon

failures = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        failures.append(name)


class FakeController:
    def __init__(self):
        self.state = "idle"
        self.muted = False
        self.applied = []
        self.api_key = ""
        self.settings_opened = 0
        self.quit_calls = 0

    def status_text(self):
        return "Ready" if not self.muted else "Muted"

    def toggle_mute(self):
        self.muted = not self.muted

    def open_settings(self):
        self.settings_opened += 1

    def quit(self):
        self.quit_calls += 1

    def config_snapshot(self):
        return {
            "hotkey": "alt_r",
            "preroll_ms": 500,
            "hud_enabled": True,
            "live_preview": False,
            "start_on_login": False,
            "microphone": None,
            "whisper_model": "base",
            "cleanup": {
                "mode": "auto",
                "groq_base_url": "https://api.groq.com/openai/v1",
                "groq_model": "openai/gpt-oss-20b",
                "ollama_base_url": "http://localhost:11434",
                "ollama_model": "llama3.2:3b",
            },
            "dictionary": [["pie torch", "PyTorch"]],
        }

    def apply_settings(self, values):
        self.applied.append(dict(values))

    def get_api_key(self):
        return self.api_key

    def set_api_key(self, key):
        self.api_key = key

    def status_lines(self):
        return ["Words today: 12", "Words total: 340"]

    def reload_config(self):
        return self.config_snapshot()


print("tray")
controller = FakeController()
tray = TrayIcon(controller, no_tray=True)
tray.run()  # headless: must return immediately
check("headless run returns", not tray.running)

menu = tray._build_menu()
check("menu built", menu is not None)
image = tray._build_image("recording")
check("icon image built", image is not None and image.size[0] > 0, str(getattr(image, "size", None)))

tray.update_status("Recording...")
tray.update_state("recording")
check("state updated", tray._state == "recording", tray._state)
tray.update_state("processing")
check("state updated again", tray._state == "processing", tray._state)
tray.update_state("bogus-state")
check("invalid state falls back to idle", tray._state == "idle", tray._state)
check("status updated", tray._status == "Recording...", tray._status)

tray._on_toggle_mute()
check("mute toggled", controller.muted is True)
tray._on_open_settings()
check("settings opened", controller.settings_opened == 1)

print("settings window")
root = tk.Tk()
root.withdraw()
window = SettingsWindow(controller, root=root)
window.show()
root.update()
check("window built", window._win is not None)
check("not dirty when opened", not window.is_dirty)

# Change a field -> the form becomes dirty and Save/Cancel enable.
window._variables["hotkey"].set("ctrl_r")
root.update()
check("dirty after a change", window.is_dirty, "is_dirty=%s" % window.is_dirty)

window._save()
root.update()
check("save applied the change", any("hotkey" in a for a in controller.applied), str(controller.applied))
check("clean after save", not window.is_dirty)
check("footer shows the word counts", "Words today" in window._stats_var.get(), window._stats_var.get())

# The dictionary round-trips through the controller (before the window closes).
rows = window._collect().get("dictionary")
check("dictionary rows", rows and rows[0][1] == "PyTorch", str(rows))

# Change then cancel -> the value reverts to the last saved baseline.
window._variables["hotkey"].set("shift_r")
root.update()
check("dirty again", window.is_dirty)
window._on_cancel()
root.update()
check("cancel reverted the field", window._variables["hotkey"].get() == "ctrl_r", window._variables["hotkey"].get())

window.close()
root.update()
root.destroy()

print("")
if failures:
    print("FAILURES: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("all checks passed")
