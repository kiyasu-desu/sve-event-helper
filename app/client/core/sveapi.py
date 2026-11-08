"""SVE 客户端核心：接口客户端。

把逆向得到的接口知识固化成一个模块，供客户端与脚本共用：

  * 签名算法（已从 bundle 还原并实测）
        sign = SHA256( 按 key 排序的 "k=v" 拼接 )，其中插入一项 "<udid>=<appKey>"
        注意：udid 作为**键名**进入签名，所以【签名依赖该值】，必须与请求头的
        deviceid 一致；不要求是真实设备 ID。与设备无关的是 sessionid。
  * 列表接口的签名：需要把 **query 参数并入签名内容**
        （detail 用空 body 即可；list 必须带 query，否则 403 Invalid sign）
  * 三个 action：signup（报名）/ cancel（取消）/ book（预约）
  * 登录换取会话：POST /urs/login 只需 Cookie（不校验 sign / sessionid）

设计要点：
  - 不依赖任何全局状态，所有身份都通过 Account 传入；
  - 会话与 deviceid 解耦（实测同一 sessionid 配任意 deviceid 均有效）；
  - 失败时抛 SveError，调用方自己决定重试策略。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime

BASE = "https://api-sve.nie.netease.com/sve/api/v1"
DEFAULT_UA = "okhttp/4.9.2"
DEFAULT_UDID = "5f3a9c1d7e2b4806"
TIMEOUT = 25

# appKey 属于**不随仓库分发的凭据**：公开它等于给出"绕过签名校验"的完整实现。
# 源码只保留读取逻辑，实际值由使用者提供。
# 取值优先级：环境变量 SVE_APP_KEY > RuntimePaths.settings 的 app_key > 报错
# （config/settings.json 已被 .gitignore 排除）
from runtime_paths import RuntimePaths
_CONFIG_FILE = str(RuntimePaths(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))).settings)
_APP_KEY_CACHE: list = []


def app_key() -> str:
    """解析 appKey。未配置时明确报错，而不是用空串静默发出签名错误的请求。"""
    if _APP_KEY_CACHE:
        return _APP_KEY_CACHE[0]
    val = (os.environ.get("SVE_APP_KEY") or "").strip()
    if not val:
        try:
            with open(_CONFIG_FILE, encoding="utf-8-sig") as fh:
                node = (json.load(fh) or {}).get("app_key")
        except Exception:
            node = None
        if isinstance(node, str):
            val = node.strip()
        elif isinstance(node, dict):
            val = str(node.get("official") or "").strip()
    if not val:
        raise SveError(
            "缺少 appKey（签名必需）。请任选一种方式提供：\n"
            "  1) 设置环境变量 SVE_APP_KEY\n"
            "  2) 在 %s 里填 {\"app_key\": \"<你的 appKey>\"}" % _CONFIG_FILE)
    _APP_KEY_CACHE.append(val)
    return val


class SveError(Exception):
    """接口调用失败（含 HTTP 错误与服务端 status=failed）。"""

    def __init__(self, msg, code=None, detail=None, http=None):
        super().__init__(msg)
        self.code = code
        self.detail = detail
        self.http = http


# ------------------------------------------------------------------ 签名
def generate_sign(data: dict, udid: str = DEFAULT_UDID, app_key_val: str | None = None) -> str:
    """还原自 bundle 的签名算法。

    关键点：appKey 是以 "<udid>" 作为**键名**插入的，而不是某个固定字段名。
    ``app_key_val=None`` 时按 app_key() 解析。
    """
    key = app_key_val or app_key()
    merged = {}
    for k, v in (data or {}).items():
        if isinstance(v, (dict, list)):
            merged[k] = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
        else:
            merged[k] = v
    merged[udid] = key
    payload = "".join("%s=%s" % (k, merged[k]) for k in sorted(merged))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sign_payload(data: dict, udid: str = DEFAULT_UDID, app_key_val: str | None = None) -> str:
    """把参与签名的串还原出来，便于排错。"""
    merged = dict(data or {})
    merged[udid] = app_key_val or app_key()
    return "".join("%s=%s" % (k, merged[k]) for k in sorted(merged))


# ------------------------------------------------------------------ 账号
@dataclass
class Account:
    """一个账号的会话与标识。

    deviceid 与 sessionid 无绑定关系（实测），但仍按账号各自保存，
    以便抓包/排错时能还原当时的请求形态。
    """
    name: str = ""
    sessionid: str = ""
    deviceid: str = DEFAULT_UDID
    user_agent: str = DEFAULT_UA
    uid: int | None = None
    nickname: str = ""
    mobile: str = ""
    version: int = 1
    session_version: int = 1
    profile_version: int = 1
    profile_updated_at: float = 0
    legacy_names: list = field(default_factory=list)

    @property
    def account_id(self) -> str:
        from accountguard import normalized_uid
        return normalized_uid(self.uid)

    @property
    def credential_lock(self):
        return self.__dict__.setdefault("_credential_lock", threading.RLock())

    def to_dict(self) -> dict:
        value = asdict(self)
        value["account_id"] = self.account_id
        return value

    @staticmethod
    def from_dict(d: dict) -> "Account":
        if d and d.get("account_id"):
            from accountguard import normalized_uid, AccountConflict
            if normalized_uid(d["account_id"]) != normalized_uid(d.get("uid")):
                raise AccountConflict("账号 ID 与 UID 不一致")
        known = {f for f in Account.__dataclass_fields__}
        return Account(**{k: v for k, v in (d or {}).items() if k in known})


# ------------------------------------------------------------------ 客户端
class SveClient:
    """SVE 接口客户端。所有请求都走这里，便于统一处理签名与错误。"""

    def __init__(self, account: Account | None = None, base: str = BASE):
        self.base = base.rstrip("/")
        self.account = account

    # ---- 底层请求 ----
    def _headers(self, sign_body: dict | None, with_session: bool = True) -> dict:
        acc = self.account
        udid = acc.deviceid if acc else DEFAULT_UDID
        h = {
            "content-type": "application/json",
            "deviceid": udid,
            "platform": "android",
            "User-Agent": acc.user_agent if acc else DEFAULT_UA,
            "sign": generate_sign(sign_body or {}, udid=udid),
        }
        if with_session and acc and acc.sessionid:
            h["sessionid"] = acc.sessionid
        return h

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None, with_session: bool = True) -> dict:
        if self.account is None:
            return self._request_inner(method, path, params=params, body=body, with_session=with_session)
        with self.account.credential_lock:
            if self.account.__dict__.get("_retired"):
                code = "identity_mismatch" if self.account.__dict__.get("_identity_blocked") else "account_deleted"
                raise SveError("账号已删除或身份未确认，拒绝继续使用旧会话", code=code)
            if with_session and path != "/urs/info":
                self.verify_identity()
            return self._request_inner(method, path, params=params, body=body, with_session=with_session)

    def verify_identity(self):
        """Warm up migrated credentials once; never trust a cached UID for a POST."""
        from accountguard import normalized_uid, AccountConflict
        if self.account is None:
            raise SveError("缺少明确账号 ID", code="identity_mismatch")
        with self.account.credential_lock:
            try:
                aid = self.account.account_id
            except AccountConflict:
                raise SveError("账号 UID 无效，不执行请求", code="identity_mismatch") from None
            fingerprint = (self.account.sessionid, self.account.deviceid, aid)
            if self.account.__dict__.get("_verified_identity") != fingerprint:
                info = self.info()
                try:
                    verified = normalized_uid(info.get("id")) == aid
                except AccountConflict:
                    verified = False
                if not verified:
                    self.account._identity_blocked = True
                    self.account._retired = True
                    raise SveError("会话的官方 UID 与记录不符，已阻止请求；请核对身份并重新登录", code="identity_mismatch")
                self.account._verified_identity = fingerprint
                self.account._verified_official_uid = info["id"]
            return self.account.__dict__["_verified_official_uid"]

    def _request_inner(self, method: str, path: str, *, params=None, body=None, with_session=True) -> dict:
        url = self.base + path
        # list 等接口要把 query 并入签名内容；detail 的 body 为空
        sign_src = dict(params or {})
        if body:
            sign_src.update(body)
        headers = self._headers(sign_src, with_session=with_session)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=TIMEOUT)
            raw = r.read().decode("utf-8", "replace")
            code = r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            code = e.code
        except Exception as e:
            raise SveError("网络请求失败: %s" % e)

        try:
            d = json.loads(raw)
        except Exception:
            raise SveError("响应不是 JSON", http=code, detail=raw[:200])

        if code != 200 or d.get("status") != "success":
            raise SveError(d.get("msg_cn") or d.get("msg") or ("HTTP %s" % code),
                           code=d.get("code"), detail=d.get("detail"), http=code)
        return d

    # ---- 接口 ----
    def info(self) -> dict:
        """当前账号资料。"""
        return self._request("GET", "/urs/info").get("item", {})

    def tournaments(self, area: str, size: int = 100, page: int = 1,
                    is_sv2: int = 0) -> list[dict]:
        """按地区列出赛事。

        注意（实测）：列表返回的 zh_user_status 是"你与该赛事的关系"，
        但 **player_num 为 None**，rule_config.max_player_number 只是规则模板值。
        要准确名额必须再调 detail。
        """
        d = self._request("GET", "/urs/tournament/list", params={
            "_size": size, "_page": page, "area": area, "is_sv2": is_sv2,
        })
        return d.get("items", [])

    def my_tournaments(self, size: int = 200, page: int = 1) -> list[dict]:
        """我的赛事（原 APP "我的"→"我的赛事"用的就是这条）。

        关键（实测）：必须带 `mine=1`，这是**服务端筛选**，返回该账号参与过的
        全部赛事（含已结束/已取消的历史）。
        其他试过的参数（user_status / only_mine / is_mine / user_only / uid…）
        服务端**全部忽略**，返回结果与不筛选时一致——别被它们误导。
        另外：不带 `area` 时返回的是跨地区数据，无需逐地区扫描。
        """
        d = self._request("GET", "/urs/tournament/list", params={
            "_size": size, "_page": page, "mine": 1,
        })
        return d.get("items", [])

    def detail(self, tournament_id) -> dict:
        """赛事详情（含准确名额与报名时段字段）。"""
        return self._request("GET", "/urs/tournament/detail/%s" % tournament_id).get("item", {})

    def action(self, tournament_id, act: str, uid: int | None = None) -> dict:
        """提交动作：signup（报名）/ cancel（取消）/ book（预约）。"""
        from accountguard import normalized_uid
        if self.account is None:
            raise SveError("提交动作需要明确账号 ID", code="identity_mismatch")
        with self.account.credential_lock:
            if uid is not None and normalized_uid(uid) != self.account.account_id:
                raise SveError("请求 UID 与账号 ID 不一致", code="identity_mismatch")
            if self.account.__dict__.get("_retired"):
                raise SveError("账号已删除或待核对，拒绝提交动作", code="identity_mismatch")
            official_uid = self.verify_identity()
            body = {"uid": official_uid, "tournament_id": str(tournament_id), "action": act}
            return self._request("POST", "/urs/tournament/action", body=body)

    # ---- 便捷方法 ----
    def signup(self, tid) -> dict:
        return self.action(tid, "signup")

    def cancel(self, tid) -> dict:
        return self.action(tid, "cancel")

    def book(self, tid) -> dict:
        return self.action(tid, "book")

    def is_available(self, tid) -> tuple[bool, dict]:
        """是否还有空位（用 detail，因为 list 不给准确名额）。"""
        it = self.detail(tid)
        pn, mx = it.get("player_num"), it.get("max_player_number")
        ok = isinstance(pn, int) and isinstance(mx, int) and pn < mx
        return ok, it

    # ---- 登录 ----
    @staticmethod
    def login_with_cookie(cookies: str | dict, udid: str = DEFAULT_UDID,
                          app_ver: str = "1.2.0") -> str:
        """用网易账号 Cookie 换 SVE 的 sessionid。

        实测：该接口**不校验 sign、不校验 sessionid**，唯一要求是 cookie。
        cookies 可以是 Cookie 头字符串，或 {name: value} 字典。
        """
        if isinstance(cookies, dict):
            cookie = "; ".join("%s=%s" % (k, v) for k, v in cookies.items())
        else:
            cookie = cookies
        body = json.dumps({"app_ver": app_ver, "udid": udid},
                          separators=(",", ":")).encode()
        req = urllib.request.Request(
            BASE + "/urs/login", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": DEFAULT_UA,
                     "Cookie": cookie})
        try:
            r = urllib.request.urlopen(req, timeout=TIMEOUT)
            raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                d = json.loads(raw)
                raise SveError(d.get("detail") or d.get("msg_cn") or "登录失败",
                               code=d.get("code"), http=e.code)
            except SveError:
                raise
            except Exception:
                raise SveError("登录失败 HTTP %s" % e.code, detail=raw[:200])
        d = json.loads(raw)
        sid = (d.get("item") or {}).get("session_id")
        if not sid:
            raise SveError("响应中没有 session_id", detail=raw[:200])
        return sid


# ------------------------------------------------------------------ 赛事状态
# 列表/详情里的 zh_user_status 取值（实测见 capture/client_recon.md）
STATUS_ACTIONS = {
    "可报名": "signup",
    "可预约": "book",
    "已预约": "cancel",
    "已报名": None,          # 已报名无需操作
    "已报满": None,          # 需等空位
    # 补录：见 is_signable_status()。**不要**在这里写确切的完整文案——
    # 实测采样 5546 场都没抓到过它，说明很罕见，措辞无法穷举。
    "（含补录字样的状态）": "signup",
}

# ------------------------------------------------------------------ 可提交报名判定
# 「可报名」是常规的可提交状态。但**有一类状态同样可以提交**：
# 赛事处于「签到 → 开赛」之间时，服务端允许**补录**——此时状态不再是
# 「可报名」。官方 App 把该状态显示为「可补录」。
#
# 为什么用**包含匹配**而不是精确相等：
#   * 实测采样 5546 场（15 个地区）都**没抓到过**这个状态，无法穷举确切文案；
#   * 它很可能带前缀/后缀，例如「签到中-可补录」「可补录」「补录中」。
# 这与项目里「已取消」（`"取消" in s`）的既有防御风格一致。
#
# ⚠️ 安全边界：这些关键词只用于回答"**能不能提交**"这一个问题。
#    它**不得**让「已报名」「已结束」「已报满」等状态变成可提交——
#    所以判定顺序与范围都很窄，见 is_signable_status()。
SIGNABLE_EXACT = ("可报名",)
SIGNABLE_HINTS = ("补录",)


def is_signable_status(status: str) -> bool:
    """该状态是否**可以提交报名/补录**。

    只认两类：
      1. 精确等于「可报名」
      2. 含「补录」字样（措辞不定，见上方说明）

    **不应**返回 True 的状态：已报名 / 已预约 / 已报满 / 已结束 / 已取消 /
    赛事中-未报名 / 签到中-未报名 / 其它未知状态。
    """
    s = status or ""
    if s in SIGNABLE_EXACT:
        return True
    return any(h in s for h in SIGNABLE_HINTS)


def is_makeup_status(status: str) -> bool:
    """是否属于「**补录**」类状态。

    与 is_signable_status() 的区别很关键，两者**不可混用**：

      * 普通「可报名」**不代表窗口已到点**——每日时段型赛事（如大奖赛
        16:00~23:59）在窗口外**同样**返回「可报名」，此时提交必被上游以
        "非报名时间"拒绝。所以窗口关闭时**不能**因为状态是可报名就放行。
      * 「补录」则相反：它本身就意味着**此刻**服务端允许补报（赛事处于
        签到→开赛之间），与常规报名窗口是否开放无关。此时窗口判定应当让位。

    所以：窗口门控只应被 is_makeup_status()/is_makeup_opportunity() 放行，
    **不能**被 is_signable_status() 放行。
    """
    return any(h in (status or "") for h in SIGNABLE_HINTS)


def is_already_in_status(status: str) -> bool:
    """该账号是否**已经在这场赛事里**（已有报名记录，无需也不能再提交）。

    两类取值都算「已在」：

      * 「已报名」——常规的已报名状态。
      * 「签到中」——**已报名**的账号进入签到阶段后的取值。

    ⚠️ 最后一条是**实测结论，不是推测**（2026-10-16，赛事 34043「校园积分赛——
    华南理工大学」，9 个账号在**同一时刻**的 detail 与参与记录对照）：

        显示「签到中」       的 5 个账号 → 参与记录里**全部在册**
        显示「签到中-未报名」 的 4 个账号 → 参与记录里**全部不在册**

    所以「签到中」与「签到中-未报名」是**同一赛事对不同账号**的取值差异：
    前者"已报上名、正在签到"，后者才是"进了签到阶段但没报上"（即可补录的那种）。

    这与 is_signable_status() 是**互斥的语义**：本函数为 True 的状态绝不能提交，
    否则会拿已经报上名的账号去重复报名。
    """
    s = status or ""
    if s == "已报名":
        return True
    return s.startswith("签到") and "未报名" not in s and not is_makeup_status(s)


def _ts_sec(v):
    """毫秒时间戳 → 秒。非正数/非数值返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v > 0:
        return v / 1000.0 if v > 1e11 else float(v)
    return None


