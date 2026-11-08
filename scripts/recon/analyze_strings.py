"""Read-only recon pass 2: find tournament / registration / signing strings in
the Hermes bundle strings dump, and try to decode base64-ish path segments.

Usage: python scripts/recon/analyze_strings.py [strings.txt] [analysis.txt]
"""
import base64
import os
import re
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

src = sys.argv[1] if len(sys.argv) > 1 else paths.STRINGS_PATH
dst = sys.argv[2] if len(sys.argv) > 2 else paths.ANALYSIS_PATH

lines = [l.rstrip("\n") for l in open(src, encoding="utf-8", errors="replace")]
print("loaded %d strings" % len(lines))

GROUPS = {
    "TOURNAMENT": ["tournament", "match_info", "matchinfo", "match_detail"],
    "REGISTER": ["enroll", "sign_up", "signup", "signUp", "apply", "regist", "baoming", "报名"],
    "QUOTA": ["quota", "remain", "left_num", "leftNum", "capacity", "limit_num", "limitNum",
              "seat", "sold", "full", "be_full", "is_full", "vacan", "yin_e", "名额"],
    "URS": ["urs/", "/urs", "ursinfo", "user_info", "userinfo"],
    "SIGN": ["hmac", "sha256", "signature", "sign=", "nonce", "x-sign", "appkey", "app_key",
             "secret", "X-", "Authorization", "token"],
    "APIROOT": ["api-sve", "nie.netease.com", "betasystem", "shadowverse-evolve"],
    "ROUTE": ["pages/card", "pages/game", "pages/tournament", "pages/match", "pages/city"],
    "FIELD": ["eventId", "matchId", "tournamentId", "activityId", "cityCode", "regionCode",
              "matchStatus", "status", "pageSize", "pageNum"],
}

hits = collections.OrderedDict((k, []) for k in GROUPS)
for l in lines:
    low = l.lower()
    for name, keys in GROUPS.items():
        if any(k.lower() in low for k in keys):
            hits[name].append(l)

out = open(dst, "w", encoding="utf-8", newline="\n")
for name, items in hits.items():
    seen = []
    for it in items:
        if it not in seen:
            seen.append(it)
    out.write("\n===== %s (%d unique / %d raw) =====\n" % (name, len(seen), len(items)))
    for it in seen[:400]:
        out.write(it[:300] + "\n")

# try to decode jwt-like / base64 path segments for signing hints
b64 = set()
for l in lines:
    for m in re.findall(r"[A-Za-z0-9+/]{24,}={0,2}", l):
        b64.add(m)
decoded = []
for m in list(b64):
    try:
        raw = base64.b64decode(m + "=" * (-len(m) % 4))
    except Exception:
        continue
    txt = raw.decode("utf-8", errors="ignore")
    printable = sum(1 for c in txt if 32 <= ord(c) < 127)
    if len(txt) >= 8 and printable / max(len(txt), 1) > 0.9:
        decoded.append((m, txt))

out.write("\n===== base64-decoded candidates (%d) =====\n" % len(decoded))
for m, txt in decoded[:200]:
    out.write("%s\n   -> %s\n" % (m[:70], txt[:200]))

out.close()
print("wrote %s" % dst)
for name, items in hits.items():
    print("  %-12s %d raw" % (name, len(items)))
print("  base64 decoded candidates: %d" % len(decoded))
