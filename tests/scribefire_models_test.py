"""Verify the Whisper and Vosk paths really run (downloads models on first use)."""
import math
import sys
import time

REPO = r"C:\Users\Tv-VI\Desktop\minimax - ZERO-FLOW"
sys.path.insert(0, REPO)

import numpy as np

from scribefire.preview import PreviewEngine
from scribefire.transcribe import Transcriber

failures = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        failures.append(name)


def tone(seconds=1.0, freq=220.0, rate=16000):
    t = np.arange(int(seconds * rate)) / rate
    return (0.2 * np.sin(2 * math.pi * freq * t)).astype(np.float32)


print("whisper (tiny model)")
tr = Transcriber(model_name="tiny")
started = time.time()
ok = tr.load(progress=lambda m: print("    %s" % m))
check("model loaded", ok, "%.1fs" % (time.time() - started))
if ok:
    check("device detected", tr.device in ("cpu", "cuda"), tr.device)
    out = tr.transcribe(tone(1.0))
    check("transcription returns text", isinstance(out, str), repr(out))
    out2 = tr.transcribe(tone(1.0, freq=440.0))
    check("second transcription works", isinstance(out2, str), repr(out2))

print("vosk preview")
partials = []
finals = []
engine = PreviewEngine(on_partial=partials.append, on_final=finals.append)
started = time.time()
ok = engine.load()
check("vosk model loaded", ok, "%.1fs" % (time.time() - started))
if ok:
    check("engine started", engine.start())
    engine.begin()
    chunk = tone(0.2, freq=300.0)
    for _ in range(10):
        engine.feed(chunk)
        time.sleep(0.02)
    final = engine.stop()
    check("final result is a string", isinstance(final, str), repr(final))
    check("no crash from the worker", True)
    engine.close()

print("")
if failures:
    print("FAILURES: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("all checks passed")
