> M06 新增线程存活、推进期限和持久化告警；服务端与看护部署顺序、每分钟 timer 和回退组合见 [M06 说明](M06-scheduler-watchdog.md)。默认发布诊断仍不等同于调度进度看护，旧接口看护也不具备 M06 能力。

> M05 已增加外部运行目录、EnvironmentFile 和离线存储迁移；新安装模板使用 `/var/lib/sve`。现有部署升级及回退前先阅读 [M05 路径与迁移说明](M05-external-runtime.md)。下面 M04 的 `config/settings.json` 和 `.deployment-backups` 路径仅适用于尚未迁移的旧模式；外部模式的对应位置为 `DATA_DIR/settings.json` 和 `DATA_DIR/deployment-backups`。迁移与代码发布是两个独立事务，生产激活另行验收。

# Linux 生产部署：agent 与可追溯发布

当前执行点是 `sve-agent`，旧 `sve-poller` 已退役，不能同时运行。代理监听 `127.0.0.1:8901`；手机使用已有 HTTPS、Cloudflare Access 和隧道入口，电脑可使用 SSH 转发。Tailscale Serve 是另选路径，不是升级前提。普通后台升级不改域名、认证策略、隧道或 APK 地址，也不要求 Windows 在线。

本次计划路径是 **生产 M01 → 一次发布 M02–M04**。M02/M03 保持 schema 2。M04 不外迁数据，实际路径仍以生产配置为准；外部数据与凭据迁移属于 M05。完整验收与限制见 [M04 说明](M04-traceable-deployment.md)。

## 1. 生成与核对发布包

先合并经过验证的代码，再从明确的完整提交 SHA 生成新目录；工具读取 Git 对象，忽略未提交工作区改动，不从工作目录复制私人文件：

```bash
python3 -B deploy/release.py build --source . --ref <完整提交SHA> --output /tmp/sve-release
python3 -B /tmp/sve-release/deploy/release.py verify --package /tmp/sve-release
```

`release.json` 包含发布 ID、源码 SHA、UTC 构建时间、schema/兼容 schema、每个运行文件的精确 SHA-256，以及按设计排除的受跟踪文件。运行白名单固定在 release.py：服务器、全部核心/UI/地区资源、通知/看护所需 Python 模块、迁移/部署工具和 agent/watchdog 模板。Android、测试、抓包、开发脚本及 retired poller unit 不部署。Linux 脚本和 unit 在构建时统一 LF，部署后按精确字节校验，不能用换行归一化掩盖差异。

目录即发布包，可打包传输；不混入 settings、数据、日志、私人 APK 或备份。到服务器后重新 verify，来源目录必须在目标安装目录之外。哈希清单用于完整性和追踪，不代替可信来源或签名认证。

## 2. 在线预检与维护准备

确认服务器实际版本、执行点、数据目录、账号身份和报名窗口；不要把 GitHub 合并当作生产已更新。以服务用户和实际虚拟环境预检：

```bash
sudo -u <服务用户> /opt/sve-monitor/.venv/bin/python -B /tmp/sve-release/deploy/release.py preflight --package /tmp/sve-release --target /opt/sve-monitor
```

预检校验清单、Python 3.10 语法、依赖、私有签名配置及 schema 2 数据。导入候选服务器时使用独立临时数据和合成密钥，并阻断网络、应用后台线程与进程启动，不触发真实调度。此临时目录只属于预检；默认 verify 不创建临时文件。

升级不会安装 apt/pip 依赖或删除虚拟环境。缺依赖时先在单独环境准备经过检查的依赖，再重新预检；不要在切换途中升级依赖。当前 requirements 需要 Flask 3.x、requests 2.31+。`SVE_APP_KEY` 可由受保护环境注入，或沿用已有 config/settings.json，不能把真实值写进命令行/PR。服务环境不一定继承交互 SSH 的环境，应核对现有 EnvironmentFile/drop-in。

