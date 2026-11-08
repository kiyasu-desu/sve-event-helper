# M20 旧轮询器退役

开发基线：新仓库独立脱敏 main `ab5200b`，包含 M00–M19 源码，不导入旧仓库历史。已阅读工作区计划第 11 节与两处 AGENTS.md。

## 源码与兼容范围

现役通知迁至 `app/client/core/notification_sender.py`，看护迁至 `scripts/runtime/watchdog.py`。看护直接使用 RuntimePaths、notification_config、watchdog_state，不导入旧 poll 或 sveenv。损坏配置导致检查失败，保留去重状态。

旧 watchdog 路径要求 `--agent`，转发新入口，共享 DATA_DIR/watchdog/watchdog-state.json、WriterLock 和日志。旧 notify 是同一模块别名。开发 signer 迁至 probe，旧路径仅为开发兼容别名。所有私人 unit/工具迁移并验证后才移除这些兼容入口。

旧 poll、action、autosignup、session_manager 和开发直连 probe_action 在模块导入时无条件退出，早于配置/凭据读取、目录创建、联网、报名和通知。旧 Windows 控制器也无条件退出，不读取 PID、不杀进程或启动旧程序。sveenv 原 UID 防护仍保留。旧测试命令转发现役调度器回归。旧 poller unit 是无重试的失败入口，不能安装或启用。

## 生产包和恢复

新发布白名单排除四个旧执行器、旧 signer 和旧 poller unit，包含现役模块、旧 watchdog/notify 转发器。copy_runtime 使用相同白名单。旧 M04/M05/M06 清单仍可校验和回退。

切换时只备份、校验并删除五个固定旧 Python 代码路径；逐层拒绝符号链接、越界及非普通文件。失败使用 journal 回退，恢复原字节和清单、移除新增代码。运行数据、配置、日志、看护状态和备份全部保留。不递归清理目录或未知文件。系统 unit、现存 Windows 脚本和私人开发工具不在此自动清理范围，不能假定已替换。

## 独立生产维护步骤（本 PR 未执行）

1. 只读核对实际发布 SHA、清单/哈希、unit/drop-in、执行位置和数据摘要；计划中 2026-11-08 的快照不能替代发布前核对。确认无额外 Windows 执行者，保留 HTTPS、认证、隧道和 APK。
2. 维护窗口记录全部 unit 的 active/enabled/masked 状态，暂停 watchdog timer/service、agent 及所有写入者，确认 poller 停止并禁用。备份一致数据、配置、看护状态和 unit/drop-in，保存受保护恢复路径。
3. 按部署 README 构建、预检并停写切换。先保留旧看护 unit，以旧入口核对代理快照和原去重状态；真实通知测试单独授权。核对数据摘要无丢失。
4. 核对系统旧 poller unit 的真实路径、符号链接和自定义设置。屏蔽前安全备份并移走已核对的本地普通 unit（mask 不能直接覆盖它），再屏蔽；禁止删除未核对路径。此动作只属于另行授权的退役部署。
5. 将原 watchdog service 的 ExecStart 切至新入口，保留私人 EnvironmentFile、User、WorkingDirectory 和安全设置。daemon-reload 后仅恢复原 timer，不能新建第二个定时执行者。确认 agent 唯一、看护成功、异常及恢复通知去重正常，核对手机浏览器/PWA/已有 Android 登录、地区赛事、订阅记录、设置和缓存更新。
6. 回退先停写，使用本版 release.py rollback/recover 恢复代码，保留最新数据。按备份恢复 watchdog unit/drop-in 和原服务状态；旧 poller 始终禁用，不能因恢复历史代码重新启用。撤销屏蔽需单独核对原状态及授权。再次核对数据和手机链路。

## 验证边界

测试使用隔离临时目录、合成身份、模拟官方接口和通知替身。未部署服务器、屏蔽生产 unit、真实报名或发通知。未实测认证后的手机/PWA/Android；源码和包演练不等于生产稳定性验收。

验证结果（2026-11-08）：Windows Python 3.11 运行 `python -B -m unittest discover -s tests -p "test_m*.py" -q`，共 213 项，成功结束（其中 2 项跳过：系统不允许创建测试符号链接、POSIX root 专属权限测试）。前端 `node tests/test_identity_ui.js` 91 项通过，脱敏扫描器 7 项通过；88 个 Python 文件通过 3.10 语法及内存编译，Linux LF、中文 PowerShell BOM 和 diff 空白检查通过。完整候选树及新仓库初始历史结合私人标识表扫描零命中，4 个既有图标已目视检查。回归覆盖无旧实现的新包启动导入、新旧看护 CLI、旧入口提前退出、异常/恢复去重、固定代码清理、中断回退、旧清单及最新数据保留。Python 3.10 真正运行、POSIX 权限和生产手机链路仍待对应环境验收。
