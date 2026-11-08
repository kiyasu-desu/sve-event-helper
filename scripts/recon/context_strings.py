"""Read-only recon pass 3: keyword-context windows over the strings dump.

Hermes packs many strings into single runs, so print +-N chars around each
keyword occurrence instead of whole lines.

Usage: python scripts/recon/context_strings.py [strings.txt] [keyword ...]
"""
import os
import re
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

blob = paths.load_blob()

KEYS = sys.argv[2:] or [
    "api/v1", "api-sve", "/urs/", "tournament", "signup", "Signup", "quota",
    "remain", "Remain", "capacity", "enroll", "sign_up", "sign_up_time",
    "member", "joined", "max_num", "limit_num", "apply",
]

W = 90
for k in KEYS:
    hits = [m.start() for m in re.finditer(re.escape(k), blob)]
    print("\n##### %s  (%d hits)" % (k, len(hits)))
    seen = collections.OrderedDict()
    for i in hits:
        snippet = blob[max(0, i - W): i + W]
        snippet = re.sub(r"\s+", " ", snippet)
        # de-duplicate on the interesting part
        key = snippet[W // 2: W // 2 + 40]
        if key in seen:
            continue
        seen[key] = snippet
        if len(seen) > 14:
            break
    for s in seen.values():
        print("  ...%s..." % s)
