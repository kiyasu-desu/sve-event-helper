"""Zero-dependency Cloudflare Access JWT verification and request user context.

Python 3.10+ standard library + system `openssl` CLI only.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
import urllib.request

PROXY_HEADERS = (
    "Cf-Access-Jwt-Assertion",
    "Cf-Access-Authenticated-User-Email",
    "Cf-Access-Authenticated-User-Id",
    "Cf-Connecting-IP",
    "Cf-Ray",
    "Cf-Visitor",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
)
_PROXY_HEADERS_LOWER = {name.lower() for name in PROXY_HEADERS}
_RSA_ENCRYPTION_ALG_ID = b"\x30\x0d\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01\x05\x00"


class AuthError(Exception):
    """Structured authentication/authorization failure mapped cleanly to HTTP."""

    def __init__(self, code: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class UserContext:
    user_id: str
    role: str
    email: str | None = None
    authenticated: bool = True


def _b64url_decode(segment: str) -> bytes:
    if not isinstance(segment, str) or not segment:
        raise AuthError("invalid_token", "Cloudflare Access 令牌段无效", 401)
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise AuthError("invalid_token", "Cloudflare Access 令牌编码无效", 401) from exc


def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes((length,))
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(encoded),)) + encoded


def _der_tlv(tag: int, body: bytes) -> bytes:
    return bytes((tag,)) + _der_len(len(body)) + body


def _der_int(raw: bytes) -> bytes:
    stripped = raw.lstrip(b"\x00")
    if not stripped:
        raise ValueError("RSA public key parameter cannot be zero")
    if stripped[0] & 0x80:
        stripped = b"\x00" + stripped
    return _der_tlv(0x02, stripped)


def jwk_rsa_to_pem(jwk: dict) -> bytes:
    """Convert an RSA JWK (`kty == 'RSA'`, `n`, `e`) into SubjectPublicKeyInfo PEM."""
    if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
        raise ValueError("Unsupported JWK key type")
    n_b64 = jwk.get("n")
    e_b64 = jwk.get("e")
    if not isinstance(n_b64, str) or not isinstance(e_b64, str):
        raise ValueError("Missing RSA JWK modulus or exponent")
    try:
        n_bytes = _b64url_decode(n_b64)
        e_bytes = _b64url_decode(e_b64)
    except AuthError as exc:
        raise ValueError("Invalid RSA JWK base64url encoding") from exc
    rsa_pub = _der_tlv(0x30, _der_int(n_bytes) + _der_int(e_bytes))
    spki = _der_tlv(0x30, _RSA_ENCRYPTION_ALG_ID + _der_tlv(0x03, b"\x00" + rsa_pub))
    b64 = base64.b64encode(spki).decode("ascii")
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"
    return pem.encode("ascii")


def verify_with_openssl(signing_input: bytes, signature: bytes, public_pem: bytes) -> bool:
    """Verify an RS256 signature using system `openssl dgst -sha256 -verify` (no shell)."""
    if not signing_input or not signature or not public_pem:
        return False
    with tempfile.TemporaryDirectory(prefix="sve-jwt-") as tmpdir:
        try:
            os.chmod(tmpdir, 0o700)
        except OSError:
            pass
        pem_path = os.path.join(tmpdir, "pub.pem")
        sig_path = os.path.join(tmpdir, "sig.bin")
        for path, payload in ((pem_path, public_pem), (sig_path, signature)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
        try:
            proc = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", pem_path, "-signature", sig_path, "-"],
                input=signing_input,
                capture_output=True,
                timeout=10,
                check=False,
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


def normalize_team_origin(team_domain: str) -> str:
    text = str(team_domain or "").strip().rstrip("/")
    if not text:
        raise AuthError("auth_misconfigured", "未配置 Cloudflare Access Team 域名", 500)
    if text.lower().startswith("http://"):
        raise AuthError("auth_misconfigured", "Cloudflare Access Team 必须使用 HTTPS", 500)
    if text.lower().startswith("https://"):
        parsed = urlsplit(text)
        if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise AuthError("auth_misconfigured", "Cloudflare Access Team 域名格式无效", 500)
        return f"https://{parsed.netloc.lower()}"
    if not re.fullmatch(r"[A-Za-z0-9.-]+", text) or text.startswith(".") or text.endswith(".") or ".." in text:
        raise AuthError("auth_misconfigured", "Cloudflare Access Team 名称格式无效", 500)
    if "." not in text:
        return "https://" + text.lower() + ".cloudflareaccess.com"
    return "https://" + text.lower()


def _default_certs_fetcher(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "sve-monitor-auth/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


class CloudflareAccessVerifier:
    """Verifies Cloudflare Access `Cf-Access-Jwt-Assertion` RS256 tokens."""

    def __init__(
        self,
        team_domain: str,
        audience: str,
        *,
        certs_fetcher=None,
        sig_verifier=None,
        ttl: int = 600,
        now_fn=time.time,
    ) -> None:
        self.team_origin = normalize_team_origin(team_domain)
        self.certs_url = f"{self.team_origin}/cdn-cgi/access/certs"
        self.audience = str(audience or "").strip()
        if not self.audience:
            raise AuthError("auth_misconfigured", "未配置 Cloudflare Access AUD", 500)
        self._certs_fetcher = certs_fetcher or _default_certs_fetcher
        self._sig_verifier = sig_verifier or verify_with_openssl
        self._ttl = max(30, int(ttl))
        self._now_fn = now_fn
        self._lock = threading.Lock()
        self._keys: dict[str, bytes] = {}
        self._fetched_at: float = -1e18
        self._last_attempt_at: float = -1e18
        self._refresh_cooldown: float = 15.0

    def _load_keys(self, *, force: bool = False) -> dict[str, bytes]:
        with self._lock:
            now = float(self._now_fn())
            cache_valid = bool(self._keys) and (now - self._fetched_at) < self._ttl
            if cache_valid and not force:
                return dict(self._keys)
            if force and self._fetched_at > 0 and (now - self._last_attempt_at) < self._refresh_cooldown:
                return dict(self._keys)
            self._last_attempt_at = now
            try:
                payload = self._certs_fetcher(self.certs_url)
            except Exception as exc:
                if self._keys:
                    return dict(self._keys)
                raise AuthError("auth_certs_unavailable", "无法获取 Cloudflare Access 公钥", 503) from exc
            parsed: dict[str, bytes] = {}
            raw_keys = payload.get("keys") if isinstance(payload, dict) else None
            if isinstance(raw_keys, list):
                for item in raw_keys:
                    if not isinstance(item, dict) or item.get("kty") != "RSA":
                        continue
                    kid = item.get("kid")
                    if not isinstance(kid, str) or not kid.strip():
                        continue
                    try:
                        parsed[kid] = jwk_rsa_to_pem(item)
                    except ValueError:
                        continue
            if not parsed:
                if self._keys:
                    return dict(self._keys)
                raise AuthError("invalid_token", "Cloudflare Access 公钥清单为空或格式无效", 401)
            self._keys = parsed
            self._fetched_at = now
            return dict(self._keys)

    def verify_token(self, token: str) -> dict:
        if not isinstance(token, str):
            raise AuthError("invalid_token", "Cloudflare Access 令牌格式无效", 401)
        parts = token.strip().split(".")
        if len(parts) != 3:
            raise AuthError("invalid_token", "Cloudflare Access 令牌结构无效", 401)
        header_b64, payload_b64, sig_b64 = parts
        try:
            header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
            claims = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise AuthError("invalid_token", "Cloudflare Access 令牌载荷无法解析", 401) from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AuthError("invalid_token", "Cloudflare Access 令牌格式无效", 401)
        if header.get("alg") != "RS256":
            raise AuthError("invalid_token", "Cloudflare Access 令牌签名算法不受支持", 401)
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid.strip():
            raise AuthError("invalid_token", "Cloudflare Access 令牌缺少 kid", 401)

        iss = claims.get("iss")
        if not isinstance(iss, str) or iss.strip().rstrip("/").lower() != self.team_origin:
            raise AuthError("invalid_token", "Cloudflare Access 签发者不匹配", 401)

        aud = claims.get("aud")
        aud_ok = (
            (isinstance(aud, str) and aud == self.audience)
            or (isinstance(aud, list) and any(isinstance(item, str) and item == self.audience for item in aud))
        )
        if not aud_ok:
            raise AuthError("invalid_token", "Cloudflare Access 受众不匹配", 401)

        now = float(self._now_fn())
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp <= now - 30:
            raise AuthError("invalid_token", "Cloudflare Access 令牌已过期", 401)
        nbf = claims.get("nbf")
        if nbf is not None:
            if isinstance(nbf, bool) or not isinstance(nbf, (int, float)) or nbf > now + 30:
                raise AuthError("invalid_token", "Cloudflare Access 令牌尚未生效", 401)

        raw_email = claims.get("email")
        if not isinstance(raw_email, str) or "@" not in raw_email or not raw_email.strip():
            raise AuthError("invalid_token", "Cloudflare Access 令牌缺少邮箱声明", 401)
        email = raw_email.strip().lower()

        signature = _b64url_decode(sig_b64)
        if not signature:
            raise AuthError("invalid_token", "Cloudflare Access 令牌签名为空", 401)

        keys = self._load_keys(force=False)
        public_pem = keys.get(kid)
        if public_pem is None:
            keys = self._load_keys(force=True)
            public_pem = keys.get(kid)
        if public_pem is None:
            raise AuthError("invalid_token", "未找到匹配的 Cloudflare Access 签名公钥", 401)

        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        if not self._sig_verifier(signing_input, signature, public_pem):
            raise AuthError("invalid_token", "Cloudflare Access 令牌签名校验失败", 401)

        out = dict(claims)
        out["email"] = email
        return out


def _iter_headers(headers):
    if headers is None:
        return ()
    if hasattr(headers, "items"):
        return headers.items()
    return ()


def get_header(headers, name: str) -> str:
    target = name.lower()
    for key, value in _iter_headers(headers):
        if str(key).lower() == target and value is not None:
            return str(value).strip()
    return ""


def has_proxy_headers(headers) -> bool:
    for key, value in _iter_headers(headers):
        if str(key).lower() in _PROXY_HEADERS_LOWER and value is not None and str(value).strip() != "":
            return True
    return False


def resolve_request_user(
    headers,
    remote_addr: str | None,
    env: dict | None = None,
    verifier: CloudflareAccessVerifier | None = None,
) -> UserContext:
    """Resolve authenticated request UserContext with strict proxy-boundary enforcement."""
    environ = os.environ if env is None else env
    mode = str(environ.get("SVE_AUTH_MODE") or "single").strip().lower()
    if mode in ("", "single"):
        return UserContext(user_id="owner", role="admin", email=None, authenticated=True)
    if mode != "cloudflare":
        raise AuthError("auth_misconfigured", "不支持的 SVE_AUTH_MODE 配置", 500)

    team = str(environ.get("SVE_CF_ACCESS_TEAM") or "").strip()
    aud = str(environ.get("SVE_CF_ACCESS_AUD") or "").strip()
    admin_emails = {
        item.strip().lower()
        for item in str(environ.get("SVE_ADMIN_EMAILS") or "").split(",")
        if item.strip()
    }
    if not team or not aud or not admin_emails:
        raise AuthError("auth_misconfigured", "Cloudflare Access 认证配置不完整", 500)
    normalize_team_origin(team)

    addr = str(remote_addr or "").strip()
    is_loopback = addr in {"127.0.0.1", "::1"}
    if not has_proxy_headers(headers) and is_loopback:
        allow_local = str(environ.get("SVE_ALLOW_LOCAL_ADMIN", "1")).strip().lower() in ("1", "true", "yes", "on")
        if allow_local:
            return UserContext(user_id="owner", role="admin", email=None, authenticated=True)
        raise AuthError("unauthorized", "本地直连管理员访问已禁用，请通过 Cloudflare Access 访问", 401)

    token = get_header(headers, "Cf-Access-Jwt-Assertion")
    if not token:
        raise AuthError("unauthorized", "缺少 Cloudflare Access 身份令牌", 401)
    if verifier is None:
        raise AuthError("auth_misconfigured", "Cloudflare Access 验证器未初始化", 500)

    claims = verifier.verify_token(token)
    email = claims["email"].strip().lower()
    if email in admin_emails:
        return UserContext(user_id="owner", role="admin", email=email, authenticated=True)
    return UserContext(user_id=email, role="user", email=email, authenticated=True)
