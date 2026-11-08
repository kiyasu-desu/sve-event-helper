# scripts/ 目录说明

本目录脚本的职责、状态与约定。**先读这份再改脚本**，能避免重复踩坑。

## 运行状态标注

| 标注 | 含义 |
|---|---|
| **核心** | 日常监控依赖，改动需谨慎 |
| 工具 | 按需使用（抓包、诊断） |
| 已废弃 | 保留供参考，不再维护；不要在核心流程里调用 |
| 一次性 | 逆向阶段的侦察片段，已完成使命，留作证据 |

---

## M01 身份与迁移

`identity_migrate.py` 默认只读预览；离线应用/恢复需要明确停止所有写入者。详见 [M01 说明](../deploy/M01-account-identity.md)。`sveenv.load_session(account_id)` 仅按 UID 数据集读取，未知 ID 不回退到其他账号；旧脚本不能写入新身份数据，也不能启动第二报名执行点。

当前回归使用 `tests/test_m00_data_protection.py`（同时覆盖 M00/M01）与 `tests/test_identity_ui.js`。历史自检的真实导入、`--help`、通知探测可能有副作用，需先审阅并隔离路径，不能将其视为生产只读检查。

## poller/ —— 旧轮询器（大部分已退役）与少量共享模块

> **当前架构不是这个目录。** 现在是「本机客户端（`app/client/`，Web 界面）+
> 服务器代理（`sve-agent`）+ 订阅体系」，见仓库根 `README.md`。
> 盯哪场赛事由界面里的**订阅**决定，不再由轮询器的配置指定。
>
> 本目录里只有两个文件仍在运行链路中：**`notify.py`**（代理发通知时导入）
> 与 **`watchdog.py`**（`sve-watchdog.timer` 调用）。其余属于单账号单赛事的
> 旧方案，已退役但保留，供对照与复用。

| 文件 | 状态 | 说明 |
|---|---|---|
| `notify.py` | **核心（仍在用）** | 双通道通知（PushPlus + 邮件）。**服务器代理会导入它**，改动需谨慎 |
| `watchdog.py` | **核心（仍在用）** | 看护的 Python 版，由 `sve-watchdog.timer` 调用（`--agent` 模式检查代理） |
| `signer.py` | 工具（仍被调用） | 签名算法。**代理走的是 `app/client/core/sveapi.py`**；此文件供 `session_manager.py` 与 `probe/` 使用 |
| `poll.py` | 已退役 | 常驻轮询器（单赛事单账号）。原为"核心"，大奖赛纳入订阅体系后不再运行 |
| `autosignup.py` | 已退役 | 自动报名的保护链（白名单、赛事名校验、自旋刷新、两段复核）。**其设计已被代理的调度器吸收** |
| `session_manager.py` | 已退役 | 会话校验与自动恢复（读 APP 的 AsyncStorage / Frida） |
| `action.py` | 已退役 | 手动报名/取消/查状态（`--status` / `--signup` / `--cancel`） |
| `watchdog.ps1` | 已退役 | 看护的 PowerShell 版（已被 `watchdog.py` 取代） |
| `watchdog_ctl.ps1` | 已退役 | 看护的启停（Windows 侧） |
| `test_window.ps1` | 工具 | 报名时段判定的回归测试（13 个边界用例，含跨零点） |

### 轮询器的单实例锁（仅对已退役的 poll.py）

`poll.py` 常驻模式会写 `capture/poller.pid`；已有存活实例时**拒绝启动**（exit 4）。
`--once` 不加锁（一次性排查允许并发）。需要强制再起一个用 `--force`。

> 为什么加这个：以前靠"命令行里是否含 `poll.py`"来判断实例数，
> 结果把启动器包装进程当成第二个实例，还误杀了真正在跑的守护进程。
> 让程序自己声明"我在跑"远比外部猜测可靠。

> ⚠️ **唯一执行点**：`sve-agent` 与 `sve-poller` 同一时间只能有一个在跑，
> 否则会对同一场赛事重复提交。`deploy/verify.sh` 有专门一节断言这一点。

---

## capture/ —— 抓包（工具）

**方案：Frida 在 APP 内 hook OkHttp**，APP 保持直连，不需要代理和证书。

| 文件 | 状态 | 说明 |
|---|---|---|
| `netcap.ps1` | 工具 | 一键启动抓包。`-Target sve` / `-Target sve2`，各用独立端口与产物文件 |
| `netcap.js` | 工具 | Frida payload 源码（hook OkHttp 链 + 23 个请求体钩子） |
| `netcap_run.py` | 工具 | 连接 frida、注入 payload、把消息写入 jsonl |
| `build_payload.js` | 工具 | 用 esbuild 把 `netcap.js` 打成单文件 bundle |
| `frida_run.py` | 工具 | 早期通用 frida 启动器 |
| `probe_device.js` | 工具 | 设备侧探针 |
| `ca_hash.py` | 工具 | 计算证书 `subject_hash_old`（签名已还原，通常用不到） |
| `env_up.ps1` | **已废弃** | HTTP 代理方案的环境搭建 |
| `mitm_ctl.ps1` / `mitmdump.cmd` | **已废弃** | mitmproxy 启停 |
| `sve_capture.py` | **已废弃** | mitmproxy 插件 |
| `payload.js` | **已废弃** | 早期 hook 脚本 |

**为什么放弃代理方案**：hook 丢失时 APP 的所有 HTTPS 请求都会因证书校验失败而中断，
表现为页面卡死、`暂无数据`、`SntpClient: requestTimeout`。改用进程内 hook 后无需代理。

