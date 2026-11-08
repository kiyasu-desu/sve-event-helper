"""Stable UID identity and version preconditions. Names are display data only."""


class AccountConflict(ValueError):
    pass


def normalized_uid(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AccountConflict("账号 UID 缺失或非法")
    text = str(value)
    if not text.isascii() or not text.isdecimal() or not text.lstrip("0"):
        raise AccountConflict("账号 UID 缺失或非法")
    return text.lstrip("0")


def revision(account) -> int:
    if account is None:
        return 0
    return account.version


def check_identity(accounts: dict, name: str, uid) -> None:
    uid = normalized_uid(uid)
    if uid in accounts and normalized_uid(accounts[uid].uid) != uid:
        raise AccountConflict("身份表的 UID 不一致")


def parse_account(body, account_type):
    if not isinstance(body, dict):
        raise AccountConflict("账号必须是对象")
    for field in ("name", "sessionid", "deviceid", "user_agent"):
        if not isinstance(body.get(field), str) or not body[field].strip():
            raise AccountConflict("账号缺少有效的 %s" % field)
    if not isinstance(body.get("nickname", ""), str) or not isinstance(body.get("mobile", ""), str):
        raise AccountConflict("账号资料格式无效")
    normalized_uid(body.get("uid"))
    return account_type.from_dict(body)
