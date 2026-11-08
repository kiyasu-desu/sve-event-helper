# M05：运行数据与凭据外部化

基线 `b6c0730`（M04 及上线前修正已合并），分支 `codex/m05-external-runtime`。
2026-11-05 只读生产核对：发布 SHA 与基线一致，schema 2；14 账号、0 订阅、29 记录；agent active、旧 poller inactive、看护 timer active；实际文件逐项 SHA-256 与清单一致，loopback 健康接口报告的运行中发布也一致。本任务不操作生产迁移、服务、认证、网络或真实报名/通知。

## 路径与优先级

| 内容 | 旧模式 | 设置外部 SVE_DATA_DIR 后 |
|---|---|---|
| UID 数据、订阅、记录、pending、删除版本、迁移备份、通知 UI 配置、远端配置 | app/client/data | DATA_DIR（schema 仍为 2） |
| 签名/通知底层配置、资料刷新周期 | config/settings.json | DATA_DIR/settings.json |
| client.log | app/client/data | DATA_DIR/logs |
| poll/watchdog 日志 | capture | DATA_DIR/logs |
| 旧 session、采样、完成状态和其他 capture 产物 | capture | DATA_DIR/capture |
| 新代码发布的私有备份及旧部署备份 | .deployment-backups | DATA_DIR/deployment-backups |
| 旧维护备份及其恢复说明（整个目录按私人材料处理） | .maintenance | DATA_DIR/maintenance-backups（保留相对路径） |
| 其他旧私人 config 文件 | config | DATA_DIR/legacy-config（保留归档，业务不读取） |
| 地区数据 | app/client/data/areas.json | 仍为发布包中的同一静态资源；缺失时保留既有兼容回退 |
| systemd stdout/stderr | capture/agent_stdout.log | journald（随系统日志保留策略管理） |

`SVE_SETTINGS_FILE` 可显式覆盖 settings 路径，外部模式禁止指回代码目录。签名优先级为 `SVE_APP_KEY` → 选定 settings；通知优先级仍为环境变量 → notify.json → 选定 settings。外部配置缺失时绝不读取旧 config 里的凭据。`SVE_SESSION_FILE` 仅用于没有身份数据时显式读取旧单会话，不成为写入来源。旧执行器的 UID 封锁保持不变；不启用第二执行点。

没有显式设置外部目录时保留旧运行模式。Windows 可使用任意受 ACL 保护的仓库外目录，并为服务/启动脚本提供同样变量；Python 不强制 Linux 路径。PowerShell 抓包/设备工具仍是手动开发工具，其路径不由此 Python 服务配置替代；不要启动它们进行生产验证。真实 APK、SSH 私钥、手工抓包导出及系统日志也不因设置 DATA_DIR 自动搬迁。

## 一致性迁移、恢复与回退

先安装 M05 代码，仍保持旧路径，按 M04 工具完成代码发布验证。使用新版本迁移工具；它默认只读预览，不启动服务、不操作 systemd、不调用官方接口、不发送通知。迁移必须在全部写入者、agent、旧 poller、看护及 timer 停止后执行。POSIX 上由管理员以 root 执行实际迁移，并通过 `--service-user` 显式指定 unit 的实际服务用户；不授予服务用户代码根目录写权限。只读预览可使用有源数据读取权限的用户。锁只能排除遵守锁协议的写入者，不能代替维护窗口。

```sh
python3 -B deploy/runtime_migrate.py --root /opt/sve-monitor --external /var/lib/sve
# 将 SVE_USER 设置为 unit 的实际服务用户名（不是当前登录用户名）。
# 确认维护窗口、停写和受保护一致备份后执行：
sudo python3 -B deploy/runtime_migrate.py --root /opt/sve-monitor --external /var/lib/sve --apply --writers-stopped --service-user "$SVE_USER"
```

迁移前单独保存受保护的一致备份，包括已有部署事务和私有文件；不能提交 GitHub。外部目的地必须在代码树外，不能已有冲突文件，不能经过符号链接。工具校验 schema/UID 归属，锁定两端，逐文件复制、校验精确哈希，校验目的身份集合后封锁旧读取器，最后移除已验证的旧私有文件。地区文件和公共模板保留；旧文件内容都在新目录中保存。新建私有目录为 700、文件为 600。POSIX 工具在成功或异常退出时，将两端运行目录、私有文件、写锁和代码根中的 `runtime-migration.json` 移交指定服务用户；公开模板和地区文件不变，代码根仍由管理员保护。反向迁移、恢复也必须以 root 并带同一 `--service-user` 执行；不要仅切换成 root 后省略所有权交接。Windows 不使用该参数，必须单独设置 ACL。