def is_makeup_opportunity(item: dict, now: datetime | None = None) -> bool:
    """赛事是否处于「**可补录**」的机会窗口。

    ## 依据（实测，2026-10-16）

    在「签到 → 开赛」之间，服务端返回的 `zh_user_status` 是
    **「签到中-未报名」**，而官方 App 把这个状态**显示为「可补录」**，
    并且此时**仍然可以报名**。

      实测采样：9 个账号 × 8 个地区，列表 + 详情 + 参与记录共约 1.4 万次，
      「补录」二字在 API 返回值里**一次都没出现过**；该窗口内的实际取值
      就是「签到中-未报名」（例：赛事 34182，checkin_time 与 start_time
      相隔 1 小时）。所以「可补录」是**界面叫法**，不是 API 状态。

    ## 判定 = 状态 + 余位 + 时间，三者缺一不可

      1. 状态属于「签到阶段且**未报名**」；
      2. 仍**有余位**（player_num < max_player_number）；
      3. 时刻落在 **[checkin_time, end_time)** 之内。

    **为什么必须要求「未报名」**：「签到中」这个少了后缀的取值表示
    **该账号已报名**（实测 34043：5 个「签到中」账号全部在参与记录里）。
    所以这里用 is_already_in_status() 明确排除，而不是按 `startswith("签到")`
    泛匹配——否则会拿已经报上名的账号去重复提交。

    **上界为什么是 end_time 而不是 start_time**（2026-10-16 修正）：
    签到阶段会**跨过开赛时刻继续存在**——实测 34043 在开赛（19:00）之后
    **38 分钟**仍返回「签到中-未报名」、仍有余位，官方 App 此时仍提供补录。
    用 `start_time` 作上界会在开赛瞬间失效，导致真实场景报不上（用户实测的失败）。
    `end_time` 是服务端给的真实赛程结束字段（40 场样本里只有 2 场恰好是 23:59，
    其余是 20:00 / 22:35 / 23:16 / 次日 10:00 这类真实值），赛事结束后
    状态会变成「已结束」，两者互为保险。

    `item` 可以是列表项或详情项；详情项字段最全（player_num 与时间字段需 detail 才有）。
    """
    s = item.get("zh_user_status") or item.get("status") or ""
    if any(h in s for h in SIGNABLE_HINTS):
        return True                      # 明确含「补录」字样，直接放行
    if not s.startswith("签到") or "未报名" not in s:
        return False                     # 只覆盖"签到阶段且未报名"
    if is_already_in_status(s):
        return False                     # 双保险：已报名的账号绝不重复提交
    pn, mx = item.get("player_num"), item.get("max_player_number")
    if not (isinstance(pn, int) and isinstance(mx, int)) or pn >= mx:
        return False                     # 必须仍有余位
    now = now or datetime.now()
    t = now.timestamp()
    ck = _ts_sec(item.get("checkin_time"))
    st = _ts_sec(item.get("start_time"))
    et = _ts_sec(item.get("end_time"))
    if ck is not None and t < ck:
        return False                     # 还没开始签到
    # 上界：优先 end_time（真实赛程结束）；缺字段时退回 start_time（保守）。
    upper = et if et is not None else st
    if upper is not None and t >= upper:
        return False
    return True


