r"""Read-only recon pass 9: decode AndroidManifest.xml from the APK and report
the package identity plus any certificate-pinning / network-security hints.

Usage: python scripts/recon/manifest.py [apk_path]
"""
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

APK = sys.argv[1] if len(sys.argv) > 1 else paths.APK_PATH
raw = zipfile.ZipFile(APK).read("AndroidManifest.xml")

# Binary XML: string pool then element chunks. For recon we do not need a full
# parser - printable UTF-16 strings plus the raw byte scan answer our questions.
pool = re.findall(rb"(?:[\x20-\x7e]\x00){3,}", raw)
strings = [s.decode("utf-16-le", "ignore") for s in pool]
uniq, seen = [], set()
for s in strings:
    if s not in seen:
        seen.add(s)
        uniq.append(s)

print("manifest size: %d bytes, %d unique strings" % (len(raw), len(uniq)))

INTERESTING = re.compile(
    r"(network_security|networkSecurity|certificate|pin|pin-set|trust-anchors|"
    r"cleartext|debug-overrides|user|cacert|profileable|debuggable|backup|"
    r"allowBackup|extractNativeLibs|usesCleartextTraffic)", re.I)

print("\n=== identity / structural strings ===")
for s in uniq:
    if re.search(r"^(com\.|android\.permission|android\.intent)", s) and len(s) < 90:
        print("  " + s)

print("\n=== network-security / security relevant strings ===")
for s in uniq:
    if INTERESTING.search(s):
        print("  " + s)

print("\n=== raw 'pin' / 'cacert' occurrences in manifest bytes ===")
for kw in (b"pin", b"cacert", b"network_security_config", b"networkSecurityConfig"):
    hits = [m.start() for m in re.finditer(re.escape(kw), raw, re.I)]
    print("  %-28s %d" % (kw.decode(), len(hits)))

# does the apk ship a network security config xml at all?
names = zipfile.ZipFile(APK).namelist()
nsc = [n for n in names if "network" in n.lower() and n.endswith(".xml")]
print("\n=== res/xml candidates ===")
for n in nsc:
    print("  " + n)
