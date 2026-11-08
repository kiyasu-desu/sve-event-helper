"""SVE 客户端签名算法（已从 bundle 源码还原并三重验证）。

来源：recon/bundle_device.bundle 中模块 2016 的 generateSign，
      哈希函数由模块 1962 提供（CryptoJS SHA256）。

原始实现：
    generateSign = function (data, extra, o) {
        var l = Object.assign({}, data, extra);
        l[o] = getSecretKey();                    // o 是 udid 的值，被当作 key 名
        var u = Object.keys(l).sort().map(function (t) {
            return isObject(l[t]) || isArray(l[t])
                ? t + "=" + JSON.stringify(l[t])
                : t + "=" + l[t];
        }).join("");
        return SHA256(u).toString();
    };

要点：
  * 签名 = SHA256(按 key 升序、无分隔符的 "k=v" 拼接串) 的十六进制（64 字符）
  * 第三个参数（udid）只作为 key 出现，其值被 appKey 覆盖
    → 注意：udid 的**值**被 appKey 覆盖，但 udid 本身仍是**键名**，
      所以【签名依赖这个 udid】——同一 body 换 udid 会得到不同签名。
      并不要求它是真实设备 ID，只要「签名用的 udid」与请求头里的
      deviceid 一致即可。（与设备无关的是 sessionid，不是签名。）
  * getSecretKey 按 API_BASE_URL 选择（正式服 / iOS / 测试服各一个密钥）
    —— **密钥本身不随仓库分发**，见下方 load_app_key()
  * 对象/数组值用 JSON.stringify（无空格）参与拼接

关于验证：
    开发期已用三组抓包样本（POST cancel / POST signup / GET 空 body）逐一比对，
    结论一致。但那三组样本绑定了**真实账号 uid**，且哈希只在"正式服密钥"下成立
    （换密钥必然不同、会误报失败），因此自检改为校验与密钥无关的**拼接串**
    （纯算法逻辑）。见文件末尾 __main__。
"""
from __future__ import annotations

import hashlib
import json
import os

# ------------------------------------------------------------------ appKey
# appKey 属于**不随仓库分发的凭据**：公开它等于给出"绕过签名校验"的完整实现。
# 因此源码里只保留读取逻辑，实际值由使用者提供。
#
# 取值优先级：环境变量 SVE_APP_KEY  >  config/settings.json 的 app_key  >  报错
#
# config/settings.json 已被 .gitignore 排除；服务端可用 systemd
# EnvironmentFile= 注入环境变量（详见 deploy/README.md）。
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sveenv
CONFIG_FILE = sveenv.SETTINGS_FILE

_KEY_CACHE: dict = {}


