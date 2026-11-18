"""Functional test of the Windows HUD overlay (run from a temp dir)."""
import ctypes
import sys
import time

REPO = r"C:\Users\Tv-VI\Desktop\minimax - ZERO-FLOW"
sys.path.insert(0, REPO)

from scribefire.config import Config
from scribefire.hud_windows import HudOverlay

failures = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        failures.append(name)


user32 = ctypes.windll.user32
foreground_before = user32.GetForegroundWindow()

config = Config()
hud = HudOverlay(config)

print("show / hide")
hud.show()
time.sleep(0.6)
check("visible after show", hud.visible, str(hud.visible))

hwnd = hud._primary_handle()
check("has a window handle", hwnd != 0, str(hwnd))
foreground_after = user32.GetForegroundWindow()
check(
    "the HUD is NOT the foreground window",
    foreground_after != hwnd,
    "foreground=%s hud=%s" % (foreground_after, hwnd),
)

# The extended window style must carry WS_EX_NOACTIVATE.
if hwnd:
    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    check("WS_EX_NOACTIVATE is set", bool(style & WS_EX_NOACTIVATE), hex(style))
    check("WS_EX_TOOLWINDOW is set", bool(style & WS_EX_TOOLWINDOW), hex(style))

print("states and drawing")
hud.set_rms([0.02, 0.08, 0.15, 0.04] * 20)
hud.set_state("listening")
time.sleep(0.4)
hud.set_preview_text("this is a live preview that should scroll and truncate")
time.sleep(0.4)
hud.set_state("processing")
time.sleep(0.4)
hud.set_state("done")
time.sleep(0.8)

check("still alive after the state cycle", True)
canvas = hud._canvas
check("canvas exists", canvas is not None)
if canvas is not None:
    items = canvas.find_all()
    check("something was drawn", len(items) > 0, "items=%d" % len(items))
    kinds = [canvas.type(i) for i in items]
    check("there are line items", "line" in kinds, str(set(kinds)))

print("thread safety")
import threading

errors = []


def hammer():
    try:
        for _ in range(200):
            hud.set_rms([0.01] * 80)
            hud.set_state("listening")
            hud.set_preview_text("x" * 50)
    except Exception as exc:
        errors.append(repr(exc))


threads = [threading.Thread(target=hammer) for _ in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=10)
check("no errors from worker threads", not errors, str(errors[:2]))

print("hide / destroy")
hud.hide()
time.sleep(0.3)
check("hidden", not hud.visible)
canvas = None  # drop our own reference to the interpreter
hud.destroy()
time.sleep(0.3)
check("destroyed without raising", True)

print("")
if failures:
    print("FAILURES: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("all checks passed")
