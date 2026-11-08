r"""Read-only recon pass 8: list camelCase / PascalCase identifiers that look like
network, signing, crypto, storage or session helpers.

Usage: python scripts/recon/camel.py [pattern] [limit]
"""
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

blob = paths.load_blob()
pat = sys.argv[1] if len(sys.argv) > 1 else r"(Sign|sign|Crypt|crypt|Token|token|Session|session|Header|header|Http|http|Api|api|Request|request|Encrypt|encrypt|Decrypt|Aes|AES|Hmac|hmac|Md5|md5|Sha|sha)"
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 400

ids = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]{3,48}", blob)
c = collections.Counter(ids)
print("distinct identifiers: %d" % len(c))
pick = sorted(k for k in c if re.search(pat, k) and re.search(r"[A-Z]", k))
print("matching %r: %d" % (pat, len(pick)))
for k in pick[:limit]:
    print("  %-46s x%d" % (k, c[k]))
