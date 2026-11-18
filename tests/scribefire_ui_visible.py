"""Does the UI actually appear when it is triggered?"""
import sys
import tempfile
import time
from pathlib import Path

REPO = r"C:\Users\Tv-VI\Desktop\minimax - ZERO-FLOW"
sys.path.insert(0, REPO)

from scribefire.config import Config
from scribefire.main import ScribefireApp

failures = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        failures.append(name)


tmp = Path(tempfile.mkdtemp()) / "config.json"
config = Config(path=tmp)
app = ScribefireApp(config, no_tray=True)

print("startup (no tray, no real mic needed)")
app.ui.start()
app._start_hud()
time.sleep(0.5)

print("settings window")
app.open_settings()
deadline = time.time() + 5
found = False
while time.time() < deadline and not found:
    time.sleep(0.1)
    root = app.ui.root
    if root is not None:
        children = root.winfo_children()
        found = any(str(c).endswith("toplevel") for c in children)
check("a settings window was created", found)
if app.settings is not None:
    check("the window is a real toplevel", app.settings._win is not None)
    check("the window is visible", bool(app.settings._win.winfo_viewable()))
    print("  window title: %r" % app.settings._win.title())

print("HUD overlay")
app.on_hotkey_press()
time.sleep(1.0)
check("the HUD reports itself visible", app.hud is not None and app.hud.visible)
if app.hud is not None:
    hwnd = app.hud._primary_handle()
    check("the HUD has a window handle", hwnd != 0, str(hwnd))
app.on_hotkey_release()
time.sleep(0.5)

app.ui.stop()
print("")
if failures:
    print("FAILURES: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("all checks passed")
