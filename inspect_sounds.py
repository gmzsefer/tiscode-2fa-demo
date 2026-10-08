"""Measure the layout of every TISCODE file in static/ and find duplicate files.

Usage:
    python3 inspect_sounds.py

For each WAV file it prints where the opening marker (OM) starts and ends and where
the infocore starts, using the peak level in 10 ms windows. These measurements are the
source of INFOCORE_START in app.py. Byte-identical files (same md5) are listed at the
end; they are the source of DUPLICATES in app.py.
"""
import hashlib, os, wave
from collections import defaultdict
import numpy as np

FOLDER = "static"
ON = 0.01      # level that counts as sound (1% of full scale)
OFF = 0.001    # level that counts as silence


def layout(path):
    with wave.open(path) as w:
        sw, ch, sr = w.getsampwidth(), w.getnchannels(), w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype={2: "<i2", 4: "<i4"}[sw])
    a = a.reshape(-1, ch)[:, 0].astype(float) / 2 ** (8 * sw - 1)
    hop = sr // 100                                    # 10 ms windows
    env = np.array([np.abs(a[i * hop:(i + 1) * hop]).max() for i in range(len(a) // hop)])
    loud = np.where(env > ON)[0]
    om_start = om_end = loud[0]
    while env[om_end] > OFF:                           # OM ends at the first silent window
        om_end += 1
    core_start = om_end + np.argmax(env[om_end:] > ON)
    return len(a) / sr, om_start / 100, om_end / 100, core_start / 100, loud[-1] / 100


def key(name):
    head = name[:-4].split("-")[0].split("_")[0]
    return (int(head) if head.isdigit() else 999, name)


files = sorted((f for f in os.listdir(FOLDER) if f.endswith(".wav")), key=key)
print(f"{'file':<12}{'length':>8}{'OM start':>10}{'OM end':>8}{'infocore':>10}{'last sound':>12}")
for f in files:
    total, s, e, c, last = layout(os.path.join(FOLDER, f))
    print(f"{f:<12}{total:>7.2f}s{s:>9.2f}s{e:>7.2f}s{c:>9.2f}s{last:>11.2f}s")

groups = defaultdict(list)
for f in files:
    groups[hashlib.md5(open(os.path.join(FOLDER, f), "rb").read()).hexdigest()].append(f)
dups = [g for g in groups.values() if len(g) > 1]
print("\nIdentical files:", ", ".join(" = ".join(g) for g in dups) if dups else "none")
