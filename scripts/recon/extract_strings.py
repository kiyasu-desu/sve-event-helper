"""Read-only recon: pull printable ASCII strings out of the Hermes bundle and
group the interesting ones (URLs, api paths, quota/registration field names).

Usage: python scripts/recon/extract_strings.py [bundle_path] [out_strings]
       (defaults: apk/assets bundle extracted into recon/, output recon/strings.txt)
"""
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

bundle = sys.argv[1] if len(sys.argv) > 1 else paths.ensure_bundle()
outfile = sys.argv[2] if len(sys.argv) > 2 else paths.STRINGS_PATH

data = open(bundle, "rb").read()
version = int.from_bytes(data[8:12], "little")
print("magic=%08x hermes_version=%d size=%d" % (int.from_bytes(data[:4], "little"), version, len(data)))

raw = re.findall(rb"[\x20-\x7e]{5,600}", data)
strings = [x.decode("ascii") for x in raw]
print("ascii runs: %d" % len(strings))

with open(outfile, "w", encoding="utf-8", newline="\n") as fh:
    for s in strings:
        fh.write(s + "\n")
print("wrote %s" % outfile)

URL_RE = re.compile(r"https?://[^\s\"'\\,;)\]<>]{4,160}")
PATH_RE = re.compile(r"/[A-Za-z0-9_\-]*(?:api|v1|v2|v3|sve|event|match|sign|enroll|register)[A-Za-z0-9_\-/]{0,70}")

urls = collections.Counter()
paths = collections.Counter()
hosts = collections.Counter()
for s in strings:
    for m in URL_RE.findall(s):
        urls[m] += 1
        h = re.match(r"https?://([^/:]+)", m)
        if h:
            hosts[h.group(1)] += 1
    for m in PATH_RE.findall(s):
        paths[m] += 1

print("\n=== hosts (%d) ===" % len(hosts))
for h, c in hosts.most_common(60):
    print("  %-55s x%d" % (h, c))

print("\n=== distinct urls (%d) ===" % len(urls))
for u, c in urls.most_common(120):
    print("  %-110s x%d" % (u[:110], c))

print("\n=== api-ish paths (%d) ===" % len(paths))
for p, c in paths.most_common(120):
    print("  %-80s x%d" % (p[:80], c))

KEYWORDS = [
    "quota", "remain", "remainNum", "left", "capacity", "limit",
    "enroll", "apply", "signUp", "signup", "registration", "regist",
    "cancel", "seat", "full", "soldOut", "sold_out",
    "eventId", "matchId", "activityId", "tournamentId",
    "accessToken", "access_token", "refreshToken", "authorization", "Authorization",
    "sign", "nonce", "timestamp", "deviceId", "device_id", "channel",
    "api", "baseUrl", "BASE_URL", "host",
]
hits = collections.Counter()
sample = collections.defaultdict(list)
for s in strings:
    low = s.lower()
    for k in KEYWORDS:
        if k.lower() in low:
            hits[k] += 1
            if len(sample[k]) < 12 and 5 <= len(s) <= 160:
                sample[k].append(s)

print("\n=== keyword hits ===")
for k, c in hits.most_common():
    print("  %-16s x%d" % (k, c))
    for ex in sample[k]:
        print("        | %s" % ex[:150])
