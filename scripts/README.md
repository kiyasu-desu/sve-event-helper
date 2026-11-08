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

## runtime/ 与现役共享模块

- `runtime/watchdog.py --once`：代理进度看护，直接读取 RuntimePaths 与通知配置。
- `app/client/core/notification_sender.py`：代理、看护、显式部署通知诊断共用的通知发送器。
- `poller/watchdog.py --once --agent`：既有 unit 临时兼容转发，与新入口共用状态、写锁和日志。
- `poller/notify.py`：临时导入别名，模块对象与现役发送器一致。
- `probe/signer.py`：开发签名工具，不进入生产包；旧 signer 路径仅为开发导入别名。

旧 poll/action/autosignup/session_manager 和 probe_action 无条件拒绝导入和启动，包括 help、单次与直连调用；旧 PowerShell 控制器同样退出。旧实现通过 Git 历史追溯。新包不包含旧执行实现、签名工具或 poller unit。兼容入口在所有私人 unit/工具迁移并验证后移除。见 [M20](../deploy/M20-retire-legacy-poller.md)。

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
| `probe_api.py` | 只读开发接口探测 |
| `test_autosignup_spin.py` | 转发现役调度器回归 |
| `test_autosignup_confirm.py` | 转发现役调度器回归 |

窗口、复核与状态变化场景维护在 `tests/test_m20_retirement.py`；去重回归维护在 `tests/test_m06_watchdog.py`。

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
已经因此误判过两次。现役看护使用 WriterLock，代理使用进度协议检查真实线程。

### 5. 后台进程不要用后台作业(background job)启动

作业被取消或会话清理时会把子进程一起杀掉（表现为"以为在看护、其实早死了"）。
生产看护由 systemd timer 托管，Windows 不承担常驻看护。

### 6. 改完脚本请跑自检

```powershell
pwsh -NoProfile -File scripts\selfcheck.ps1          # 全量自检（推荐）
python scripts\recon\check_ps1.py                    # 只查 PowerShell
<venv>\python.exe scripts\recon\check_scripts.py     # 只查 Python
```

### 7. 测试必须把日志指到独立文件

调度器使用模拟客户端与回调；看护、部署回归使用 TemporaryDirectory 和通知替身。测试前设置隔离的 SVE_DATA_DIR/SVE_SETTINGS_FILE，禁止把日志、状态或备份写入生产目录。真实导入检查跳过旧 poller 目录和测试模块，旧测试命令只转发到现役回归。
