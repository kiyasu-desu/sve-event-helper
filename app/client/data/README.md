# app/client/data —— 运行时数据目录

本目录存放客户端/代理的**运行时数据**。除 `areas.json` 外，其余文件都含
凭据或个人数据，**已被 `.gitignore` 排除，不要提交**。

| 文件 | 内容 | 是否提交 |
|---|---|---|
| `areas.json` | 省/市列表（34 省 / 268 市） | ✅ **提交**（界面需要） |
| `identity-v2.json` | UID 账号、会话、订阅、记录、版本、墓碑与幂等凭据 | ❌ 含凭据 |
| `identity-migration.json` / `identity-backups/` | 事务状态、旧数据快照、迁移候选与待处理原件 | ❌ 含凭据 |
| `accounts.json` | 旧账号表；迁移后是拒绝旧读取器的 schema 标记 | ❌ 私人运行文件 |
| `accounts.json.bak` | 上一份账号表的备份 | ❌ 含凭据 |
| `notify.json` | 通知渠道配置（界面「通知设置」写入） | ❌ 可能含凭据 |
| `records.json` | 旧记录；迁移后为 schema 标记 | ❌ 私人运行文件 |
| `subscriptions.json` | 旧订阅；迁移后为 schema 标记 | ❌ 私人运行文件 |
| `remote.json` | 远端执行配置（服务器地址、用户名、私钥路径） | ❌ 含基础设施信息 |
| `client.log` | 运行日志 | ❌ 含账号、邮箱、IP |

## 让凭据彻底离开仓库

`SVE_DATA_DIR` 可指定仓库外目录，但不会自动搬走旧数据、日志或配置。既有数据先按 M01 单独预览/迁移；外部化切换由 M05 安排。静态 `areas.json` 随代码保留，数据目录未提供时由代码目录读取：

```bash
export SVE_DATA_DIR=/var/lib/sve        # Linux / systemd EnvironmentFile
```

```powershell
$env:SVE_DATA_DIR = "D:\sve-data"       # Windows
```

不要把受保护的身份数据、待处理集合或备份贴入 PR。详见 [M01 迁移说明](../../../deploy/M01-account-identity.md)。

## areas.json 为什么必须保留

它是从 APP bundle 的字符串表提取的（原提取脚本 `scripts/recon/extract_areas.py`）。
由于提取源（`recon/cjk_all.txt`）属于他人软件、已不再随仓库分发，**这份数据
无法从本仓库内重新生成**，所以它必须随仓库提供。