schema 1 或迁移未完成须先按 [M01 说明](M01-account-identity.md) 单独安排离线迁移；M04 不会自动猜格式、清理数据或把本机账号表复制到服务器。

## 3. 停写、切换和恢复

在不会影响报名窗口的维护时段，记录并暂停 watchdog timer/service、agent、旧 poller 和所有其他写入者/本地执行者。保留原服务的启用状态，禁止把旧 poller重新启用。`upgrade.sh` 只检查停用状态并切换代码，**不会替操作者停启服务或修改 unit**：

```bash
sudo APP_DIR=/opt/sve-monitor bash /tmp/sve-release/deploy/upgrade.sh apply /tmp/sve-release
```

直接 Python 工具需显式 `--writers-stopped`；这个声明不替代停写核对。工具还取得 schema 2 目录写入锁，防止与 agent/迁移并发。使用自定义 SVE_DATA_DIR 时，wrapper 继承变量；直接工具传 `--data-dir`，并在升级、恢复和服务中始终使用同一路径。

切换前在目标 `.deployment-backups/<事务ID>/` 保存一致性的 data/config/capture 私有快照与旧代码哈希，私有文件为 600、备份入口为 700（Windows 须另查 ACL）。目标 data、settings、notify、会话、日志、迁移备份不从发布包复制，不清理。仅固定公共代码白名单替换；静态地区文件属于发布资源。

`deployment-state.json` 记录 prepared → copying → complete；它只含公共代码哈希和事务标识，可供服务读取。新服务发现未完成事务会拒绝启动，避免执行混合代码。每个文件使用刷盘后的原子替换，**整个文件集合不是一次原子操作**：一致性由停写、目录锁、持久化事务与中断恢复共同保证。代码备份和私有快照都不自动删除。

同一个完整发布重复 apply 为无改动成功；已安装文件哈希异常、坏包、非法路径、缺配置或 schema 冲突会拒绝。切换中失败后保持服务停止，先 recover/rollback，不能继续启动或重新 apply 覆盖事务。

完成后只恢复 agent 与原 watchdog/timer，保留现有 unit/drop-in、网络和认证。已有 unit 不因 M04 自动重写；更新模板只用于新安装或另行人工比较。当前模板禁用字节码，watchdog 只检查接口可达，不等于调度线程健康（M06 才完善）。

## 4. 默认只读验收与显式外部测试

```bash
sudo -u <服务用户> APP_DIR=/opt/sve-monitor bash /opt/sve-monitor/deploy/verify.sh
sudo -u <服务用户> APP_DIR=/opt/sve-monitor bash /opt/sve-monitor/deploy/verify.sh --health-url http://127.0.0.1:8901/api/health
```

默认检查精确文件哈希、事务完成、依赖、配置、数据结构/权限/目录可写性及单一 systemd 执行点；不调用官方接口，不运行旧 poller/watchdog，不发送通知，不写生产日志/临时文件/字节码。包尚未安装时用 release.py verify 检查包本身；在线预检与默认只读检查是不同职责。

`/api/version` 和 `/api/health` 返回启动时发布 ID/SHA/构建时间、启动时间和存储可用性，不含账号/会话/地址。磁盘被更新但进程没重启时仍报告原发布，`--health-url` 会发现进程与磁盘发布不一致。legacy-unversioned 表示没有可信发布清单；健康接口不证明线程仍在推进，更不证明手机认证链路或上游正常。

以下操作会访问外部系统，必须明确调用，普通验收不自动执行：

```bash
bash /opt/sve-monitor/deploy/verify.sh --validate-upstream --account-id <明确UID>
bash /opt/sve-monitor/deploy/verify.sh --test-notify
```

前者只用指定账号请求一次官方资料并比较 UID，不写缓存、不刷新会话、不报名；后者会真实发送已配置渠道的测试消息。输出不回显账号和凭据。渠道接受请求不能保证终端实际送达；真实报名始终不用于默认验收。