def load_app_key(which: str = "official") -> str:
    """读取 appKey。``which`` 可指定 official / ios / dev（对应不同服务端）。

    未配置时**明确报错**，而不是退回空串——否则会静默发出签名错误的请求，
    表现为"接口一直返回 403"，很难定位。
    """
    if "value" in _KEY_CACHE:
        return _KEY_CACHE["value"]
    val = (os.environ.get("SVE_APP_KEY") or "").strip()
    if not val:
        try:
            with open(CONFIG_FILE, encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {}
        node = cfg.get("app_key")
        if isinstance(node, str):
            val = node.strip()
        elif isinstance(node, dict):
            val = str(node.get(which) or "").strip()
    if not val:
        raise RuntimeError(
            "缺少 appKey（签名必需）。请任选一种方式提供：\n"
            "  1) 设置环境变量 SVE_APP_KEY\n"
            "  2) 在 %s 里填 {\"app_key\": \"<你的 appKey>\"}" % CONFIG_FILE)
    _KEY_CACHE["value"] = val
    return val

# udid 仅作为 key 名参与拼接，其值被 appKey 覆盖；但键名本身会进签名，
# 所以签名**依赖这个值**——它必须与请求头里的 deviceid 一致。
# 用一个固定占位是可行的（不要求是真实设备 ID），也让签名可复现。
DEFAULT_UDID = "0000000000000001"


def _render(value) -> str:
    """模拟 JS 模板拼接中 String(value) 的行为。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def sign_payload(data: dict | None = None, udid: str = DEFAULT_UDID,
                 secret_key: str | None = None,
                 extra: dict | None = None, key_name: str | None = None) -> str:
    """返回参与签名的原始拼接串（调试用）。

    ``secret_key=None`` 时按 load_app_key() 解析（环境变量 → 配置文件）。
    """
    if secret_key is None:
        secret_key = load_app_key()
    merged: dict = dict(data or {})
    if extra:
        merged.update(extra)
    # 第三参数是 udid 的值，它被当作 key 名；缺省时退回 "udid"
    merged[key_name if key_name else udid] = secret_key
    return "".join("%s=%s" % (k, _render(merged[k])) for k in sorted(merged.keys()))


def generate_sign(data: dict | None = None, udid: str = DEFAULT_UDID,
                  secret_key: str | None = None,
                  extra: dict | None = None, key_name: str | None = None) -> str:
    """按 APP 的算法生成 sign。``secret_key=None`` 时自动解析 appKey。"""
    payload = sign_payload(data, udid, secret_key, extra, key_name)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_headers(session: dict, body: dict | None = None) -> dict:
    """构造带合法 sign 的请求头。"""
    headers = {
        "content-type": "application/json",
        "deviceid": session["deviceid"],
        "platform": "android",
        "sessionid": session["sessionid"],
        "User-Agent": session.get("user_agent", "okhttp/4.9.2"),
        "sign": generate_sign(body or {}, udid=session.get("deviceid", DEFAULT_UDID)),
    }
    return headers


if __name__ == "__main__":
    # 自检：校验**拼接串**（纯算法逻辑，与 appKey 无关）。
    #
    # 原先这里比对了三组抓包签名，但那需要 (a) 真实账号 uid、(b) 正式服 appKey
    # 才能复现——两者都不该留在仓库里；而且一旦使用者换了密钥，哈希必然不同、
    # 会误报失败。所以改为用一个**测试密钥**校验拼接规则：排序、无分隔符、值渲染。
    TESTKEY = "TESTSECRET"
    UDID = DEFAULT_UDID

    def payload_of(body: dict) -> str:
        return sign_payload(body, udid=UDID, secret_key=TESTKEY)

    cases = [
        # 升序：udid 的值 0000... < action < tournament_id < uid
        (payload_of({"uid": 100000, "tournament_id": "33933", "action": "cancel"}),
         "%s=%saction=canceltournament_id=33933uid=100000" % (UDID, TESTKEY)),
        (payload_of({"uid": 100000, "tournament_id": "33933", "action": "signup"}),
         "%s=%saction=signuptournament_id=33933uid=100000" % (UDID, TESTKEY)),
        # 空 body：只剩 udid=key
        (payload_of({}),
         "%s=%s" % (UDID, TESTKEY)),
        # 对象/数组用 JSON.stringify（无空格）；布尔与 null 按 JS 字面量渲染
        (payload_of({"a": {"x": 1, "y": [2, 3]}, "b": True, "c": None}),
         '%s=%sa={"x":1,"y":[2,3]}b=truec=null' % (UDID, TESTKEY)),
        # extra 与 data 合并后一起参与排序
        (sign_payload({"uid": 100000}, udid=UDID, secret_key=TESTKEY,
                      extra={"zzz": 1}),
         "%s=%suid=100000zzz=1" % (UDID, TESTKEY)),
        # key_name 覆盖：key 名改掉，值仍是密钥
        (sign_payload({}, udid=UDID, secret_key=TESTKEY, key_name="deviceid"),
         "deviceid=%s" % TESTKEY),
    ]
    names = ["cancel", "signup", "空 body", "对象/数组/布尔/null",
             "extra 合并", "key_name 覆盖"]

    ok = 0
    print("== 拼接串校验（测试密钥 %s，与真实 appKey 无关）==" % TESTKEY)
    for (got, expected), name in zip(cases, names):
        match = got == expected
        ok += 1 if match else 0
        print("%s  %-22s" % ("[OK]  " if match else "[FAIL]", name))
        if not match:
            print("       expected = %s" % expected)
            print("       got      = %s" % got)

    # 签名可复现（同输入同输出）——确认没有引入随机性或时间依赖
    stable = (generate_sign({"uid": 100000}, secret_key=TESTKEY)
              == generate_sign({"uid": 100000}, secret_key=TESTKEY))
    print("%s  %-22s" % ("[OK]  " if stable else "[FAIL]", "签名可复现"))
    ok += 1 if stable else 0

    total = len(cases) + 1
    print("\n通过 %d/%d" % (ok, total))
    if ok != total:
        raise SystemExit(1)

    # 顺带确认 appKey 能解析到；未配置时给出明确提示（而不是静默发出错误签名的请求）
    try:
        load_app_key()
        print("appKey：已配置")
    except RuntimeError as exc:
        print("appKey：未配置 -> %s" % exc)
    raise SystemExit(0)
