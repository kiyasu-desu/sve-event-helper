"""Offline privacy gate. Reports categories/locations only, never matched values.

Python 3.10+, standard library only. Does not import application code or use network.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import ipaddress
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_HOSTS = {
    "api-sve.nie.netease.com", "login-betasystem.nie.netease.com",
    "nie.netease.com", "www.163.com", "www.pushplus.plus", "pushplus.plus",
    "smtp.qq.com", "schemas.android.com", "www.w3.org", "github.com",
    "tailscale.com", "login.tailscale.com", "mirrors.huaweicloud.com",
    "bootstrap.pypa.io",
}
SYNTHETIC = {"10001", "10002", "123456", "999", "1", "0", "100000"}
RULES = {
    "private-key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "personal-path": r"(?i)(?:[a-z]:[\\/]+Users[\\/]+[^\s\\/<>\"']+|/h" r"ome/[^\s/<>\"']+)",
    "email": r"[\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,}",
    "phone": r"(?<![\w.])1[3-9]\d{9}(?![\w.])",
    "credential-literal": r'''(?i)["']?(?:app_?key|session_?id|cookie|token|password|authcode|secret(?:_key(?:_official|_ios|_dev)?)?)["']?\s*[:=]\s*["']([A-Za-z0-9_+/=;.-]{8,})["']''',
    "account-literal": r'''(?i)["']?(?:uid|nickname)["']?\s*[:=]\s*(?:["']([^"'\n]+)["']|(\d+))''',
    "capture-hash": r"\b[a-fA-F0-9]{64}\b",
}
COMPILED = {k: re.compile(v) for k, v in RULES.items()}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".ico", ".gif"}


@lru_cache(maxsize=4096)
def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL)


def findings(data: bytes, known: list[str]) -> list[tuple[int, str]]:
    # Binary artifacts require separate review; caller handles approved image formats.
    if b"\0" in data:
        return [(1, "binary-review-required")]
    try:
        source = data.decode("utf-8-sig")
    except UnicodeError:
        return [(1, "encoding-review-required")]
    hits = set()
    for number, line in enumerate(source.splitlines(), 1):
        for value in known:
            if value and value in line:
                hits.add((number, "known-private"))
        for category, pattern in COMPILED.items():
            for match in pattern.finditer(line):
                value = match.group(0)
                if category == "email" and value.endswith(("@example.com", "@example.org", "@users.noreply.github.com")):
                    continue
                if category == "personal-path" and re.search(r"(?i)[\\/]Users[\\/]+you(?:[\\/]|$)", value):
                    continue
                if category == "account-literal":
                    value = match.group(1) or match.group(2)
                    if value in SYNTHETIC or value.startswith(("<", "synthetic-", "test-")) or value in {"昵称", "测试账号"}:
                        continue
                    # Variable expressions (not string/number literals) never match.
                if category == "credential-literal" and match.group(1) in {"TESTSECRET", "NTES_SESS=fake", "NTES_SESS=fake-cookie-value"}:
                    continue
                hits.add((number, category))
        for match in re.finditer(r'https?://[^\s<>"`\x27)]+', line):
            try:
                url = urlsplit(match.group(0))
                host = url.hostname or ""
                if url.username or url.password:
                    hits.add((number, "url-credentials"))
                if host and host not in PUBLIC_HOSTS and host not in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "100.x.x.x"} and not (host == "example.com" or host.endswith(".example.com")):
                    if not any(c in host for c in "{}%$") and host != "..." and not host.endswith("."):
                        hits.add((number, "unreviewed-host"))
            except ValueError:
                hits.add((number, "invalid-url"))
        for match in re.finditer(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", line):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            # OIDs and public browser version numbers are not infrastructure addresses.
            if match.group().startswith(("2.5.4.", "1.2.840.")) or "Chrome/" in line or "MuMu " in line:
                continue
            if not address.is_loopback and not address.is_unspecified and str(address) != "10.0.2.2" and not str(address).startswith(("192.0.2.", "198.51.100.", "203.0.113.")):
                hits.add((number, "infrastructure-ip"))
    return sorted(hits)


def entries(mode: str, ref: str):
    if mode == "working":
        for raw in git("ls-files", "-z").split(b"\0"):
            if raw:
                name = raw.decode("utf-8")
                path = ROOT / name
                if path.is_symlink():
                    yield name, b"\0", ""
                elif path.is_file():
                    yield name, path.read_bytes(), ""
                else:
                    # Deleted tracked files are omitted from the candidate working tree.
                    continue
    else:
        command = ("ls-files", "--stage", "-z") if mode == "index" else ("ls-tree", "-rz", ref)
        for raw in git(*command).split(b"\0"):
            if not raw:
                continue
            meta, path = raw.split(b"\t", 1)
            parts = meta.split()
            sha = parts[1] if mode == "index" else parts[2]
            data = b"\0" if parts[0] in (b"120000", b"160000") else git("cat-file", "blob", sha.decode())
            yield path.decode("utf-8"), data, sha.decode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["working", "index", "tree", "history", "range"], default="working")
    parser.add_argument("--ref", default="HEAD", help="tree ref, history tip or base..tip range")
    parser.add_argument("--private-config", type=Path, default=ROOT / "config/private-scan.json")
    args = parser.parse_args()
    known = []
    if args.private_config.exists():
        config = json.loads(args.private_config.read_text(encoding="utf-8-sig"))
        known = config["known_private"]
        if not isinstance(known, list) or not all(isinstance(v, str) and v for v in known):
            raise ValueError("Invalid private scan configuration")
    else:
        print("NOTE: no private identifier list; generic rules only")
    refs = git("rev-list", args.ref).decode().splitlines() if args.mode in {"history", "range"} else [args.ref]
    count = 0
    scanned = 0
    images = 0
    cache = {}
    for ref in refs:
        mode = "tree" if args.mode in {"history", "range"} else args.mode
        if mode == "tree":
            metadata = git("show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", ref)
            for line, category in findings(metadata, known):
                count += 1
                print(f"{ref[:12]}:<commit-metadata>:{line}: {category}")
        for name, data, sha in entries(mode, ref):
            scanned += 1
            label = f"{ref[:12]}:" if mode == "tree" else ""
            # Never echo a private token even if someone uses it as a filename.
            safe_name = name
            for value in known:
                safe_name = safe_name.replace(value, "<redacted>")
            if findings(safe_name.encode("utf-8"), []):
                safe_name = "<redacted-path>"
            for _, category in findings(name.encode("utf-8"), known):
                count += 1
                print(f"{label}{safe_name}:<path>: {category}")
            if re.search(r"(?:^|/)(?:private\.properties|settings\.json|accounts\.json|subscriptions\.json|records\.json|notify\.json|remote\.json|identity-v2\.json|identity-migration\.json|deployment-state\.json|runtime-migration\.json|watchdog-state\.json|release\.json|secrets\.env|private-scan\.json|signing-samples\.json)$|(?:^|/)(?:identity-backups|deployment-backups|maintenance-backups|legacy-config|\.deployment-backups|\.maintenance)/|\.(?:apk|aab|jks|keystore|pem)$", name):
                count += 1
                if any("/" + folder + "/" in "/" + name for folder in ("identity-backups", ".deployment-backups", "deployment-backups", ".maintenance", "maintenance-backups", "legacy-config")):
                    safe_name = "<private-backup-path>"
                print(f"{label}{safe_name}:<path>: private-file")
            if Path(name).suffix.lower() in IMAGE_SUFFIXES:
                images += 1
                continue
            if sha and sha in cache:
                result = cache[sha]
            else:
                result = findings(data, known)
                if sha:
                    cache[sha] = result
            for line, category in result:
                count += 1
                print(f"{label}{safe_name}:{line}: {category}")
    print(f"Scanned {scanned} file versions; {count} findings; {images} image versions require visual review.")
    return 1 if count else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        print("Scan failed; configuration or Git read error (details suppressed).")
        raise SystemExit(2)