def is_signable_item(item: dict, now: datetime | None = None) -> bool:
    """这份**赛事数据**当前是否可提交报名/补录。

    与仅看状态字符串的 is_signable_status() 的区别：本函数还能识别
    「签到中-未报名 + 仍在签到窗口 + 有余位」这种补录机会，
    但它需要 item 里的时间与名额字段（列表项可能缺 player_num，详情项最全）。
    """
    return is_signable_status(item.get("zh_user_status")) or is_makeup_opportunity(item, now)


def classify_status(status: str) -> str:
    """把状态归类，便于界面筛选与调度决策。"""
    s = status or ""
    # ⚠️ 顺序关键：必须在 `startswith("签到")` **之前**判「补录」。
    # 否则「签到中-可补录」会先被归成 checkin，界面就不会给「立即报名」按钮，
    # 调度器也不会尝试提交——这正是本次要修的缺陷。
    if is_signable_status(s):
        return "open"
    if s in ("可预约",):
        return "bookable"
    # 「签到中」（**无**「-未报名」后缀）表示该账号**已报名**、正在签到，
    # 所以归入「已报名」组而不是 checkin——判定与 is_already_in_status() 同一份。
    # ⚠️ 顺序关键：必须在下面的 `startswith("签到")` **之前**，
    #    否则它会先被归成 checkin，界面把它当"可处理"的赛事列出来。
    #    （「签到中-未报名」不含本分支，仍归 checkin，由补录机会决定是否可操作。）
    if s == "已预约" or is_already_in_status(s):
        return "mine"
    if s == "已报满":
        return "full"
    # 已取消：实测在抽样的 800 场里没出现，说明它很罕见。
    # 这里按"含取消字样"防御性识别，避免因为取值不同（如"已取消-未报名"）而漏掉。
    if "取消" in s:
        return "cancelled"
    if s.startswith("签到"):
        return "checkin"
    if s.startswith("赛事中"):
        return "running"
    if s == "已结束":
        return "ended"
    return "other"


