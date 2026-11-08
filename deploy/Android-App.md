# Android App —— 在手机上新增网易账号

> 这是一个**极薄的 WebView 外壳**：主界面直接加载服务器上的 Web UI
> （`https://sve.example.com`），只额外提供一件网页做不到的事——**新增账号**。

---

## 一、为什么需要原生 App

现有 PWA 已经完整可用，但它**无法新增账号**。原因是原理性的，不是实现问题：

| 环节 | 说明 |
|---|---|
| 换取 SVE 会话需要**网易的会话 Cookie**（`NTES_YD_SESS` / `NTES_SESS` / `l_yd_sign`） | 这些 Cookie 是 **HttpOnly** 的 |
| PWA 里的 JS | **读不到 HttpOnly Cookie**；而且它们属于 `*.nie.netease.com`，跨域也拿不到 |
| 服务器 | 无头环境，**没有浏览器可开**（会报「未找到 Chrome 或 Edge」） |
| **原生 WebView** | ✅ Android 的 `CookieManager` 能读到 App 内 WebView 产生的 Cookie |

所以：**只有原生 App 能做这件事**。这也正是选它而不是继续用 PWA 的唯一理由。

---

## 二、它是怎么工作的

```
┌─ MainActivity ─────────────────────────────────────────┐
│  WebView → https://sve.example.com                    │
│  （复用全部现有界面：账号/赛事/订阅/通知设置…）        │
│  ＋ 一个原生悬浮按钮「新增账号」                        │
└──────────────────────┬─────────────────────────────────┘
                       │ 点击
┌──────────────────────▼─────────────────────────────────┐
│  LoginActivity                                         │
│   ① WebView 打开**官方登录页**，用户正常登录           │
│      （手机号+验证码 / 密码 / 扫码，取决于页面提供）   │
│   ② 轮询 CookieManager，直到出现会话 Cookie            │
│   ③ WebView 切到自家域名（Access Cookie 是 App 级共享）│
│   ④ **同源 fetch** 把 Cookie POST 给 /api/login/cookie │
└──────────────────────┬─────────────────────────────────┘
                       │
┌──────────────────────▼─────────────────────────────────┐
│  服务器 /api/login/cookie                              │
│   SveClient.login_with_cookie() → sessionid            │
│   → /urs/info 取昵称/uid → 写入 identity-v2.json          │
│   → 校验身份和冲突后保存单个账号，不整表同步          │
└────────────────────────────────────────────────────────┘
```

### 两个关键设计

**① 为什么第 ④ 步要走 WebView 的 fetch，而不是原生 HTTP**

`/api/login/cookie` 受 **Cloudflare Access** 保护，Access 的 JWT 存在 WebView 的
Cookie 里。原生的 HTTP 客户端（OkHttp 之类）拿不到它，会直接被 Access 挡在门外。
让 WebView 自己发这个请求，就自然带上了认证。

**② 为什么第 ③ 步要切到自家域名**

从登录页（`login-betasystem.nie.netease.com`）直接 fetch 我们的 API 属于**跨站请求**：
CORS 会拦，`SameSite=Lax` 的 Access Cookie 也不会带上。先让 WebView 导航到
`https://sve.example.com`，再发 fetch 就是**同源**，一切正常。

---

## 三、凭据边界（重要）

| 项 | 是否进入 App |
|---|---|
| appKey（签名密钥） | ❌ **不进**。签名只在服务器做 |
| sessionid | ❌ **不进**。App 不保存会话 |
| 网易账号密码 | ❌ **不进**。密码只输入到官方登录页，App 不接触 |
| 网易 Cookie | ⚠️ **仅在内存中搬运一次**，POST 给服务器后即丢弃，不落盘 |
| 服务器地址 | ✅ 私有构建配置注入（`Cfg.BASE_URL`） |

App 是浏览器外壳，WebView 会保存认证 Cookie；丢失设备后应撤销其访问权限。私人 APK 包含服务地址，不上传 GitHub 或 Releases。

---

## 四、服务器侧接口

### `POST /api/login/cookie`

```jsonc
// 请求
{ "cookies": "NTES_YD_SESS=xxx; l_yd_sign=yyy" }   // 也接受 {"名": "值"} 字典
// 可选："udid" —— 不传则用服务端默认值

// 成功
{ "ok": true, "name": "昵称", "uid": 123456, "nickname": "昵称" }

// 失败（一律 HTTP 200 + ok:false，附可读原因）
{ "ok": false, "error": "Cookie 里没有会话项（需要 NTES_SESS / NTES_YD_SESS / l_yd_sign）" }
{ "ok": false, "error": "换取会话失败：响应中没有 session_id" }
```

**安全约定**：

- 响应里**绝不回显 sessionid、也绝不回显手机号**
- 日志只记 Cookie **个数**与失败原因，不记内容
- 提前校验是否含会话 Cookie 名——实测坏 Cookie 时上游会返回 200 但不带
  `session_id`（不是清晰报错），提前拦一道能让失败原因更明确

---

## 五、构建

### 前置

