"""Verify that dictated text actually lands in a focused text field."""
import sys
import threading
import time
import tkinter as tk

REPO = r"C:\Users\Tv-VI\Desktop\minimax - ZERO-FLOW"
sys.path.insert(0, REPO)

from scribefire.config import Config
from scribefire.inject import TextInjector, read_clipboard

failures = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        failures.append(name)


import ctypes

user32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32


def force_foreground(hwnd):
    """Windows blocks foreground changes from a background process."""
    fg = user32.GetForegroundWindow()
    cur = k32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    user32.AttachThreadInput(cur, fg_thread, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetActiveWindow(hwnd)
    user32.AttachThreadInput(cur, fg_thread, False)


root = tk.Tk()
root.title("injection target")
entry = tk.Entry(root, width=60)
entry.pack(padx=20, pady=40)
root.attributes("-topmost", True)
root.update()

hwnd = int(root.winfo_id())
parent = user32.GetParent(hwnd) or hwnd
force_foreground(parent)
root.lift()
root.focus_force()
root.update()
time.sleep(0.3)
root.update()
entry.focus_force()
root.update()

# Put something in the clipboard first so we can check it is restored.
read = read_clipboard()
print("  clipboard before: %r" % (read,))

config = Config()
config.set("inject.paste_delay_ms", 120)
injector = TextInjector(config)

text = "the quick brown fox jumps over the lazy dog"
print("  fg before inject: %s (target %s)" % (user32.GetForegroundWindow(), parent))

# A real target application runs its own event loop, so the paste event is
# handled while the injection is still in flight.  Do the same here.
result = {}


def run_inject():
    result["ok"] = injector.inject(text)


worker = threading.Thread(target=run_inject)
worker.start()
deadline = time.time() + 3
while worker.is_alive() and time.time() < deadline:
    root.update()
    time.sleep(0.01)
worker.join(timeout=3)
root.update()

ok = result.get("ok", False)
check("inject reported success", ok)
print("  fg after inject:  %s" % user32.GetForegroundWindow())

time.sleep(0.6)
root.update()
value = entry.get()
print("  entry now holds: %r" % value)
check("text landed in the focused field", text in value, repr(value))

after = read_clipboard()
check("clipboard restored", after == read, "before=%r after=%r" % (read, after))

root.destroy()

print("")
if failures:
    print("FAILURES: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("all checks passed")