# 界面上默认隐藏的状态（无操作价值或已失效）
HIDDEN_BY_DEFAULT = ("ended", "cancelled")


# ------------------------------------------------------------------ 赛事类别
# 依据列表/详情里的 rule_config.rule_name（实测取值）：
#     大型赛（先到先得）/ 大型赛（抽选） / 中型赛 / 城市赛特别赛 / 积分赛
# 用户要求的展示顺序：大型赛·中型赛·特别赛 → 积分赛 → 娱乐赛
CLASS_ORDER = {
    "major": 0,      # 大型赛 / 中型赛 / 特别赛
    "points": 1,     # 积分赛
    "casual": 2,     # 娱乐赛
    "other": 9,
}
CLASS_LABEL = {
    "major": "大型/中型/特别赛",
    "points": "积分赛",
    "casual": "娱乐赛",
    "other": "其它",
}


def classify_class(rule_name: str) -> str:
    """把 rule_config.rule_name 归到展示用的类别。"""
    rn = rule_name or ""
    if any(k in rn for k in ("大型赛", "中型赛", "特别赛")):
        return "major"
    if "积分赛" in rn:
        return "points"
    if "娱乐赛" in rn:
        return "casual"
    return "other"


if __name__ == "__main__":
    # 自检：签名算法与既有样本比对（见 signer.py 的三条实测样本）
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    body = {"uid": 10001, "tournament_id": "33933", "action": "signup"}
    print("  sign(signup) =", generate_sign(body))
    print("  payload      =", sign_payload(body))
    print()
    print("  与 scripts/poller/signer.py 的样本应完全一致（同一算法）。")
