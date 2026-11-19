"""M24 unit tests for Cloudflare Access JWT verification and proxy-boundary auth context."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/client/core"))

HAS_OPENSSL = shutil.which("openssl") is not None


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_rs256_openssl(private_key_path: Path, signing_input: bytes) -> bytes:
    proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key_path)],
        input=signing_input,
        capture_output=True,
        check=True,
        timeout=15,
    )
    return proc.stdout


def _make_jwt(header: dict, payload: dict, signer) -> str:
    h_b64 = _b64u_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p_b64 = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = signer(signing_input)
    return f"{h_b64}.{p_b64}.{_b64u_encode(sig)}"


def _rsa_jwk_from_private_pem(private_key_path: Path, kid: str) -> dict:
    mod_out = subprocess.check_output(
        ["openssl", "rsa", "-in", str(private_key_path), "-pubout", "-modulus", "-noout"],
        stderr=subprocess.DEVNULL,
        timeout=15,
    ).decode("ascii").strip()
    assert mod_out.startswith("Modulus=")
    n_bytes = bytes.fromhex(mod_out[len("Modulus="):])
    # Standard openssl genrsa uses public exponent 65537 (0x010001).
    e_bytes = (65537).to_bytes(3, "big")
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
        "n": _b64u_encode(n_bytes),
        "e": _b64u_encode(e_bytes),
    }


class AuthContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory(prefix="sve-m24-auth-")
        cls.addClassCleanup(cls.tmpdir.cleanup)
        cls.key_dir = Path(cls.tmpdir.name)
        cls.team = "test-team"
        cls.issuer = "https://" + cls.team + ".cloudflareaccess.com"
        cls.aud = "synthetic-audience-tag-m24"
        if HAS_OPENSSL:
            cls.priv1 = cls.key_dir / "priv1.pem"
            cls.priv2 = cls.key_dir / "priv2.pem"
            subprocess.run(
                ["openssl", "genrsa", "-out", str(cls.priv1), "2048"],
                check=True, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["openssl", "genrsa", "-out", str(cls.priv2), "2048"],
                check=True, capture_output=True, timeout=30,
            )
            cls.jwk1 = _rsa_jwk_from_private_pem(cls.priv1, "kid-1")
            cls.jwk2 = _rsa_jwk_from_private_pem(cls.priv2, "kid-2")

    def test_single_mode_defaults_to_owner_admin(self):
        import auth_context as ac

        for env in ({}, {"SVE_AUTH_MODE": ""}, {"SVE_AUTH_MODE": "single"}):
            ctx = ac.resolve_request_user(
                {"Cf-Connecting-IP": "192.0.2.10"},
                "192.0.2.10",
                env=env,
                verifier=None,
            )
            self.assertEqual(
                ac.UserContext(user_id="owner", role="admin", email=None, authenticated=True),
                ctx,
            )

        with self.assertRaises(ac.AuthError) as err:
            ac.resolve_request_user({}, "127.0.0.1", env={"SVE_AUTH_MODE": "invalid-mode"}, verifier=None)
        self.assertEqual(500, err.exception.status_code)

    def test_cloudflare_mode_requires_team_aud_and_admins(self):
        import auth_context as ac

        base = {
            "SVE_AUTH_MODE": "cloudflare",
            "SVE_CF_ACCESS_TEAM": self.team,
            "SVE_CF_ACCESS_AUD": self.aud,
            "SVE_ADMIN_EMAILS": "admin@example.com",
        }
        for missing in ("SVE_CF_ACCESS_TEAM", "SVE_CF_ACCESS_AUD", "SVE_ADMIN_EMAILS"):
            env = dict(base)
            env.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaises(ac.AuthError) as err:
                    ac.resolve_request_user({}, "127.0.0.1", env=env, verifier=None)
                self.assertEqual(500, err.exception.status_code)

        env = dict(base, SVE_ADMIN_EMAILS=" , ")
        with self.assertRaises(ac.AuthError) as err:
            ac.resolve_request_user({}, "127.0.0.1", env=env, verifier=None)
        self.assertEqual(500, err.exception.status_code)

    def test_proxy_headers_on_loopback_without_jwt_fail_closed(self):
        import auth_context as ac

        env = {
            "SVE_AUTH_MODE": "cloudflare",
            "SVE_CF_ACCESS_TEAM": self.team,
            "SVE_CF_ACCESS_AUD": self.aud,
            "SVE_ADMIN_EMAILS": "admin@example.com",
            "SVE_ALLOW_LOCAL_ADMIN": "1",
        }
        verifier = ac.CloudflareAccessVerifier(
            self.team, self.aud,
            certs_fetcher=lambda _url: {"keys": []},
            sig_verifier=lambda _i, _s, _p: False,
        )
        spoof_headers = [
            {"Cf-Connecting-IP": "192.0.2.5"},
            {"cf-ray": "synthetic-ray-1"},
            {"Cf-Visitor": '{"scheme":"https"}'},
            {"X-Forwarded-For": "192.0.2.5"},
            {"X-Forwarded-Host": "sve.example.com"},
            {"X-Forwarded-Proto": "https"},
            {"Cf-Access-Authenticated-User-Email": "admin@example.com"},
        ]
        for hdrs in spoof_headers:
            with self.subTest(headers=hdrs):
                with self.assertRaises(ac.AuthError) as err:
                    ac.resolve_request_user(hdrs, "127.0.0.1", env=env, verifier=verifier)
                self.assertEqual(401, err.exception.status_code)

    def test_direct_loopback_without_proxy_headers_allows_local_admin_only_when_enabled(self):
        import auth_context as ac

        base_env = {
            "SVE_AUTH_MODE": "cloudflare",
            "SVE_CF_ACCESS_TEAM": self.team,
            "SVE_CF_ACCESS_AUD": self.aud,
            "SVE_ADMIN_EMAILS": "admin@example.com",
        }
        verifier = ac.CloudflareAccessVerifier(
            self.team, self.aud,
            certs_fetcher=lambda _url: {"keys": []},
            sig_verifier=lambda _i, _s, _p: False,
        )
        for addr in ("127.0.0.1", "::1"):
            ctx = ac.resolve_request_user({}, addr, env=base_env, verifier=verifier)
            self.assertEqual("owner", ctx.user_id)
            self.assertEqual("admin", ctx.role)

            ctx_enabled = ac.resolve_request_user(
                {}, addr, env=dict(base_env, SVE_ALLOW_LOCAL_ADMIN="1"), verifier=verifier
            )
            self.assertEqual("owner", ctx_enabled.user_id)
            self.assertEqual("admin", ctx_enabled.role)

            with self.assertRaises(ac.AuthError) as err:
                ac.resolve_request_user(
                    {}, addr, env=dict(base_env, SVE_ALLOW_LOCAL_ADMIN="0"), verifier=verifier
                )
            self.assertEqual(401, err.exception.status_code)

        with self.assertRaises(ac.AuthError) as err:
            ac.resolve_request_user({}, "192.0.2.10", env=base_env, verifier=verifier)
        self.assertEqual(401, err.exception.status_code)

    def test_mock_verifier_validates_claims_and_role_mapping(self):
        import auth_context as ac

        now = 1_800_000_000.0
        jwk = {
            "kty": "RSA",
            "kid": "mock-kid",
            "n": _b64u_encode(b"\x01" * 256),
            "e": _b64u_encode((65537).to_bytes(3, "big")),
        }
        calls = []

        def fake_verify(signing_input: bytes, signature: bytes, public_pem: bytes) -> bool:
            calls.append((signing_input, signature, public_pem))
            return signature == b"valid-sig"

        verifier = ac.CloudflareAccessVerifier(
            f"https://{self.team}.example.com/",
            self.aud,
            certs_fetcher=lambda _url: {"keys": [jwk]},
            sig_verifier=fake_verify,
            now_fn=lambda: now,
        )
        jwt = _make_jwt(
            {"alg": "RS256", "kid": "mock-kid", "typ": "JWT"},
            {
                "iss": f"https://{self.team}.example.com",
                "aud": [self.aud],
                "exp": int(now) + 300,
                "nbf": int(now) - 10,
                "email": "Friend@example.com",
            },
            lambda _inp: b"valid-sig",
        )
        env = {
            "SVE_AUTH_MODE": "cloudflare",
            "SVE_CF_ACCESS_TEAM": f"https://{self.team}.example.com/",
            "SVE_CF_ACCESS_AUD": self.aud,
            "SVE_ADMIN_EMAILS": "admin@example.com",
        }
        ctx = ac.resolve_request_user(
            {"Cf-Access-Jwt-Assertion": jwt, "Cf-Connecting-IP": "192.0.2.50"},
            "127.0.0.1",
            env=env,
            verifier=verifier,
        )
        self.assertEqual(1, len(calls))
        self.assertIn(b"-----BEGIN PUBLIC KEY-----", calls[0][2])
        self.assertEqual(
            ac.UserContext(user_id="friend@example.com", role="user", email="friend@example.com", authenticated=True),
            ctx,
        )

    @unittest.skipUnless(HAS_OPENSSL, "openssl CLI required for real RS256 verification tests")
    def test_valid_rs256_jwt_routes_admin_and_user_roles(self):
        import auth_context as ac

        now = 1_800_000_000.0
        certs_urls = []

        def fetch(url: str):
            certs_urls.append(url)
            return {"keys": [self.jwk1]}

        verifier = ac.CloudflareAccessVerifier(
            self.team,
            self.aud,
            certs_fetcher=fetch,
            now_fn=lambda: now,
        )
        env = {
            "SVE_AUTH_MODE": "cloudflare",
            "SVE_CF_ACCESS_TEAM": self.team,
            "SVE_CF_ACCESS_AUD": self.aud,
            "SVE_ADMIN_EMAILS": "admin@example.com, second-admin@example.com",
        }

        admin_jwt = _make_jwt(
            {"alg": "RS256", "kid": "kid-1"},
            {"iss": self.issuer, "aud": [self.aud], "exp": int(now) + 120, "email": "ADMIN@example.com"},
            lambda inp: _sign_rs256_openssl(self.priv1, inp),
        )
        # Even if Cf-Access-Authenticated-User-Email header is spoofed, JWT email wins.
        admin_ctx = ac.resolve_request_user(
            {
                "Cf-Connecting-IP": "192.0.2.10",
                "Cf-Access-Jwt-Assertion": admin_jwt,
                "Cf-Access-Authenticated-User-Email": "friend@example.com",
            },
            "127.0.0.1",
            env=env,
            verifier=verifier,
        )
        self.assertEqual("owner", admin_ctx.user_id)
        self.assertEqual("admin", admin_ctx.role)
        self.assertEqual("admin@example.com", admin_ctx.email)
        self.assertTrue(admin_ctx.authenticated)

        user_jwt = _make_jwt(
            {"alg": "RS256", "kid": "kid-1"},
            {"iss": self.issuer, "aud": self.aud, "exp": int(now) + 120, "email": "Friend@example.com"},
            lambda inp: _sign_rs256_openssl(self.priv1, inp),
        )
        user_ctx = ac.resolve_request_user(
            {
                "X-Forwarded-For": "192.0.2.11",
                "Cf-Access-Jwt-Assertion": user_jwt,
                "Cf-Access-Authenticated-User-Email": "admin@example.com",
            },
            "127.0.0.1",
            env=env,
            verifier=verifier,
        )
        self.assertEqual("friend@example.com", user_ctx.user_id)
        self.assertEqual("user", user_ctx.role)
        self.assertEqual("friend@example.com", user_ctx.email)
        self.assertEqual([self.issuer + "/cdn-cgi/access/certs"], certs_urls)

    @unittest.skipUnless(HAS_OPENSSL, "openssl CLI required for real RS256 verification tests")
    def test_rejects_expired_wrong_aud_wrong_iss_or_tampered_signature(self):
        import auth_context as ac

        clock = [1_800_000_000.0]
        fetch_count = [0]
        keyset = {"keys": [self.jwk1]}

        def fetch(_url: str):
            fetch_count[0] += 1
            return keyset

        verifier = ac.CloudflareAccessVerifier(
            self.team,
            self.aud,
            certs_fetcher=fetch,
            now_fn=lambda: clock[0],
        )

        def sign1(extra_header=None, **claims_override):
            header = {"alg": "RS256", "kid": "kid-1"}
            if extra_header:
                header.update(extra_header)
            claims = {
                "iss": self.issuer,
                "aud": [self.aud],
                "exp": int(clock[0]) + 120,
                "nbf": int(clock[0]) - 5,
                "email": "friend@example.com",
            }
            claims.update(claims_override)
            return _make_jwt(header, claims, lambda inp: _sign_rs256_openssl(self.priv1, inp))

        bad_tokens = [
            "not-a-jwt",
            sign1(exp=int(clock[0]) - 60),
            sign1(nbf=int(clock[0]) + 60),
            sign1(aud=["wrong-aud"]),
            sign1(iss="https://wrong-team.example.com"),
            sign1(email=""),
            sign1(extra_header={"alg": "HS256"}),
        ]
        good = sign1()
        h, p, s = good.split(".")
        tampered_payload = _b64u_encode(json.dumps({
            "iss": self.issuer,
            "aud": [self.aud],
            "exp": int(clock[0]) + 120,
            "email": "admin@example.com",
        }).encode("utf-8"))
        bad_tokens.append(f"{h}.{tampered_payload}.{s}")
        bad_tokens.append(f"{h}.{p}.AAAA")

        for tok in bad_tokens:
            with self.subTest(token=tok[:24]):
                with self.assertRaises(ac.AuthError) as err:
                    verifier.verify_token(tok)
                self.assertEqual(401, err.exception.status_code)

        # Unknown kid within cooldown does not immediately re-fetch.
        tok_k2 = _make_jwt(
            {"alg": "RS256", "kid": "kid-2"},
            {"iss": self.issuer, "aud": [self.aud], "exp": int(clock[0]) + 120, "email": "friend@example.com"},
            lambda inp: _sign_rs256_openssl(self.priv2, inp),
        )
        before_fetches = fetch_count[0]
        with self.assertRaises(ac.AuthError) as err:
            verifier.verify_token(tok_k2)
        self.assertEqual(401, err.exception.status_code)
        self.assertEqual(before_fetches, fetch_count[0])

        # After cooldown, unknown kid refreshes JWKS and succeeds with rotated key.
        keyset = {"keys": [self.jwk1, self.jwk2]}
        clock[0] += 20.0
        claims = verifier.verify_token(tok_k2)
        self.assertEqual("friend@example.com", claims["email"])
        self.assertEqual(before_fetches + 1, fetch_count[0])


if __name__ == "__main__":
    unittest.main()
