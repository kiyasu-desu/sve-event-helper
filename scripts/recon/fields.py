"""Read-only recon pass 4: list snake_case field names in the bundle strings.

Usage: python scripts/recon/fields.py [pattern] [max]
"""
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

blob = paths.load_blob()
pat = sys.argv[1] if len(sys.argv) > 1 else r"(player|num|count|limit|quota|sign|join|max|min|total|status|time|city|event|match|tournament|fee|price|seat|rank)"
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 300

ids = re.findall(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", blob)
c = collections.Counter(ids)
print("distinct snake_case ids: %d" % len(c))
pick = sorted(k for k in c if re.search(pat, k))
print("matching %r: %d" % (pat, len(pick)))
for k in pick[:limit]:
    print("  %-45s x%d" % (k, c[k]))
