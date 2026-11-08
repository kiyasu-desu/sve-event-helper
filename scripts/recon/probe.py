r"""Read-only recon pass 5: dump a small window around each occurrence of a
keyword, plus every /api/... path found in the bundle.

Usage: python scripts/recon/probe.py <keyword> [before] [after] [max_hits]
       python scripts/recon/probe.py --paths
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

blob = paths.load_blob()

if len(sys.argv) > 1 and sys.argv[1] == "--paths":
    paths = sorted(set(re.findall(r"/api/v[0-9][A-Za-z0-9_/\-]{0,60}", blob)))
    print("=== /api/... paths (%d) ===" % len(paths))
    for p in paths:
        print("  " + p)
    bases = sorted(set(re.findall(r"https?://[A-Za-z0-9.\-]+\.(?:netease|ease)\.com[A-Za-z0-9/_\-]{0,60}", blob)))
    print("\n=== netease/ease hosts+path (%d) ===" % len(bases))
    for b in bases:
        print("  " + b)
    sys.exit(0)

key = sys.argv[1]
before = int(sys.argv[2]) if len(sys.argv) > 2 else 60
after = int(sys.argv[3]) if len(sys.argv) > 3 else 220
maxhits = int(sys.argv[4]) if len(sys.argv) > 4 else 10

hits = [m.start() for m in re.finditer(re.escape(key), blob)]
print("### %s : %d hits" % (key, len(hits)))
for i in hits[:maxhits]:
    print("  [%d] %s" % (i, blob[max(0, i - before): i + after]))
