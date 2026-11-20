# Android App —— 在手机上新增网易账号

> 这是一个**极薄的 WebView 外壳**：公开版本**不内置任何服务器地址与凭据**，首次启动引导用户输入自己的 HTTPS 服务地址（例如 `https://sve.example.com`），主界面加载服务器上的 Web UI，并额外提供网页做不到的原生能力——**新增账号**。

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
┌─ ServerSetupActivity（首次启动或切换服务器）───────────┐
│  用户输入并校验 HTTPS origin（禁止明文 HTTP、路径与凭据）
│  写入应用私有 SharedPreferences，不入仓库与打包产物     │
└──────────────────────┬─────────────────────────────────┘
                       │ 进入主界面
┌─ MainActivity ───────▼─────────────────────────────────┐
│  WebView → 用户配置的 HTTPS 地址 (如 https://sve.example.com)
│  （复用全部现有界面：账号/赛事/订阅/通知设置…）        │
│  支持「新增账号」(sve://login)、「更换服务器地址」     │
│  (sve://server)、「退出登录」(sve://logout)、桌面快捷  │
│  方式及断网兜底页                                      │
└──────────────────────┬─────────────────────────────────┘
                       │ 点击「新增账号」
┌──────────────────────▼─────────────────────────────────┐
│  LoginActivity                                         │
│   ① WebView 打开**官方登录页**，用户正常登录           │
│      （手机号+验证码 / 密码 / 扫码，取决于页面提供）   │
│   ② 轮询 CookieManager，直到出现会话 Cookie            │
│   ③ WebView 切到用户配置的域名（Access Cookie App 级共享）
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
CORS 会拦，`SameSite=Lax` 的 Access Cookie 也不会带上。先让 WebView 导航到用户配置的
HTTPS 服务器地址（如 `https://sve.example.com`），再发 fetch 就是**同源**，一切正常。

---

## 三、凭据边界（重要）

| 项 | 是否进入 App |
|---|---|
| appKey（签名密钥） | ❌ **不进**。签名只在服务器做 |
| sessionid | ❌ **不进**。App 不保存会话 |
| 网易账号密码 | ❌ **不进**。密码只输入到官方登录页，App 不接触 |
| 网易 Cookie | ⚠️ **仅在内存中搬运一次**，POST 给服务器后即丢弃，不落盘 |
| 服务器地址 | ⚙️ **通用版由首次启动用户输入并保存在本机**（公开包 `BuildConfig.SVE_BASE_URL` 为空；仅 `-PsveUsePrivateUrl=true` 的私有 debug 构建可选内置，且严禁用于 release） |

App 是浏览器外壳，WebView 会保存认证 Cookie；丢失设备后应撤销其访问权限。通用签名版 `app-release.apk` 不包含任何私人服务器地址与通知密钥，可公开发布到 GitHub Releases；使用 `-PsveUsePrivateUrl=true` 打出的私有 debug 包仍包含服务地址，绝不能上传 GitHub 或 Releases。

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

### 推荐安装方式（普通用户）

普通用户无需搭建 Android 构建环境：直接前往项目 [GitHub Releases](https://github.com/kiyasu-desu/sve-event-helper/releases) 下载通用正式签名版 `app-release.apk`，安装后按引导输入自己的 HTTPS 服务器地址（如 `https://sve.example.com`）即可使用。

### 通用 APK 本地构建与 Release 正式签名（维护者）

默认无需 `private.properties` 即可运行调试构建与单元测试：

```powershell
$env:JAVA_HOME = '<你的 JDK 路径>'
$env:ANDROID_HOME = 'D:\Android\Sdk'
cd sve-monitor\android
& 'D:\Android\gradle\gradle-8.10.2\bin\gradle.bat' --no-daemon testDebugUnitTest assembleDebug
```

若要打包用于公开发布的正式签名版 `app-release.apk`：

1. 使用 `keytool` 生成签名密钥库（建议存放在仓库外安全目录），**严禁把 `.jks` / `.keystore` 提交到 Git**。
2. 复制 `android/keystore.properties.example` 为 `android/keystore.properties`（已被 `.gitignore` 排除）并填写密钥路径、别名与口令；或者设置环境变量 `SVE_KEYSTORE_FILE`、`SVE_KEYSTORE_PASSWORD`、`SVE_KEY_ALIAS`、`SVE_KEY_PASSWORD`。
3. 执行正式构建（不要传入 `-PsveUsePrivateUrl=true`，Gradle 在 release 任务中也会强制拦截私人地址）：

```powershell
& 'D:\Android\gradle\gradle-8.10.2\bin\gradle.bat' --no-daemon assembleRelease
```

产物位于 `android/app/build/outputs/apk/release/app-release.apk`（`*.apk` 与 `build/` 均在 `.gitignore` 内）。

### 可选：私有调试构建（仅限个人 debug 兼容）

如需给个人设备打一个预填地址的调试包，可复制 `android/private.properties.example` 为 `android/private.properties` 填入你的生产 HTTPS origin（或设置 `SVE_BASE_URL`），并在构建 **debug** 时显式传入 `-PsveUsePrivateUrl=true`：

```powershell
& 'D:\Android\gradle\gradle-8.10.2\bin\gradle.bat' --no-daemon assembleDebug -PsveUsePrivateUrl=true
```

- 仅当应用尚未保存运行时地址时才回退读取该编译期地址；既有的私人 APK、域名、Cloudflare Access 和隧道无需更换。
- `release` 与 `build` 等含 Release 的任务会无条件拒绝 `-PsveUsePrivateUrl=true` 并将 `BuildConfig.SVE_BASE_URL` 固定为空串。

---

## 六、安装与首次配置

1. 下载通用正式签名版 `app-release.apk`（或本地构建的 `app-debug.apk`）传到手机
2. 手机上允许「安装未知来源应用」（首次会提示）
3. 打开「SVE 赛事助手」，在「配置服务器地址」界面输入你的 HTTPS 服务根地址（如 `https://sve.example.com`）并保存；后续如需更换，可在 App 内「设置 → 更换服务器地址」、断网兜底页或长按桌面应用图标快捷方式进入修改

> 用 `adb` 更省事：`adb install -r app-release.apk`。

### 升级与渠道迁移说明（保留兼容性与数据）

Android 对同包名（`com.svetool.monitor`）强制校验签名证书一致；`assembleDebug` 使用各台构建机本地的 `debug.keystore`（公开固定口令、机器间证书互不相同），因此不能将其用于 GitHub Releases 公开分发，也无法通过跨证书密钥轮换直接覆盖。既有用户按自身需求选择以下三条路径之一：

1. **继续沿用既有私人或旧版 APK（免重装，<= v1.6）**：普通服务端与 Web/PWA 升级不要求重装 APK，现有 Cloudflare Access 登录态与 `/api/login/cookie` 兼容性完全保留；旧版 APK（`versionCode <= 3`）在网页侧点击「退出登录」时会自动兼容回退至 `/cdn-cgi/access/logout`。
2. **升级至 v1.7（`versionCode = 4`，支持原生 `sve://logout` 清空 WebView Cookie）**：v1.7 新增 `sve://logout` 桥接，可在 App 内一键清空 WebView 全域 Cookie（含 Cloudflare Access 团队域认证态）并重载服务器入口页以便切换账号。在原出包机上执行 `gradle --no-daemon assembleDebug`（或带 `-PsveUsePrivateUrl=true`）或使用同正式签名密钥打包 `app-release.apk`，因包名相同、签名一致且 `versionCode` 递增至 `4`，可直接 `adb install -r` 覆盖升级，不清除既有本机配置。
3. **一次性切换至公开正式签名 `app-release.apk` 渠道**：首次跨渠道安装前需先卸载旧 `app-debug.apk`，再安装 `app-release.apk`。卸载仅清除手机本机 WebView 缓存的 Cloudflare Access Cookie（重新进入填写 HTTPS 地址并完成一次 Access 验证即可），**账号、订阅、报名记录与设置均保存在服务端，不会丢失**；切换后由同一正式签名密钥签出的后续 `app-release.apk` 版本均可直接覆盖升级。如需回退回原 `debug` 渠道，同理卸载后装回原 `app-debug.apk` 即可。

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
├── keystore.properties.example   # 正式签名配置模板（占位值）
├── private.properties.example    # 可选私有调试地址模板（占位值）
├── .gitignore                    # 忽略 build/、*.apk、local.properties、private.properties、签名材料
└── app/
    ├── build.gradle.kts
    └── src/
        ├── main/
        │   ├── AndroidManifest.xml
        │   ├── java/com/svetool/monitor/
        │   │   ├── Cfg.kt                 # 登录页、scheme 与网易 Cookie 名（不含服务地址）
        │   │   ├── ServerUrlValidator.kt  # HTTPS origin 校验与规范化（纯 Kotlin）
        │   │   ├── ServerConfig.kt        # 运行时服务器地址持久化
        │   │   ├── ServerSetupActivity.kt # 首次启动/切换服务器配置页
        │   │   ├── MainActivity.kt        # WebView 主界面 + 断网兜底 + sve://login / server / logout 桥接 + 快捷方式入口
        │   │   ├── LoginGate.kt           # 登录 Cookie 稳定期判定（纯函数）
        │   │   └── LoginActivity.kt       # 登录页 + Cookie 读取 + 同源 POST
        │   └── res/
        │       ├── values/{strings,themes,colors}.xml
        │       ├── xml/shortcuts.xml               # 桌面长按「更换服务器」快捷方式
        │       ├── drawable/ic_launcher_foreground.xml   # 矢量图标，仓库内无二进制图片
        │       └── mipmap-anydpi-v26/ic_launcher.xml
        └── test/java/com/svetool/monitor/
            ├── LoginGateTest.kt
            └── ServerUrlValidatorTest.kt
```
