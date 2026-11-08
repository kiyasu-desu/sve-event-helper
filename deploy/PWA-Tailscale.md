# 手机访问：PWA + Tailscale 接入指南

把赛事助手装到手机主屏，随时随地订阅/取消/看状态。

**方案要点**：不写原生 App、不上架商店。界面本身就是网页，加一层 PWA 外壳即可；
网络用 Tailscale 组私有网，**公网零暴露、代理无需改绑定地址**。

---

## 一、先搞清楚一件事：为什么不能直接开 `http://<IP>:8901`

浏览器的 **Service Worker** 与**「安装应用」入口**都只在**安全上下文**（Secure Context）下可用。
安全上下文只有两种：

| 情况 | 是否安全上下文 |
|---|---|
| `https://...`（证书有效） | ✅ |
| `http://localhost` / `http://127.0.0.1` | ✅ |
| **`http://<局域网IP>:8901`** | ❌ |
| `http://<tailscale IP>:8901` | ❌ |

所以直接开 `http://100.x.x.x:8901` 会表现为：

- 页面能正常打开、功能都好用 ✅
- 但**没有安装提示**、Service Worker **不注册**、离线壳**没有** ❌

**这不是坏了**，是平台的硬性要求。要拿到「像 App 一样」的体验，必须有一个**证书有效的 HTTPS 地址**。

Tailscale 的 **Serve** 正好提供这个：给每台机器签发 `https://<机器名>.<你的tailnet>.ts.net`
的有效证书，并把流量反代到你本机的 `127.0.0.1:8901`。

```
手机 ──HTTPS(有效证书)──> tailscale ──> 服务器 127.0.0.1:8901（sve-agent）
                                          ↑ 仍然只绑本地回环，公网不可达
```

---

## 二、服务器端

### 1. 安装并登录 Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

按提示打开输出的链接、用你的账号登录。记下这台机器的 Tailscale 名称与 IP：

```bash
tailscale status
tailscale ip -4
```

### 2. 启用 HTTPS 证书（**关键前提**）

`tailscale serve` 要签发证书，必须先在管理后台开启两项：

1. 打开 <https://login.tailscale.com/admin/dns>
2. 启用 **MagicDNS**
3. 在同一页找到 **HTTPS Certificates**，点击启用

> 不开这两项，`tailscale serve` 会报证书相关错误。

### 3. 把 HTTPS 反代到本地代理

```bash
# 把 https://<机器名>.<tailnet>.ts.net/ 反代到本机 127.0.0.1:8901
sudo tailscale serve --bg 8901
```

`--bg` 表示后台常驻（不占用当前终端）。也可以用显式写法：

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8901
```

查看与撤销：

```bash
tailscale serve status      # 看当前映射
sudo tailscale serve reset  # 撤销全部映射
```

> **不要**用 `tailscale funnel`。`serve` 只在你的 tailnet 内可见；`funnel` 会把它暴露到公网。

### 4. 拿到手机要访问的地址

```bash
tailscale serve status
```

输出的 `https://<机器名>.<tailnet>.ts.net` 就是手机要打开的地址。

---

## 三、手机端

1. 应用商店安装 **Tailscale**，登录**同一个账号**（同一 tailnet）
2. 确认 Tailscale 处于连接状态
3. 用 **Chrome** 打开上一步的 `https://<机器名>.<tailnet>.ts.net`
4. 页面正常显示后，Chrome 菜单 →
   - **「安装应用」**（新版 Chrome），或
   - **「添加到主屏幕」**（旧版）
5. 主屏出现 **SVE** 图标，点开是全屏无地址栏的独立窗口

> 若菜单里没有安装项：确认地址是 `https://`（不是 `http://`），
> 且证书有效（地址栏无警告）。见第五节的排查。

---

## 四、安全须知（**必读**）

⚠️ **当前界面没有任何认证。**

任何能打开这个地址的人，都能看到你的账号列表、订阅状态，并能**替你订阅/取消报名**。

因此：

| 做法 | 说明 |
|---|---|
| ✅ **只用 `tailscale serve`** | 默认仅 tailnet 内可见，认证由 Tailscale 在**网络层**完成 |
| ❌ **不要用 `tailscale funnel`** | 会把界面暴露到公网，等于把你的报名能力公开 |
| ❌ **不要把代理改成 `0.0.0.0`** | 保持 `127.0.0.1`，让它只能被本机/Tailscale 反代访问 |
| ⚠️ 定期检查 tailnet 成员 | 别人加入你的 tailnet 就等于获得这个界面的访问权 |
| ⚠️ 若要公网访问 | **必须先加认证**（至少一个访问令牌）+ HTTPS，不要跳过 |

另外记住：**手机只连你自己的代理，不直连游戏接口**。这意味着

- `appKey`（签名密钥）只存在于服务器，**永不落到手机上**
- 手机丢失不会泄露签名能力；只需在 Tailscale 后台移除该设备

---

## 五、排查

| 现象 | 原因 / 处理 |
|---|---|
| 页面能开，但**没有「安装应用」** | 地址不是 HTTPS。确认用的是 `https://<机器>.<tailnet>.ts.net`，不是 `http://<IP>:8901` |
| `tailscale serve` 报证书错误 | 后台的 **MagicDNS** 或 **HTTPS Certificates** 没开 |
| 手机打不开地址 | 手机 Tailscale 未连接；或不在同一 tailnet |
| 装了 App 但显示旧界面 | Service Worker 缓存。**下拉刷新**，或先在浏览器里「清除网站数据」；开发者改界面后应把 `app/client/ui/sw.js` 顶部的 `VERSION` 加一 |
| App 里看不到实时名额 | 正常设计：`/api/*` **永不缓存**，永远是实时值。若显示不出，说明与服务器断开了 |
| 想撤销手机访问 | Tailscale 后台移除该设备，或 `sudo tailscale serve reset` |

---

## 六、开发者备忘

- PWA 资产：`app/client/ui/manifest.json`、`sw.js`、`icon-*.png`
- 静态文件由 `server.py` 的通配路由 `@app.get("/<path:fn>")` 提供，
  `sw.js` 与 `manifest.json` 会带 `Cache-Control: no-store`（避免 SW 更新卡住）
- **改动界面后请把 `sw.js` 的 `VERSION` 加一**，激活时会清掉所有旧缓存
- 在 `http://127.0.0.1:8801`（本机）验证最方便：localhost 是安全上下文，
  SW 可正常注册，Chrome DevTools → Application → Service Workers 可查看状态