---

## probe/ —— 逆向期的验证脚本（一次性）

| 文件 | 说明 |
|---|---|
| `list_sign.py` | **穷举反推 list 接口的签名内容**（11 种候选，唯一命中） |
| `verify_sign.py` / `bruteforce_sign.py` | 签名算法的早期验证 |
| `probe_api.py` / `probe_action.py` | 接口探测 |
| `test_autosignup_spin.py` | 自旋刷新逻辑的单元测试（4 场景，不触网） |
| `test_autosignup_confirm.py` | 两次复核流程的单元测试（6 场景，不触网） |

后两个是**有回归价值的测试**，改动 `autosignup.py` 后应重跑。

---

## recon/ —— 静态分析与工程检查（一次性 + 工具）

**逆向期的一次性片段**（模块级即执行，依赖 `sys.argv`，不要当模块导入）：
`extract_strings.py`、`analyze_strings.py`、`manifest.py`、`cjk16.py`、
`find_sign.py`、`camel.py`、`fields.py`、`paths.py`、`context_strings.py`、`probe.py`

**工程检查工具**（可反复运行，建议改完代码就跑）：

| 文件 | 说明 |
|---|---|
| `check_scripts.py` | Python 分层验证：py_compile → 真实导入 → CLI `--help`。**必须用 venv 解释器**（需要 frida） |
| `check_ps1.py` | PowerShell 语法检查，**PS7 与 PS5.1 双引擎** |
| `audit_paths.py` | 扫描是否有对工作区之外的写操作 |

---

## device/ —— 设备操作（工具）

| 文件 | 状态 | 说明 |
|---|---|---|
| `adb_helpers.ps1` | 工具 | adb 封装 |
| `set_proxy.ps1` | 工具 | 设置/清除设备 HTTP 代理（代理方案已废弃，保留备用） |
| `install_ca.ps1` | **已废弃** | 安装系统证书。本 MuMu 根分区只读 + dm-verity，`remount`/`disable-verity` 被剥离，**只能检测不能写入** |

---

## config.ps1 与 sveenv.py —— 配置的单一来源

PowerShell 侧用 `scripts/config.ps1`，Python 侧用 `scripts/sveenv.py`。
两者对应同一组设置，都支持**环境变量覆盖 + 自动发现**。

**为什么需要**：曾把 adb 路径硬编码在 4 个文件里，换机器就得改多处；
现在只在配置里写一次。

| 环境变量 | 作用 |
|---|---|
| `SVE_ADB` | adb 可执行文件路径 |
| `SVE_SERIAL` | 默认设备序列号 |
| `SVE_SVE_SERIAL` / `SVE_SVE2_SERIAL` / `SVE_PJSK_SERIAL` | 覆盖各实例端口 |

**实例约定**：

```
sve  = 127.0.0.1:16384   监控用（大奖赛报名监控跑在这里）
sve2 = 127.0.0.1:16448   第二账号（验证登录流程，不影响 sve 会话）
pjsk = 127.0.0.1:16416   其它用途，本项目不要碰
```

---

## 约定与注意事项

### 1. 含中文的 `.ps1` 必须保存为 **UTF-8 with BOM**

Windows PowerShell 5.1 以 **ANSI** 解析脚本文件：无 BOM 的 UTF-8 中文会被破坏，
导致语法错误（而且报错位置往往指向中文注释那一行，极具误导性）。

- **PS7** 默认按 UTF-8 读，两种编码都能跑；
- **PS5.1** 只有带 BOM 才能正确读中文。

所以：**含中文 → 存 BOM**。`check_ps1.py` 会同时用两个引擎验证，能立刻发现这类问题。

### 2. Python 文件用 UTF-8 无 BOM

Python 侧没有这个问题，统一无 BOM 即可（在源文件里声明 `encoding="utf-8"` 读取）。

### 3. 不要在脚本里硬编码路径

工具链路径（adb / python / node / openssl）与目录都从 `config.ps1` / `sveenv.py` 取，
需要新位置时加进候选列表，不要散落在各处。

### 4. 判断进程存活请用 pid 文件，不要匹配命令行

命令行匹配会把"你自己那条含关键字的诊断命令"和"启动器包装进程"一起匹配进来，
已经因此误判过两次。参考 `watchdog_ctl.ps1` 与 `poll.py` 的锁实现。

### 5. 后台进程不要用后台作业(background job)启动

作业被取消或会话清理时会把子进程一起杀掉（表现为"以为在看护、其实早死了"）。
用 `Start-Process` 独立启动 + pid 文件（见 `watchdog_ctl.ps1`）。

### 6. 改完脚本请跑自检

```powershell
pwsh -NoProfile -File scripts\selfcheck.ps1          # 全量自检（推荐）
python scripts\recon\check_ps1.py                    # 只查 PowerShell
<venv>\python.exe scripts\recon\check_scripts.py     # 只查 Python
```

### 7. 测试必须把日志指到独立文件

`autosignup.py` 的日志目标 `LOGFILE` 是**可运行时替换**的。任何单元测试都要改它：

```python
A.LOGFILE = os.path.join(PROJECT_ROOT, "vendor", "tmp", "test_poll.log")
```

否则测试输出会混进 `capture/poll.log`，看起来就像**生产环境发生了自动报名**——
曾经因为这件事误判过一次（日志里出现 `静默等待 0 秒`、`模拟抖动` 等测试特征）。

同理，`check_scripts.py` 的"真实导入"步骤会跳过 `test_*.py`：
测试模块的执行方式是**显式运行它们自己**，而不是被别人导入。