| 组件 | 版本 | 说明 |
|---|---|---|
| JDK | 17+ | 实测 **JDK 22 可用** |
| Android SDK | platform **android-34** + build-tools **34.0.0** | 只需命令行工具，不需要 Android Studio |
| Gradle | **8.10.2** | 无需 wrapper，直接用发行版即可 |

安装 SDK（命令行工具）：

```powershell
$sdk = 'D:\Android\Sdk'
# cmdline-tools 解压到 $sdk\cmdline-tools\latest
& "$sdk\cmdline-tools\latest\bin\sdkmanager.bat" --sdk_root="$sdk" --licenses
& "$sdk\cmdline-tools\latest\bin\sdkmanager.bat" --sdk_root="$sdk" `
    "platform-tools" "platforms;android-34" "build-tools;34.0.0"
```

> 国内网络下 Gradle 发行版建议用镜像（华为云 15 秒 vs 官方源 30 分钟）：
> `https://mirrors.huaweicloud.com/gradle/gradle-8.10.2-bin.zip`

`android/local.properties`（**已被 gitignore，不要提交**）：

```properties
sdk.dir=D\:\\Android\\Sdk
```

### 私有服务地址（构建必填）

复制 `android/private.properties.example` 为 `android/private.properties`，在本机填入**现有生产 HTTPS origin**；也可用环境变量 `SVE_BASE_URL`（优先级更高）。不要把真实地址放进命令行参数、源码、构建日志或 PR。缺失地址、示例域名、带路径/查询/凭据的地址都会使 Gradle 配置失败，避免生成连向示例站点的包。

示例文件中的 `https://sve.example.com` 仅说明格式，不能直接用于构建。APK 仍然包含注入后的地址，是私人产物。既有 APK、域名、Cloudflare Access 和隧道无需更换；本次没有后端协议变化。

### 出包

```powershell
$env:JAVA_HOME = '<你的 JDK 路径>'
$env:ANDROID_HOME = 'D:\Android\Sdk'
cd sve-monitor\android
& 'D:\Android\gradle\gradle-8.10.2\bin\gradle.bat' --no-daemon assembleDebug
```

产物：

```
android/app/build/outputs/apk/debug/app-debug.apk
```

（`*.apk` 与 `build/` 均在 `.gitignore` 内，不会进仓库。）

---

## 六、侧载安装

1. 把 `app-debug.apk` 传到手机（数据线 / 网盘 / `adb install`）
2. 手机上允许「安装未知来源应用」（首次会提示）
3. 安装后打开 → 桌面出现「SVE 赛事助手」

> 用 `adb` 更省事：`adb install -r app-debug.apk`

---

## 七、新增账号怎么用

1. 打开 App（主界面就是熟悉的 Web UI）
2. 点右下角 **「新增账号」** 悬浮按钮
3. 在弹出的官方登录页里**正常登录**（手机号+验证码 / 密码，看页面给什么）
4. 登录成功后 App 会自动：
   - 读出会话 Cookie
   - 交给服务器换取 `sessionid`
   - 校验身份后保存单个账号到服务器的 `identity-v2.json`
5. 回到主界面，新账号出现在账号列表里

---

## 八、已知限制与排查

| 现象 | 原因与处理 |
|---|---|
| 一直停在「等待登录完成…」 | 登录尚未真正成功。确认页面上确实已完成登录；**手机号+验证码**这条路出来的是 `NTES_YD_SESS`，已包含在检测名单里 |
| 「换取会话失败：响应中没有 session_id」 | Cookie 还没生效（页面仍在跳转）或登录未成功。回登录页确认 |
| 「Cookie 里没有会话项…」 | App 读到的 Cookie 里没有会话名——通常是登录没走完 |
| 主界面白屏 | 检查能否用手机浏览器打开 `https://sve.example.com`；若浏览器也不行，是 Access/隧道问题，与本 App 无关 |
| 主界面要求登录 | 那是 **Cloudflare Access** 的验证（邮箱验证码），与网易账号无关；过一次即可 |
| 新增账号后主界面没更新 | 会自动 `reload()`；若没有，手动下拉或重开 App |

**限制**：

- 登录页若出现**滑块验证**，需要在 WebView 里手动完成（App 不干预，也干预不了）
- 登录页若改版，Cookie 名或流程可能变化——检测名单在 `Cfg.SESSION_COOKIE_NAMES`，
  与电脑端 `weblogin.py` 的 `SESSION_COOKIE_NAMES` 保持一致
- App 只做「加账号」这一件原生事，其余全部复用网页。**网页更新后 App 无需重新打包**

---

## 九、目录结构

```
android/
├── settings.gradle.kts
├── build.gradle.kts
├── gradle.properties
├── .gitignore              # 忽略 build/、*.apk、local.properties、签名材料
└── app/
    ├── build.gradle.kts
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/svetool/monitor/
        │   ├── Cfg.kt            # 地址与 Cookie 名（无任何凭据）
        │   ├── MainActivity.kt   # WebView 主界面 + 「新增账号」按钮
        │   └── LoginActivity.kt  # 登录页 + Cookie 读取 + 同源 POST
        └── res/
            ├── values/{strings,themes,colors}.xml
            ├── drawable/ic_launcher_foreground.xml   # 矢量图标，仓库内无二进制图片
            └── mipmap-anydpi-v26/ic_launcher.xml
```
