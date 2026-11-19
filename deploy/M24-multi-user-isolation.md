# M24：Cloudflare Access 多用户权限隔离、共享 UID 订阅与首次消息引导

本说明记录 M24（Plan 2）在生产环境启用 Cloudflare Access 多用户隔离模式的配置方法、升级步骤与成对回退机制。

---

## 1. 模式概览

- **默认单用户兼容模式（`SVE_AUTH_MODE` 未设置或为 `single`）**：
  - 所有请求映射为 `owner`（`admin`），保持本地桌面客户端、纯 SSH 隧道部署及既有单元测试行为完全不变。
- **Cloudflare Access 多用户模式（`SVE_AUTH_MODE=cloudflare`）**：
  - 凡携带代理请求头（`Cf-Access-Jwt-Assertion`、`Cf-Connecting-IP`、`Cf-Ray`、`Cf-Visitor`、`X-Forwarded-*`、`Cf-Access-Authenticated-User-*`）或来自非回环地址的请求，必须携带有效的 `Cf-Access-Jwt-Assertion`（RS256）并通过 Cloudflare JWKS 公钥与系统 `openssl` 验签，严防 `cloudflared` 从 `127.0.0.1` 转发时绕过认证。
  - 验签通过后，邮箱命中 `SVE_ADMIN_EMAILS` 的请求进入管理员 `owner`（`admin`）空间；其余白名单邮箱进入各自独立的普通用户（`user`）空间。
  - 纯本机回环且无任何代理头的直连请求（如管理员 SSH 隧道、本机健康检查）：
    - `/api/scheduler/health` 保持本机回环只读看护访问；
    - 其余接口在 `SVE_ALLOW_LOCAL_ADMIN=1`（默认）时映射至管理员 `owner`（`admin`）空间。

---

## 2. Cloudflare Zero Trust 后台配置清单

1. **配置 One-time PIN 登录方式**：
   - 进入 Cloudflare Zero Trust -> **Settings** -> **Authentication** -> **Login methods**，确保已启用 **One-time PIN**。
2. **创建或编辑 Self-hosted Application**：
   - 进入 **Access** -> **Applications**，选择对应自托管域名应用。
   - 在 **Overview** 页面复制 **Application Audience (AUD) Tag**（用于 `SVE_CF_ACCESS_AUD`）。
   - 在 **Settings** -> **Team Domain** 确认团队名称 `<team-name>`（即 `<team-name>.cloudflareaccess.com`，用于 `SVE_CF_ACCESS_TEAM`）。
3. **配置白名单访问策略（Policy）**：
   - Action 选择 `Allow`；Include 规则配置允许登录的管理员与朋友邮箱列表（Emails）。

---

## 3. 服务器 `EnvironmentFile` 配置清单

在服务器受保护的环境变量文件（如 `/etc/sve/secrets.env`，权限 `root:root 0600`）中追加以下配置：

```ini
SVE_AUTH_MODE=cloudflare
SVE_CF_ACCESS_TEAM=your-team-name
SVE_CF_ACCESS_AUD=replace-with-cf-access-application-aud
SVE_ADMIN_EMAILS=admin@example.com
SVE_ALLOW_LOCAL_ADMIN=1
```

> 说明：
> - `SVE_ADMIN_EMAILS` 支持逗号分隔多个管理员邮箱（大小写不敏感）。
> - 管理员的通知仍按 `ENV_NOTIFY > <SVE_DATA_DIR>/notify.json > <SVE_SETTINGS_FILE>` 三层优先级解析。
> - 普通用户的通知配置独立保存在 `<SVE_DATA_DIR>/users/<user_key>/notify.json`（权限 `0600`，目录 `0700`），绝不读取或继承管理员环境变量、`notify.json` 或 `settings.json`。

---

## 4. 数据兼容、升级与成对回退

1. **Schema v2 保持不变**：
   - `identity-v2.json` 保持 Schema v2 不变；用户与游戏 UID 的多对多绑定关系独立保存在 `<SVE_DATA_DIR>/users-v1.json`。
   - 首次升级启动时若 `users-v1.json` 尚不存在，服务自动将现有全部游戏账号归属至 `owner`（`admin`），原有账号、订阅与记录零丢失。
2. **共享 UID 规则（规则 B）**：
   - 多位白名单用户均可通过真实游戏登录绑定同一游戏 UID，共享该 UID 的自动报名订阅与报名记录（全服仍由唯一 `sve-agent` 单执行点调度）。
   - 单人点击「删除」仅解除该用户与该 UID 的绑定；仅当最后一位绑定者解除绑定（引用归零）时，才停止该 UID 的订阅并写入墓碑。
   - 自动报名完成或时间冲突通知按当前绑定该 UID 的所有用户分别投递到各自已启用的通知渠道。
3. **成对回退**：
   - 若通过 `deploy/release.py rollback --target <install_dir> --writers-stopped` 回退至旧版本代码，旧版本直接忽略 `users-v1.json` 与 `users/` 目录，继续读取完好的 `identity-v2.json` 与 `<SVE_DATA_DIR>/notify.json`。
   - 若仅需临时退回单用户模式，将 `SVE_AUTH_MODE=single`（或注释掉 `SVE_AUTH_MODE`）并重启 `sve-agent` 即可。
