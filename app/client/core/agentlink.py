"""本机客户端 ↔ 服务器自动报名代理的连接层。

设计要点：
  * **SSH 隧道**：代理只监听服务器的 127.0.0.1，本机通过 `ssh -N -L` 打通。
    服务器不开放任何公网端口，也不需要额外的认证层。
  * **隧道自愈**：ssh 进程意外退出时（断网、休眠唤醒）自动重启，
    否则界面会"看着正常但所有操作都超时"。
  * **账号推送**：本机明确登录后仅提交单个会话，由服务器校验身份与前置版本；
    启动/重连只读拉取摘要，不发送离线账号整表。

不依赖第三方库，只用标准库（urllib + subprocess），方便在两边复用。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_REMOTE_PORT = 8901     # 服务器代理监听的端口
DEFAULT_LOCAL_PORT = 8901      # 本机隧道映射到的端口
SSH_CONNECT_TIMEOUT = 12


def _ssh_exe() -> str:
    return shutil.which("ssh") or "ssh"


def _list_ssh_pids() -> list[tuple[int, str]]:
    """列出 ssh 进程的 (pid, commandline)。仅用于清理自己遗留的隧道。"""
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
                  "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=25,
                                 encoding="utf-8", errors="replace").stdout or ""
        else:
            out = subprocess.run(["pgrep", "-af", "ssh"], capture_output=True, text=True,
                                 timeout=15, encoding="utf-8", errors="replace").stdout or ""
    except Exception:
        return []
    res = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1) if "\t" in line else line.split(" ", 1)
        if len(parts) != 2:
            continue
        try:
            res.append((int(parts[0]), parts[1]))
        except ValueError:
            continue
    return res


def kill_stale_tunnels(user: str, host: str, local_port: int, remote_port: int,
                       logger=None) -> int:
    """清掉"上一次客户端进程遗留"的同一条隧道。

    为什么需要：Windows 上父进程被杀时**不会**连带终止子进程，
    所以客户端每重启一次就会多留一个 ssh.exe。实测跑了几轮后攒到 5 个，
    其中还有两个同时绑在同一个本地端口上（功能没坏，但这是确定的泄漏）。

    匹配条件刻意写得很窄——必须**同时**包含
        -L <本地端口>:127.0.0.1:<远端端口>
        且 <用户>@<主机>
    只可能命中我们自己创建的隧道，不会误伤用户其它 ssh 会话。
    （教训：以前用宽泛的进程名/关键字匹配杀进程，误杀过用户的 Edge 和轮询器。）
    """
    needle_fwd = "-L %d:127.0.0.1:%d" % (local_port, remote_port)
    needle_dst = "%s@%s" % (user, host)
    killed = 0
    for pid, cmd in _list_ssh_pids():
        if needle_fwd in cmd and needle_dst in cmd:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, timeout=15)
                else:
                    os.kill(pid, 15)
                killed += 1
                if logger:
                    logger("清理遗留隧道 pid=%d" % pid)
            except Exception:
                pass
    return killed


class AgentLink:
    """访问代理 HTTP 接口（经隧道）。"""

    def __init__(self, base: str = "http://127.0.0.1:%d" % DEFAULT_LOCAL_PORT,
                 timeout: float = 25.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None,
             timeout: float | None = None) -> dict:
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except Exception:
                return {"ok": False, "error": "HTTP %s: %s" % (e.code, raw[:200])}
        except Exception as e:
            return {"ok": False, "error": "无法连接代理（%s）：%s" % (self.base, e)}
        try:
            return json.loads(raw)
        except Exception:
            return {"ok": False, "error": "响应不是 JSON: %s" % raw[:200]}

    def state(self, account_id=None) -> dict:
        return self.endpoint("GET", "/api/state", params={"account_id": account_id} if account_id else {})

    def endpoint(self, method, path, body=None, params=None):
        if params:
            from urllib.parse import urlencode
            path += "?" + urlencode(params)
        return self._req(method, path, body, timeout=90)

    def push_accounts(self, accounts: dict, active: str | None) -> dict:
        """Deprecated: never transmit a cached table, even to an old server."""
        return {"ok": False, "code": "legacy_sync_disabled",
                "error": "整表推送已禁用；请明确重新登录后提交单个会话"}

    def accounts_summary(self) -> dict:
        return self._req("GET", "/api/accounts/summary")

    def save_session(self, account: dict, expected_revision, *, operation_id, restore=False) -> dict:
        return self._req("POST", "/api/accounts/session", {
            "account": account, "expected_version": expected_revision,
            "operation_id": operation_id, "restore": restore,
        }, timeout=60)

    def subscribe(self, tid, account: str | None = None, *, expected_uid=None) -> dict:
        b = {"tournament_id": str(tid)}
        if account:
            b["account_id"] = account
        if expected_uid is not None:
            b["expected_uid"] = expected_uid
        return self._req("POST", "/api/subscribe", b, timeout=90)

    def unsubscribe(self, tid, account: str | None = None, *, expected_uid=None) -> dict:
        b = {"tournament_id": str(tid)}
        if account:
            b["account_id"] = account
        if expected_uid is not None:
            b["expected_uid"] = expected_uid
        return self._req("POST", "/api/unsubscribe", b, timeout=40)

    def detail(self, tid) -> dict:
        return self._req("GET", "/api/detail?tournament_id=%s" % tid, timeout=60)

    def health(self) -> tuple[bool, str]:
        d = self._req("GET", "/api/state", timeout=8)
        if d.get("ok") is False and "error" in d:
            return False, d["error"]
        return True, "代理在线，账号 %d 个，订阅 %d 条" % (
            len(d.get("accounts") or []), d.get("sub_count") or 0)


class Tunnel:
    """维护一条 ssh -L 本地转发；进程退出时自动重启。"""

    def __init__(self, host: str, user: str, key: str | None = None,
                 remote_port: int = DEFAULT_REMOTE_PORT,
                 local_port: int = DEFAULT_LOCAL_PORT,
                 ssh_port: int = 22, logger=None):
        self.host = host
        self.user = user
        self.key = key
        self.remote_port = remote_port
        self.local_port = local_port
        self.ssh_port = ssh_port
        self.log = logger or (lambda m: None)
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._process_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_error = ""

    # ---- 命令行 ----
    def _argv(self) -> list[str]:
        args = [_ssh_exe(), "-N",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=%d" % SSH_CONNECT_TIMEOUT,
                "-L", "%d:127.0.0.1:%d" % (self.local_port, self.remote_port)]
        if self.ssh_port and self.ssh_port != 22:
            args += ["-p", str(self.ssh_port)]
        if self.key:
            args += ["-i", self.key]
        args.append("%s@%s" % (self.user, self.host))
        return args

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # 先清掉上次进程遗留的同一条隧道，避免 ssh.exe 越攒越多
        kill_stale_tunnels(self.user, self.host, self.local_port, self.remote_port,
                           logger=self.log)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="ssh-tunnel")
        self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._process_lock:
                    if self._stop.is_set():
                        break
                    self.proc = subprocess.Popen(
                        self._argv(),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        creationflags=(subprocess.CREATE_NO_WINDOW
                                       if os.name == "nt" else 0))
                self.log("隧道已启动：本地 %d → %s:%d" %
                         (self.local_port, self.host, self.remote_port))
                self._last_error = ""
                # 阻塞等它退出（正常情况会一直挂着）
                out, _ = self.proc.communicate()
                if out:
                    self._last_error = out.strip()[:300]
            except Exception as e:
                self._last_error = "%s: %s" % (type(e).__name__, e)
                self.log("隧道启动失败：%s" % self._last_error)
            # If logging/communication failed while SSH is still alive, retain
            # ownership so stop() can drain it before any replacement is built.
            with self._process_lock:
                if self.proc is not None and self.proc.poll() is None:
                    break
                self.proc = None
            if self._stop.is_set():
                break
            self.log("隧道中断，3 秒后重连…（%s）" % (self._last_error or "进程退出"))
            self._stop.wait(3)

    def stop(self) -> None:
        self._stop.set()
        with self._process_lock:
            p = self.proc
        if p:
            try:
                p.terminate()
            except OSError:
                if p.poll() is None:
                    raise
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        if self._thread is not None:
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                raise RuntimeError("SSH tunnel worker has not exited")
        self.proc = None

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def status(self) -> dict:
        return {"running": self.running,
                "local_port": self.local_port,
                "remote_port": self.remote_port,
                "host": self.host, "user": self.user,
                "last_error": self._last_error}