`runtime-migration.json` 是受保护的事务记录，含私人路径及哈希，不上传仓库，也不由代码升级/回退覆盖。未完成时 M05 拒绝启动任何路径；旧位置的 identity-migration 封锁也使 M01–M04 拒绝初始化空数据集。中断后不要启动服务，使用同一 root/external 和方向加 `--recover --writers-stopped` 继续，保留相同 `--service-user` 参数。强制终止进程或断电可能跳过退出交接；必须由管理员先完成恢复及权限检查，不得直接启动服务。复制阶段从完整源重做；退休阶段从已校验目的继续清理。数据损坏或变动会拒绝，保留剩余文件供离线处理，不强制覆盖。

回退先停写，运行同一命令加 `--reverse` 预览，再加 `--apply --writers-stopped`。它搬回**当前最新数据**，保留新会话、昵称、订阅、记录和新增文件；不会恢复过时快照。反向中断用 `--reverse --recover --writers-stopped`。两方向重复运行幂等；切换后仅允许事务指定的权威目录，非活动目录保留封锁标记。未完成时可恢复当前方向；需要取消则先完成恢复，再做反向迁移。

若复制已完成但所有权交接报错，继续保持停写，修正权限问题后以相同方向、相同服务用户重跑 `--apply --writers-stopped`；幂等分支会重新交接所有权。事务仍未完成时才使用 `--recover`。服务用户应能读取 0600 事务，代码根目录本身无需也不应交给服务用户。

离线迁移不编辑 unit/env 文件，事务完成后仍保持停机，先修改配置并验证服务用户实际权限。确认代码根仍为 root 所有且服务用户不可写，服务用户能读取事务并在活动数据目录写入；已有目录的权限也须按 700/600 私下核对。若之后需要回退 M04 代码，应先反向搬回，再恢复旧 unit/env，最后按 M04 代码回退流程执行；旧代码不能在外部目录上继续读取原有 settings。旧 M04 工具不知道新备份路径，外部模式的代码升级/恢复必须使用 M05 工具。旧清单仍可校验及回退，新发布包包含路径模块、迁移工具和模板。

## 旧维护备份的上线前检查

`.maintenance` 中的会话、签名配置及其他旧维护材料现已全部纳入双向迁移映射，不按扩展名过滤，也不跳过其中的 README。迁出沿用事务记录、逐文件哈希验证、700/600 新建权限、服务用户交接和中断恢复；仅在目的副本全部验证后退休源文件。反向迁移从 `DATA_DIR/maintenance-backups` 恢复到原 `.maintenance` 相对位置，保留最新内容。该目录不是业务配置来源，不能将其中旧会话或签名配置自动覆盖到当前生效配置。

发布前在停写窗口私下盘点源目录，核对所有备份均在映射和事务中，再验证外部归档的文件数、哈希、权限及恢复路径。不得上传这些材料、文件名清单或事务到 GitHub；`.gitignore` 和脱敏扫描器均阻止两处私人备份目录入库。此修复没有读取或迁移生产中的具体备份，现网四份备份仍须在实际维护窗口完成此验收。

旧版本生成的未完成事务可能没有记录 `.maintenance`。新版发现未记录文件时会拒绝恢复，不会修改事务或擅自扩展已开始的复制集；旧事务已完成而源仍有维护文件时，也会拒绝幂等“已完成”。保持停写，先将这些剩余材料另行归档到受保护的代码树外目录，逐文件验证哈希、权限和可恢复性后，再退休对应源文件并重试原方向。保留独立归档和恢复位置记录，不能删除事务、重跑初始化或用旧快照覆盖当前身份数据。本工具不会自动执行这项额外归档。

## systemd 与私有环境

