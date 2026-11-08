r"""Read-only recon pass 7: extract UTF-16LE CJK strings from the raw bundle and
write the matches to a UTF-8 file (console encoding mangles CJK).

Usage: python scripts/recon/cjk16.py [pattern] [outfile]
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

DATA = open(paths.ensure_bundle(), "rb").read()
pat = sys.argv[1] if len(sys.argv) > 1 else r"名额|剩余|已满|报名|候补|人数|定员|满员|上限|报名费|参赛"
out = sys.argv[2] if len(sys.argv) > 2 else paths.CJK_HITS_PATH

UNIT = rb"(?:[\x00-\xff][\x4e-\x9f]|[\x00-\x7f]\x00)"
RUN = re.compile(UNIT + rb"{2,80}")

uniq = []
seen = set()
for m in RUN.finditer(DATA):
    raw = m.group(0)
    if len(raw) % 2:
        raw = raw[:-1]
    try:
        s = raw.decode("utf-16-le")
    except UnicodeDecodeError:
        continue
    s = s.strip()
    if len(s) < 2 or s in seen:
        continue
    seen.add(s)
    uniq.append(s)

rx = re.compile(pat)
hits = [s for s in uniq if rx.search(s)]

with open(out, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("### pattern: %s\n### unique utf16 runs: %d, matching: %d\n\n" % (pat, len(uniq), len(hits)))
    for s in hits:
        fh.write(s.replace("\n", " ") + "\n")

print("unique utf16 runs: %d, matching %d -> %s" % (len(uniq), len(hits), out))

# also persist every short-to-medium CJK run for later grepping
with open(paths.CJK_ALL_PATH, "w", encoding="utf-8", newline="\n") as fh:
    n = 0
    for s in uniq:
        if re.search(r"[\u4e00-\u9fff]", s) and 2 <= len(s) <= 80 and not re.search(r"[\ufffd]", s):
            fh.write(s.replace("\n", " ") + "\n")
            n += 1
print("cjk_all.txt lines: %d" % n)
