"""Smoke test for the pure-python Scribefire modules (run from a temp dir)."""
import math
import os
import sys
import tempfile

REPO = r"C:\Users\Tv-VI\Desktop\minimax - ZERO-FLOW"
sys.path.insert(0, REPO)

import numpy as np

from scribefire import cleanup, config as cfgmod, dictionary, hud_common
from scribefire.audio import StreamingResampler

failures = []


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        failures.append(name)


print("config")
tmpdir = tempfile.mkdtemp()
path = os.path.join(tmpdir, "config.json")
c = cfgmod.Config(path=__import__("pathlib").Path(path))
check("defaults merge", c.hotkey == "alt_r" and c.preroll_ms == 500)
c.set("cleanup.groq_model", "custom-model")
check("dotted set", c.get("cleanup.groq_model") == "custom-model")
c.save()
reloaded = cfgmod.Config.load(__import__("pathlib").Path(path))
check("round trip", reloaded.get("cleanup.groq_model") == "custom-model")
check("unknown key default", c.get("nope.nope", "fallback") == "fallback")
c.add_words(5)
c.add_words(3)
check("word stats", c.stats["words_today"] == 8 and c.stats["words_total"] == 8)
check("hotkey resolve", cfgmod.resolve_hotkey("alt_r") in ("alt_r", "alt"))

print("hud_common")
silent = [0.0] * 80
points = hud_common.build_wave_points(silent, 140, 44)
ys = [p[1] for p in points]
check("silence is flat", max(ys) - min(ys) < 0.001, "spread=%s" % (max(ys) - min(ys)))
check("silence thickness is min", abs(points[0][2] - hud_common.WAVE_THICKNESS_MIN) < 1e-6)
loud = [0.4] * 80
loud_points = hud_common.build_wave_points(loud, 140, 44)
offset = abs(loud_points[-1][1] - 22.0)
check("loud is displaced from centre", offset > 15, "offset=%s" % offset)
varying = [0.03 if (i % 8) < 4 else 0.12 for i in range(80)]
var_points = hud_common.build_wave_points(varying, 140, 44)
spread = max(p[1] for p in var_points) - min(p[1] for p in var_points)
check("varying speech makes a wave", spread > 8, "spread=%s" % spread)
check("loud is thicker", loud_points[-1][2] > points[-1][2])
check("newest sample at right edge", abs(loud_points[-1][0] - 140) < 1e-6)
nib = hud_common.nib_position(loud, 140, 44)
check("nib near right edge", abs(nib[0] - (140 - hud_common.NIB_MARGIN)) < 1e-6)
measure = lambda s: len(s) * 7.0
trunc = hud_common.truncate_from_left("hello world this is a long sentence", measure, 100)
check("truncates from the left", trunc.startswith("…") and "sentence" in trunc, repr(trunc))
check("keeps short text", hud_common.truncate_from_left("hi", measure, 100) == "hi")
check("pulse range", 0.4 <= hud_common.pulse_opacity(0.3) <= 1.0)
check("fade to zero", hud_common.fade_opacity(10.0) == 0.0)

print("dictionary")
d = dictionary.PersonalDictionary([["pie torch", "PyTorch"], ["flow", "zero flow"], ["cloud", "Claude"]])
out, fired = d.apply("i use pie torch and cloud every day")
check("replaces multiword", "PyTorch" in out, out)
check("replaces single", "Claude" in out, out)
out2, _ = d.apply("the flower blooms")
check("no substring corruption", out2 == "the flower blooms", out2)
out3, _ = d.apply("PIE TORCH rules")
check("case-insensitive match", "PyTorch" in out3, out3)
out4, _ = d.apply("pie torch")
check("respects replacement casing", out4 == "PyTorch", out4)

print("cleanup (rule tier)")
out = cleanup.rule_based_cleanup("um so i think that the the meeting is um at three")
check("drops fillers", "um" not in out.lower(), out)
check("collapses repeats", "the the" not in out.lower(), out)
check("capitalises", out[0].isupper(), out)
check("terminal punctuation", out.endswith("."), out)
out = cleanup.rule_based_cleanup("let's meet at 3 on friday. oh no let's meet at 5 on wednesday")
check("drops correction phrase", "oh no" not in out.lower(), out)
check("keeps corrected text", "5 on wednesday" in out.lower(), out)
out = cleanup.rule_based_cleanup("hello , world . how are you")
check("fixes spacing before punctuation", " ," not in out and " ." not in out, out)
check("keeps space after punctuation", "Hello, world." in out, out)

print("audio resampler")
res = StreamingResampler(48000, 16000)
t = np.arange(4800) / 48000.0
tone = (0.5 * np.sin(2 * math.pi * 440 * t)).astype(np.float32)
out = res.process(tone)
# The streaming FIR needs a few future samples, so the very end of the signal
# is still in the filter when the input stops: ~(numtaps-1)/ratio samples.
expected = 1600 - (res._numtaps - 1) // 3
check("48k -> 16k length", abs(out.size - expected) <= 2, "got %d expected ~%d" % (out.size, expected))
check("amplitude preserved", 0.4 < float(np.max(np.abs(out))) < 0.6, str(float(np.max(np.abs(out)))))
res2 = StreamingResampler(48000, 16000)
chunks = [tone[i:i + 480] for i in range(0, 4800, 480)]
whole = np.concatenate([res2.process(c) for c in chunks])
res3 = StreamingResampler(48000, 16000)
single = res3.process(tone)
diff = float(np.max(np.abs(whole[:single.size] - single)))
check("chunked == single-shot", diff < 1e-4, "max diff %s" % diff)

print("")
if failures:
    print("FAILURES: %d -> %s" % (len(failures), failures))
    sys.exit(1)
print("all checks passed")