模板使用 `EnvironmentFile=/etc/sve/secrets.env`、`StateDirectory=sve`、`StateDirectoryMode=0700`、`UMask=0077`、`ProtectSystem=strict` 和 `ReadWritePaths=/var/lib/sve`。环境文件必须事先以 root:root 600 配置，父目录建议 root:root 700；systemd 在切换服务用户前读取它，不需服务用户读取文件本身。参考 `config/secrets.env.example`，保留实际入口和所有既有服务参数，只替换存储与日志配置；不要覆盖已有网络、认证、端口或 drop-in。

首次安装器仅支持模板路径，要求私下预配置环境文件与签名配置；它不会复制来源私有配置。已有部署不要重跑首次安装器。升级/检查工具按当前进程环境解析路径；管理员 shell 不会自动继承 systemd EnvironmentFile。可通过临时 systemd oneshot 在相同用户、EnvironmentFile、只读代码及可写数据沙箱下运行 diagnostics，使用 `--data-dir /var/lib/sve` 明确目标。不要 shell source 环境文件、在 argv 拼接密钥或打印环境。

按新模板审核并更新 agent/watchdog；删除旧 append: 日志设置和旧代码目录写权限，不覆盖其他生产定制。确认 `/var/lib/sve` 属于实际服务用户且 700，递归核对私有子目录/文件权限。服务用户应显式运行 `deploy/diagnostics.py --root /opt/sve-monitor --data-dir /var/lib/sve --probe-storage`，在数据、日志、capture 和备份目录完成临时文件创建、fsync、原子替换及删除；只返回操作结果，不调用上游或通知。须在与服务相同的 systemd 用户/沙箱中运行；仅 root 能写不算通过。该探针是显式写入验收，默认检查仍只读。环境文件路径配置必须与 unit 的 ReadWritePaths 一致。journald 访问按主机现有日志授权控制。

恢复 agent 前核对新路径中的 UID/地区、数据数量、通知来源、发布清单、停写事务和权限，确认旧路径启动被拒绝。恢复后验证会话更新、订阅保存/重启恢复以及单执行点；涉及账号/订阅的自动化验证使用模拟数据。最后恢复原看护 timer，核对它使用同一环境。手机浏览器/PWA/既有 APK 沿用原 HTTPS/认证和 Cookie 登录协议，无需重装或清除登录状态。

## 验证与边界

隔离测试覆盖预览无写入、两方向迁移与重复执行、最新会话/昵称保留、复制/封锁/源退休中断恢复、冲突/路径重叠/双写锁拒绝、外部配置优先级、通知读取/持久化、静态地区和日志路径。继续运行 M00–M04 及页面兼容回归，确认没有真实网络、业务线程、报名或通知动作。

本机 Windows 无法证明 Linux systemd 沙箱、POSIX 实际权限和认证后的手机链路；这些是生产发布前的必要验收项。本任务不自动迁移生产，也不轮换现有凭据。历史暴露沿用 M07 的遗留结论，不进行历史改写或强推；当前候选树、暂存树和待推送提交另行扫描。

权限修复补充验证：无网络、只读代码副本的 Linux 容器通过 8 项迁移测试，覆盖 root 所有代码目录、独立非 root 用户、两方向三种中断恢复，以及交接失败后的幂等重试。子进程实际降权后验证事务可读、活动数据及写锁可用、代码根不可写。该隔离验证不代替生产 systemd 沙箱和手机链路验收。

交付验证：Python 回归合计 156 项（155 通过、1 项既有符号链接用例因 Windows 权限跳过），其中 M05 6 项覆盖双向故障恢复、损坏事务/目的文件拒绝及实际外部持久化；M04 20 项含外部备份/代码回退和显式写探针。22 项页面/PWA 模拟、7 项扫描器测试通过。79 个 Python 文件按 3.10 语法解析并内存编译；8 个 Linux 脚本/unit 保持 LF，三个部署 shell 脚本通过 Git Bash 语法检查。运行使用本机可用的 Python 和既有依赖目录，未改动生产依赖。

当前候选工作树、完整暂存树及实际待推送提交使用仓库私有标识配置扫描，未命中；四个既有图标已目视确认无个人内容。测试继续保留生产数据/日志哈希不变及外部动作拦截断言。未跟踪的 AGENTS.md、维护计划和其他工作目录保持原状，不纳入功能提交；已有历史暴露仍未清理，不将当前树扫描通过表述为全部 Git 历史脱敏。
