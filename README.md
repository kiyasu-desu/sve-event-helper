# SVE 赛事助手 —— 通用赛事自动报名

《影之诗：进化对决》赛事的**通用自动报名系统**。

项目仓库：[sve-event-helper](https://github.com/kiyasu-desu/sve-event-helper)（私有，需 GitHub 登录及访问权限）。应用侧栏入口的发布来源及生产核对见 [M21 部署说明](deploy/README.md#m21-项目仓库入口)。

多账号、按地区浏览赛事、订阅后**自动抢位报名**；本机提供 Web 界面，服务器负责实际执行。
不再局限于某一场大奖赛——任何出现在赛事列表里的比赛都可以订阅。

```
┌─────────────────────────── 本机（Windows）───────────────────────────┐
│  客户端  app/client/server.py  (Flask)   http://127.0.0.1:8801       │
│          app/client/ui/*.html  ← 登录 / 浏览 / 订阅 / 我的赛事 / 通知 │
│          app/client/data/identity-v2.json  ← 账号与会话的单一来源        │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  SSH 隧道（本机 8901 → 服务器 8901）
                                │  电脑可走 SSH；手机沿用 HTTPS / Cloudflare Access
┌───────────────────────────────┴──────────────────────────────────────┐
│  服务器 同一个 server.py，以 systemd 服务 sve-agent 常驻              │
│         监听 127.0.0.1:8901                                          │
│         ├─ 按订阅逐场守护（分档轮询，见 §2）                          │
│         ├─ 两段复核后完成报名                                         │
│         ├─ 推送通知（PushPlus + 邮件）                                │
│         └─ 订阅持久化，重启后自动恢复                                 │
└──────────────────────────────────────────────────────────────────────┘
```

> **本机可以关机。** 已下发的订阅由服务器独立执行——调度器、会话、报名请求、
> 通知都在服务器侧。手机通过现有 HTTPS/认证入口直接使用服务器，普通操作不依赖 Windows 在线。
> （实测：本机客户端与隧道全部停止时，服务器仍能独立完成整条链路。）

---

生产升级采用指定 Git 提交的发布清单、停写切换和保留最新数据的代码回退，见 [部署流程](deploy/README.md)。本仓库使用独立初始历史导入已完成 M00–M19 的源码快照；源码导入或分支合并均不代表生产已部署。

## 1. 三个组成部分

| 组件 | 位置 | 职责 |
|---|---|---|
| **客户端** | `app/client/server.py` + `app/client/ui/` | 多账号登录、按地区浏览赛事、下单订阅、看「我的赛事」、配置通知、切换执行位置 |
| **服务器代理** | 同一份 `server.py`，systemd 服务 `sve-agent` | 实际执行订阅与自动报名；重启后从 `identity-v2.json` 的订阅集合恢复守护 |
| **SSH 隧道** | `app/client/core/agentlink.py` | 把本机 `8901` 映射到服务器 `127.0.0.1:8901`；自愈式重建 |

**执行位置可切换**：界面「自动报名执行位置」面板决定订阅是**在本机跑**还是**推给服务器跑**。
两者**只能有一个在跑**（见 §2「唯一执行点」）。

**与早期形态的区别**：项目最初只是"某场大奖赛的名额监控 + 人工报名"。
现在的核心是**订阅体系 + 全自动报名**：一次订阅，代理按报名窗口自动提速、自动提交、
自动复核，成功即通知并移出订阅列表——**全程无需人工**。

---

## 2. 订阅与自动报名

### 订阅体系

订阅按 `(账号, 赛事)` 维度记录，持久化在 `app/client/data/identity-v2.json` 的 subscriptions 中。
代理启动时调用 `restore_schedulers` 逐条恢复守护，因此**重启不丢订阅**。

同一场赛事可以**被多个账号各自订阅**（每个账号独立尝试），也可以随时取消。
报名成功后该条订阅**自动移出列表**（并写入已报名记录），不必手动清理。

### 分档轮询

以**窗口开放时刻 T** 为基准自动变频（每日窗口型取「每天的开放时刻」，
例如每天 16:00 开放的赛事，基准就是当天的 16:00）：

| 相对 T | 间隔 | 档位 |
|---|---|---|
| T 前 > 40 分钟 | 1800s | 低频待命 |
| T 前 40 ~ 6 分钟 | 300s | 正常 |
| T 前 6 ~ 2 分钟 | 15s | 加密 |
| **T 前后各 2 分钟** | **0.8s** | **高频抢位** |
| T 后 2 分钟起（窗口内） | 15s | 正常 |

档位参数可在 `config/settings.json` 调整（`idle_interval` / `warm_window_seconds` /
`warm_interval` / `near_window_seconds` / `poll_interval` / `hot_window_seconds` / `hot_interval`）。

两条关键性质（有断言测试）：

- **低频档会「夹住」剩余时间**，保证在进入下一档之前一定醒来，不会睡过提速时机；
- **各档单调**（离窗口越近越快），不会出现"离得近反而等更久"。

### 两段复核（判定报名是否真的成功）

```
窗口开启 → 分档轮询 → 发现可报名 → 提交报名
   └─ 立即复核
        ├─ 未通过 → 继续等下一次
        └─ 通过 → 静默 60 秒（此间不发任何通知）
             └─ 二次确认
                  ├─ 网络类错误 → 窗口内重试（不轰炸服务端）
                  ├─ 状态不对   → 继续等待
                  └─ 通过       → 通知「报名成功」+ 写记录 + 自动移出订阅
```

立即复核只能看出"这一枪有没有打中"，看不出"座位有没有坐稳"（服务端可能随后回滚）；
二次确认专门覆盖后者。**两段都通过才算任务完成**——所以收到通知就等于确实报上了。

### 通知

双通道：**PushPlus（微信）+ 邮件（SMTP）**。成功表示渠道接受请求，不能保证手机或邮箱终端实际送达。

**只在需要你知道时发**：报名成功（含"发现账号已是已报名"）、
看护判定代理不可达、会话失效。订阅本身、每轮尝试都不发通知。

> ⚠️ **通知配额**：PushPlus 免费版与 QQ 邮箱 SMTP 都有频率限制，
> 短时间连发会被限流。因此设计上**一次报名事件只发一条通知**，并在通道内做退避重试。

### 唯一执行点

同一场赛事只能有一个执行者在跑，否则会**重复提交**。
`sve-agent` 与已退役的 `sve-poller` 互斥，`deploy/verify.sh` 有专门一节断言这一点。

---

## 3. 签名算法（已还原并验证）

接口要求每个请求带合法 `sign`。算法从 APP 的 bundle 中还原：

```js
generateSign = function (data, extra, o) {
    var l = Object.assign({}, data, extra);
    l[o] = getSecretKey();          // o 是 udid 的值，被当成 key 名
    var u = Object.keys(l).sort().map(k =>
        k + '=' + (isObject(l[k]) || isArray(l[k]) ? JSON.stringify(l[k]) : l[k])
    ).join('');
    return SHA256(u).toString();
};
```

| 要素 | 说明 |
|---|---|
| 算法 | **SHA256**（64 位十六进制；不是 MD5） |
| 拼接 | 按 key 升序、`k=v`、**无分隔符** |
| 对象/数组 | 用 `JSON.stringify`（无空格）参与拼接 |
| appKey | 正式服密钥，**不随仓库分发**（见下） |
| udid 的作用 | **作 key 名**（其值被 appKey 覆盖）→ **签名依赖这个 udid**，必须与请求头的 `deviceid` 一致 |

> ⚠️ **不要把它理解成"签名与设备无关"**——那是错的。udid 的值虽然被 appKey 覆盖，
> 但 udid 本身作为**键名**进入拼接串，所以**同一 body 换 udid 会得到不同签名**。
> 它**不要求**是真实设备 ID：只要"算签名用的 udid"与请求头里的 `deviceid` 一致即可。
> **与设备无关的是 `sessionid`**（见 §4）。

**list 接口的签名要带 query 参数**（detail 用空 body 即可；list 不带 query 会 403）。

实现：`app/client/core/sveapi.py`（客户端侧）与 `scripts/probe/signer.py`（脚本侧）。
`signer.py` 自带拼接规则自检（`python scripts/probe/signer.py`，与密钥无关，
换任何 appKey 都能跑）。

### appKey 的配置

appKey 属于**不随仓库分发的凭据**——仓库里只保留读取逻辑。
按以下任一方式提供（优先级从高到低）：

```bash
export SVE_APP_KEY=<你的 appKey>          # 1) 环境变量
```

```jsonc
// 2) config/settings.json —— 该文件已被 .gitignore 排除
{ "app_key": "<你的 appKey>" }
```

> 服务端部署可用 systemd `EnvironmentFile=` 注入，详见 [deploy/README.md](deploy/README.md)。
> **未配置时签名函数会明确报错**，而不是用空密钥静默发出必然 403 的请求——
> 后者表现为"接口一直 403 Invalid sign"，很难定位。
> `deploy/verify.sh` 有专门一节检查 appKey 是否可解析。

---

## 4. 账号与会话

### 单一来源

会话、订阅与记录的权威来源为 **`<SVE_DATA_DIR>/identity-v2.json`**，默认目录仍是 `app/client/data/`。
所有关联使用官方 UID 的规范十进制字符串 `account_id`，名称仅用于展示。同名不同 UID 可同时存在；同 UID 改名并重新登录仍更新同一身份。

该文档一次原子发布账号、active_account_id、订阅、记录、删除墓碑和幂等凭据。旧三个 JSON 文件迁移后是防止旧版本运行/写入的 schema 标记，不能拿旧账号表替换它们。

`load_session(account_id)` 只读该数据集；明确指定未知 ID 时不使用默认账号或旧会话兜底。退役轮询器和直连报名脚本不能在 UID 数据集上成为第二执行点。

升级旧数据必须先停止写入并运行只读迁移预览；不会在启动时自动改写真实账号。详见 [M01 身份、协议与迁移说明](deploy/M01-account-identity.md)。本地与服务器分别迁移，不互相覆盖账号表。

### sessionid 与设备无关

**`sessionid` 不绑定 deviceid**，所以会话可以从本机迁移到服务器（或反方向）。
服务器上**没有浏览器也没有模拟器**，全靠这份会话工作。

### 会话失效怎么办

服务器上无法自动重新登录。失效时的处理：

1. 在本机客户端重新登录（浏览器登录流程，`app/client/core/weblogin.py` 用真实 Chrome + CDP 读 Cookie）；
2. 新版客户端在登录前固定两端版本，**明确登录成功后按 ID 提交单个会话**；服务器再次校验官方 UID 和版本。启动、重连不再整表推送；按 ID、版本明确删除会保留历史与删除墓碑；
3. 服务器随后即可继续。

另外代理**检测到会话失效会推送告警**，不必等你自己发现。

### 把运行时数据移出仓库

设置 `SVE_DATA_DIR` 可把账号、订阅、通知配置等指到仓库外：

```bash
export SVE_DATA_DIR=/var/lib/sve        # Linux / systemd EnvironmentFile
```

```powershell
$env:SVE_DATA_DIR = "D:\sve-data"       # Windows
```

这样"**仓库里没有凭据**"是结构性事实，而不是靠 `.gitignore` 的自觉。
生产实际路径须独立核对，不能只设置变量就假定旧数据已搬走（外部化见 M05）。

---

## 5. 客户端界面

打开 <http://127.0.0.1:8801/>（订阅日志在 `/subs`）。界面分六个区块：

| 区块 | 功能 |
|---|---|
| **账号** | 多账号登录/切换/删除；显示 uid 与会话状态 |
| **赛事列表** | 按省份/城市级联筛选；显示班级标签、名额、报名费；可「立即报名」或「订阅」；已订阅的显示「取消订阅」 |
| **自动报名订阅** | 当前所有订阅及其运行状态、逐条日志 |
| **自动报名执行位置** | 配置服务器主机/用户/私钥/端口；「启用远端执行」/「切回本机执行」/「比较两端账号」 |
| **通知设置** | 配置 PushPlus 与邮件通道；含「发送测试通知」 |
| **我的赛事** | 当前账号已报名/可预约的赛事 |

### 通知设置的两条约定

- 设置页只显示配置状态、来源及开关，不显示密钥摘要、地址、邮箱或私人路径。
  文本、密钥和数字输入框每次进入或成功保存后为空；留空保留原配置，填写后更新。
- **环境变量优先级最高**（`SVE_PUSHPLUS_TOKEN` / `SVE_SMTP_AUTHCODE` 等）。
  由环境变量提供的字段在界面上**只读**，否则会出现"改了不生效"的困惑。

**注意**：通知是由**执行报名的那一方**发出的。远端模式下是服务器上的代理，
所以改服务器配置要打开**服务器的界面**（经隧道 `http://127.0.0.1:8901/`），
本机界面改的是本机那份。

---

## 6. 常用命令

```powershell
# 启动客户端（独立进程；不要用后台作业，作业被清理时会连带杀掉）
Start-Process cmd /c start /min cmd /c 'cd /d <项目路径> && .\vendor\venv-client\Scripts\python.exe -u app\client\server.py > capture\client_stdout.log 2>&1'
```

```bash
# —— 服务器侧（在服务器上执行）——
sudo bash deploy/install.sh /tmp/sve-release # 仅首次安装；生产升级见 deploy/README.md
sudo systemctl restart sve-agent            # 改了配置或代码后重启
bash /opt/sve-monitor/deploy/verify.sh      # 逐项自检（推荐每次都跑）

# 常用运维
systemctl status sve-agent
systemctl status sve-watchdog.timer
journalctl -u sve-agent -n 50
```

```powershell
# —— 本机自检 ——
$py = '.\vendor\venv314\Scripts\python.exe'
& $py scripts\recon\check_scripts.py        # Python：编译 → 真实导入 → CLI
& $py scripts\recon\check_ui.py             # 页面：内联 JS 语法 + 元素 id 引用
& $py scripts\recon\check_lineendings.py    # 行尾：.sh/.service 必须 LF
& $py scripts\recon\check_ps1.py            # PowerShell（PS7 + 5.1 双引擎）
& $py scripts\recon\audit_paths.py          # 越界写操作扫描
& .\scripts\selfcheck.ps1                   # 全量：路径/语法/运行状态/设备

# —— 单项排查 ——
& $py scripts\probe\signer.py              # 签名拼接规则自检（与密钥无关）
& $py -B deploy/diagnostics.py              # 只读部署检查，不调用官方接口
Get-Content capture\poll.log -Tail 20       # 轮询日志
```

---

## 7. 部署到 Linux 服务器

监控/报名部分可搬到 Debian/Ubuntu 上常驻，用 systemd 取代 Windows 侧的
`*_ctl.ps1` 启停脚本。**完整步骤、排查表与安全说明见 [deploy/README.md](deploy/README.md)**，
这里只列要点：

- **凭据外部化**：`SVE_DATA_DIR` 与 `EnvironmentFile=` 让仓库里不含任何凭据；
- **appKey** 必须配置（见 §3），否则报名会全部 403；
- **时区必须显式设为 `Asia/Shanghai`**——云镜像默认 UTC，会让窗口判断整体错 8 小时；
- **执行点互斥**：服务器与 Windows 本地不要同时常驻，否则会重复提交。

---

## 8. 目录结构

> 每个脚本的职责、状态（核心/工具/已退役/一次性）与约定，
> 见 **[scripts/README.md](scripts/README.md)** —— 改脚本前先读那份。
> 客户端内部结构见 **[app/client/README.md](app/client/README.md)**。

```
sve-monitor/
├─ README.md                      本文件
├─ .gitignore                     ★ 排除凭据与运行时数据（见文件内注释）
├─ config/
│  ├─ settings.json               ⚠️ 含推送凭据与 appKey（已 gitignore）
│  └─ settings.example.json       配置模板（占位值，可提交）
├─ app/client/                    ★ 客户端 + 服务器代理（同一份代码）
│  ├─ server.py                   Flask 应用：本地客户端与服务端代理双角色
│  ├─ core/
│  │  ├─ sveapi.py                接口客户端 + 签名（appKey 从配置读）
│  │  ├─ scheduler.py             订阅调度器：分档轮询 + 两段复核
│  │  ├─ weblogin.py              浏览器登录（Chrome + CDP 读 Cookie）
│  │  └─ agentlink.py             代理连接 + SSH 隧道（自愈）
│  ├─ ui/                         index.html / subs.html
│  ├─ data/                       运行时数据（除 areas.json 外均 gitignore）
│  │  ├─ areas.json               省/市列表（界面需要，必须保留）
│  │  └─ identity-v2.json            ⚠️ 账号与会话（本地私有）
│  └─ test_*.py                   完成链路与自动移出的回归测试
├─ deploy/                        Linux 部署
│  ├─ install.sh / uninstall.sh / verify.sh
│  ├─ systemd/                    sve-agent / sve-poller / sve-watchdog
│  └─ README.md                   ★ 部署与排查（含安全说明）
├─ scripts/
│  ├─ README.md                   ★ 脚本索引与约定（先读这个）
│  ├─ sveenv.py                   ★ 路径与账号/会话的单一来源（Python 侧）
│  ├─ config.ps1                  ★ 同上，PowerShell 侧
│  ├─ selfcheck.ps1               全量自检：路径/语法/运行状态/设备
│  ├─ recon/                      静态分析与**检查器**
│  │  ├─ check_scripts.py         Python 分层验证（编译 → 导入 → CLI）
│  │  ├─ check_ui.py              页面内联 JS 语法 + 元素 id 引用
│  │  ├─ check_lineendings.py     行尾检查（.sh/.service 必须 LF）
│  │  ├─ check_ps1.py             PowerShell 双引擎语法检查
│  │  ├─ audit_paths.py           越界写操作扫描
│  │  └─ …                        逆向期静态分析片段（一次性）
│  ├─ poller/                     ⚠️ 旧轮询器（已退役，见 §12）
│  ├─ capture/                    Frida 进程内抓包（仅需要时用）
│  ├─ device/                     模拟器代理与证书工具
│  └─ probe/                      逆向期验证 + 两个可回归的单元测试
├─ capture/                       运行数据与逆向记录（仅 README 提交）
└─ vendor/                        私有 venv（venv314 / venv-client）
```

**`apk/` 与提取物不在仓库内**：官方安装包已移到仓库外，从它提取的 bundle 与
字符串表（原 `recon/`）已删除。地区数据 `areas.json` 是那份提取物的产物，
因为无法从仓库内重新生成，所以**必须保留并提交**。

---

## 9. 运行边界（脚本只动工作区）

已用 `scripts/recon/audit_paths.py` 扫描全部脚本，结论：**对工作区之外的写操作 0 处**——
状态、日志、运行时数据、venv 全部落在工作区内。

工作区之外的**只读**访问仅有：系统 `python.exe`（创建 venv 时）、
`node.exe`（打包 payload 时）、模拟器自带的 `adb.exe`。

**明确不碰**：`%USERPROFILE%\.mitmproxy`（mitmproxy 配置库——因为不可写，
已显式改用工作区内的目录）、注册表、系统目录、其它模拟器实例。

> 例外是 ADB 在**设备内部**的操作（推送 `frida-server`、读取 APP 的存储），
> 属于模拟器内部，不算宿主机越界。

---

## 10. 工程约定与自检

改代码前请遵守（完整说明见 [scripts/README.md](scripts/README.md)）：

| 约定 | 原因 |
|---|---|
| **`.sh` / `.service` / `.timer` 必须 LF 行尾** | Windows 上用 Python 文本模式写文件会把 `\n` 翻成 `\r\n`，bash 会报 `syntax error near unexpected token $'in\r'`，且**报错位置指向毫不相干的 `case ... in`**——这个坑踩过两次，已固化成 `check_lineendings.py` |
| 含中文的 `.ps1` 存 **UTF-8 BOM** | PS 5.1 按 ANSI 解析无 BOM 的 UTF-8，中文会破坏语法（踩过 3 次） |
| **不硬编码工具路径** | 统一走 `sveenv.py` / `config.ps1`，支持环境变量覆盖与自动发现 |
| **判断进程存活用 pid 文件**，不匹配命令行 | 命令行匹配会把"自己的诊断命令"和启动器包装进程一起匹配进来（误判过两次） |
| 后台进程用 `Start-Process` + pid 文件 | 后台作业被取消时会连带杀掉子进程（曾导致"以为在看护、其实早死了"） |
| 凭据只从**单一来源**读 | 避免两份副本不一致；便于整体外部化 |
| 唯一执行点 | 防止重复提交报名 |

**改完请跑自检**：

```powershell
& $py scripts\recon\check_scripts.py        # 编译 → 真实导入 → CLI
& $py scripts\recon\check_ui.py             # 页面内联 JS 与 id 引用
& $py scripts\recon\check_lineendings.py    # 行尾
```

其中**"真实导入"这层不可省**：`ast.parse` 只验文本能否解析成语法树，
抓不到 `global` 声明顺序、名称未定义、缺依赖这类问题——曾因此漏掉真实错误。
`check_ui.py` 同理：整段 `<script>` 里一个多余的 `}` 会让**所有按钮失效且服务端无日志**，
只有在真实页面里才看得出来。

---

## 11. 已知限制

- **会话有效期未知**：`sessionid` 何时失效尚未观测到。失效时服务器无法自行恢复，
  需在本机重新登录一次（新版客户端仅提交本次明确登录的会话）。代理检测到失效会推送告警。
- **服务器上没有浏览器/模拟器**：登录只能在有图形界面的本机完成。
- **通知有配额**：PushPlus 免费版与 QQ SMTP 都有限流，异常情况下通知可能延迟（不会丢）。
- **城市赛这类「单次开放且已过期」的窗口**：开放时刻一旦过去，代理会判定为"窗口一直开着"，
  因此会持续按正常频率轮询（而不是降频）。这是 instant 型窗口的既定行为。
- **`scripts/probe/verify_sign.py` 会以 exit=1 结束**：这是它的设计结论
  （"算法已确认，缺 udid 的真实取值"），不是故障。

---

## 12. 附：旧轮询器退役

早期单账号单赛事执行链已无条件封锁，不再提供启动、直连报名或会话恢复操作。旧实现仅用于 Git 历史追溯；现役通知、看护与开发签名工具已经迁出该目录。兼容范围、代码包清理和独立生产 unit 维护见 [M20](deploy/M20-retire-legacy-poller.md)。

## 外部运行目录（M05）

服务器可将运行数据、settings、日志和发布备份迁出代码目录。已有部署先阅读 [M05 迁移与回退](deploy/M05-external-runtime.md)，在停写维护窗口用离线工具迁移，再配置受保护的 EnvironmentFile；仅设置 SVE_DATA_DIR 不会自动搬走旧文件。地区数据继续随代码发布，现有手机入口和登录协议保持兼容。

## M20 旧执行链退役

自动报名由 UID 服务订阅调度器执行。通知使用 `app/client/core/notification_sender.py`，代理看护使用 `scripts/runtime/watchdog.py --once`。历史轮询、直连报名、会话恢复及 Windows 轮询控制器已封锁。旧 `scripts/poller/watchdog.py --once --agent` 暂时转发到同一现役模块、状态与写锁。新包排除旧执行器与开发签名工具；部署及回退见 [M20](deploy/M20-retire-legacy-poller.md)。
