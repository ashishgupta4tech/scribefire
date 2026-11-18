"""End-to-end test of the Scribefire state machine with stubbed hardware."""
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = r"C:\Users\Tv-VI\Desktop\minimax - ZERO-FLOW"
sys.path.insert(0, REPO)

import numpy as np

from scribefire.config import Config
from scribefire.main import ScribefireApp

failures = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        failures.append(name)


class StubUi:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def register_settings_window(self, window):
        pass


class FakeRecorder:
    def __init__(self):
        self.preroll_ms = 500
        self.calls = []

    def begin_recording(self):
        self.calls.append("begin")

    def end_recording(self):
        self.calls.append("end")
        return np.zeros(16000, dtype=np.float32)

    @property
    def rms_samples(self):
        return [0.01] * 80


class FakeTranscriber:
    def __init__(self):
        self.is_loaded = True
        self.calls = []

    def load(self, progress=None):
        return True

    def transcribe(self, audio, language=None):
        self.calls.append(audio)
        return "um so i think that pie torch is the the best framework"


class FakeInjector:
    def __init__(self):
        self.pasted = []

    def inject(self, text):
        self.pasted.append(text)
        return True


class FakeHud:
    def __init__(self):
        self.events = []

    def show(self):
        self.events.append("show")

    def hide(self):
        self.events.append("hide")

    def set_state(self, state):
        self.events.append("state:%s" % state)

    def set_rms(self, buf):
        self.events.append("rms")

    def set_preview_text(self, text):
        self.events.append("preview")

    def clear_preview(self):
        self.events.append("clear")


tmp = Path(tempfile.mkdtemp()) / "config.json"
config = Config(path=tmp)
config.set("cleanup.mode", "rule")  # no network in the test
app = ScribefireApp(config, no_tray=True)
app.ui = StubUi()
app.recorder = FakeRecorder()
app.transcriber = FakeTranscriber()
app.injector = FakeInjector()
app.hud = FakeHud()
app.dictionary.set_pairs([["pie torch", "PyTorch"]])
app._start_worker()

print("state machine")
check("starts idle", app.state == "idle", app.state)

app.on_hotkey_press()
check("press -> recording", app.state == "recording", app.state)
check("recorder began", app.recorder.calls == ["begin"], str(app.recorder.calls))
check("hud shown", "show" in app.hud.events, str(app.hud.events))
check("hud listening", "state:listening" in app.hud.events, str(app.hud.events))

app.on_hotkey_release()
check("release -> processing", app.state == "processing", app.state)
check("hud processing", "state:processing" in app.hud.events, str(app.hud.events))

deadline = time.time() + 5
while time.time() < deadline and app.state != "idle":
    time.sleep(0.05)

check("returns to idle", app.state == "idle", app.state)
check("hud done", "state:done" in app.hud.events, str(app.hud.events))
check("exactly one paste", len(app.injector.pasted) == 1, str(app.injector.pasted))

pasted = app.injector.pasted[0] if app.injector.pasted else ""
print("  pasted: %r" % pasted)
check("filler removed", " um " not in pasted.lower(), pasted)
check("repeat collapsed", "the the" not in pasted.lower(), pasted)
check("dictionary applied", "PyTorch" in pasted, pasted)
check("capitalised", pasted[:1].isupper(), pasted)
check("terminal punctuation", pasted.endswith("."), pasted)

print("short recording is discarded")
app.recorder.end_recording = lambda: None
app.on_hotkey_press()
app.on_hotkey_release()
time.sleep(0.4)
check("nothing pasted for a tap", len(app.injector.pasted) == 1, str(app.injector.pasted))
check("back to idle", app.state == "idle", app.state)

print("word counter")
app.config.add_words(len(pasted.split()))
check("words counted", app.config.stats["words_today"] > 0, str(app.config.stats))

print("")
if failures:
    print("FAILURES: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("all checks passed")
