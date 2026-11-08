"""Probe whether the captured session can drive the tournament API directly.

This is the make-or-break question for a standalone poller: if the captured
`sessionid` + `sign` headers still work, polling can be a ~60 line script with
no emulator, no frida and no proxy. If they are rejected, the signature is
per-request and has to be recovered from the app.

Usage:
    python scripts/probe/probe_api.py            # connectivity only
    python scripts/probe/probe_api.py --detail   # also fetch the tournament detail

Credentials are read from capture/session.json (written by hand or by a helper)
and are never printed in full.
"""
import argparse
import json
import os
import sys
import time

import requests

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
import sveenv  # noqa: E402
SESSION_FILE = sveenv.ACCOUNTS_FILE   # 会话统一来源
BASE = "https://api-sve.nie.netease.com/sve/api/v1"
TOURNAMENT_ID = "33997"
TIMEOUT = 20


def mask(value: str) -> str:
    if not value:
        return "(empty)"
    return value[:8] + "..." + value[-4:] if len(value) > 14 else value


def load_session() -> dict:
    session = sveenv.load_session()
    if not session or not session.get("sessionid"):
        sys.exit("取不到会话：请确认 %s 里有账号且 active 指向它" % SESSION_FILE)
    # utf-8-sig: PowerShell's Set-Content -Encoding UTF8 writes a BOM.
    data = session
    for key in ("sessionid", "deviceid", "sign"):
        if not data.get(key):
            sys.exit("session file is missing '%s'" % key)
    return data


def build_headers(session: dict) -> dict:
    return {
        "content-type": "application/json",
        "deviceid": session["deviceid"],
        "platform": "android",
        "sign": session["sign"],
        "sessionid": session["sessionid"],
        "User-Agent": session.get("user_agent", "okhttp/4.9.2"),
    }


def call(headers: dict, path: str, params: dict | None = None):
    url = "%s/%s" % (BASE, path)
    started = time.time()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return None, "request failed: %s" % exc, time.time() - started
    elapsed = time.time() - started
    return resp, None, elapsed


def show(label: str, resp, err, elapsed: float) -> None:
    if err:
        print("  %-28s ERROR  %s" % (label, err))
        return
    body = resp.text
    preview = body[:200].replace("\n", " ")
    print("  %-28s HTTP %s  %.2fs  %s" % (label, resp.status_code, elapsed, preview))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true", help="also fetch the tournament detail")
    ap.add_argument("--list", action="store_true", help="also fetch the tournament list")
    args = ap.parse_args()

    session = load_session()
    print("session file : %s" % SESSION_FILE)
    print("sessionid    : %s" % mask(session["sessionid"]))
    print("deviceid     : %s" % mask(session["deviceid"]))
    print("sign         : %s" % mask(session["sign"]))
    print()

    headers = build_headers(session)

    print("connectivity:")
    resp, err, el = call(headers, "version/latest")
    show("version/latest", resp, err, el)
    resp, err, el = call(headers, "cf")
    show("cf", resp, err, el)

    if args.list:
        print("\ntournament list (Guangzhou):")
        resp, err, el = call(headers, "urs/tournament/list",
                             {"_size": 20, "_page": 1, "area": "广东省-广州市", "is_sv2": 0})
        show("tournament/list", resp, err, el)
        if resp is not None and resp.status_code == 200:
            try:
                items = resp.json().get("items", [])
                for it in items[:5]:
                    print("      id=%s name=%s status=%s" %
                          (it.get("tournament_id"), it.get("name"), it.get("zh_user_status")))
            except Exception as exc:  # noqa: BLE001
                print("      body is not the expected JSON: %s" % exc)

    if args.detail:
        print("\ntournament detail %s:" % TOURNAMENT_ID)
        resp, err, el = call(headers, "urs/tournament/detail/%s" % TOURNAMENT_ID)
        show("tournament/detail", resp, err, el)
        if resp is not None and resp.status_code == 200:
            try:
                item = resp.json().get("item", {})
                print("      name       = %s" % item.get("name"))
                print("      player_num = %s" % item.get("player_num"))
                print("      max        = %s" % item.get("max_player_number"))
                print("      my status  = %s" % item.get("zh_user_status"))
            except Exception as exc:  # noqa: BLE001
                print("      body is not the expected JSON: %s" % exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