手机发布条件：原认证会话可用、Cookie 登录换会话兼容、按 UID 区分/切换账号、地区与赛事、订阅/记录、M02 昵称刷新、PWA 更新和前后台/断网重连，以及已安装 APK。保留原入口，不要求清 Cookie 或重装 APK。模拟验收不足以替代真机认证后链路；无法验证的项必须标记为待验，不能只凭 SSH HTTP 200 宣称发布通过。

## 5. 中断恢复与兼容回退

保持服务停止，使用保留在安装目录外的发布工具：

```bash
sudo APP_DIR=/opt/sve-monitor bash /tmp/sve-release/deploy/upgrade.sh recover
# 已完成切换后需要回退时：
sudo APP_DIR=/opt/sve-monitor bash /tmp/sve-release/deploy/upgrade.sh rollback
```

recover 与 rollback 都校验旧代码备份哈希及当前数据 schema，再恢复前一份完整代码；它们**从不自动恢复旧数据快照**。rolling_back 中断后可再次执行。新代码增加的公共文件若旧版本不存在，只移除这些固定白名单文件；不递归删除目录。

回退期间，现有 unit 直接执行的 `app/client/server.py` 会先被替换为不导入任何业务模块、始终拒绝启动的临时入口。旧入口暂不恢复；其他公共文件恢复并校验完成、事务先提交为 rolled_back 后，才最后原子恢复旧入口并清除 entry_pending。这样旧 M01 无需认识新事务格式，也不会在 rolling_back 期间运行混合代码。

如果事务已为 rolled_back 但 entry_pending 仍为 true，必须继续 recover：它会重新核对已恢复文件，再发布或确认旧入口，最后清除标记，不直接按“已回退”跳过。仅入口发布后的元数据收尾中断时，其他代码已经全部校验完成；恢复不回写业务数据。恢复始终使用安装目录外、包含此次修正的发布工具，不能用旧 M04 工具处理新入口保护阶段。

M01→M02–M04 的数据仍为 schema 2，可保留上线后的新会话、资料、订阅和记录回退代码。回到 M01 会恢复 M02/M03 原有缺陷，须评估受影响任务。首次安装无旧代码时 rollback 会回到无代码状态，不代表存在可运行的旧服务。

如果未来 schema 不兼容、身份迁移未完成、备份损坏或无法确认旧代码兼容，工具拒绝；保留最新数据并暂停，另做前向修复/人工核对或明确的向下迁移。不能以最初私有快照覆盖上线后业务数据。

## 6. 首次安装与运维边界

`install.sh /绝对路径/发布目录` 只接受尚无 server.py 和权威数据的首次安装；已有部署必须走上面的升级路径。首次安装会 apt/pip、建虚拟环境和目录、写新的 unit，但不自动启动服务、不覆盖已有 unit/drop-in、不导入来源包私有配置。先准备受保护签名配置；模板在发布包的 config/settings.example.json。首次安装、代码升级、服务启用和手机生产验收分别执行。

SSH 入口见 [SSH 指南](PWA-SSH-Tunnel.md)，Tailscale 入口见 [Tailscale 指南](PWA-Tailscale.md)，Android 说明见 [Android](Android-App.md)。它们是可选部署形态；生产现有 Cloudflare 入口继续保留。不要运行旧 poll.py --once、自检或安装器来证明安全；旧脚本可能写日志、请求官方或发消息。

## M20 退役升级

新 unit 使用 `scripts/runtime/watchdog.py --once`；旧 `scripts/poller/watchdog.py --once --agent` 保留兼容。发布事务只备份、删除五个固定旧 Python 代码路径，回退恢复其原始字节，运行数据不回退。旧 unit 的禁用/屏蔽、私人 drop-in 核对和维护窗口步骤见 [M20](M20-retire-legacy-poller.md)。只启用同一个看护 timer。
