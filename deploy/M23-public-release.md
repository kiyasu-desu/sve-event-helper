# M23 项目公开化整理、侧栏去私有提示与通用签名版 Android APK 发布指引

日期：2026-11-19。分支：`codex/m23-public-release-and-generic-apk`（基于 `main` 提交 `8a46438`）。

---

## 一、GitHub 仓库 About 推荐配置（纯中文）

本机未安装 `gh` CLI，请在 GitHub 仓库主页右上角 **About → ⚙️（Edit repository details）** 手动填写：

- **Description（纯中文）**：
  ```text
  《影之诗：进化对决》通用赛事自动报名助手，支持多账号管理、按地区订阅自动抢位与微信/邮件通知，提供 Web/PWA 控制面板与 Android 辅助登录客户端。
  ```
- **Website**：留空（由自部署用户各自托管，避免指向私人域名）
- **Topics**：
  ```text
  shadowverse-evolve, sve, automation, flask, pwa, android, python
  ```

---

## 二、本地正式签名出包与 GitHub Releases 上传步骤

通用版 Release APK（`versionCode = 3`、`versionName = "1.6"`）不包含任何服务器地址与私人凭据，可在维护者本机用独立签名密钥打包后公开上传到 GitHub Releases。

### 1. 生成并保管正式签名密钥（仓库外保存）

如果尚未生成正式发布密钥库，请在仓库目录**之外**的受保护路径执行（示例放在 `%USERPROFILE%\.keys\sve-release.jks`，请自行妥善备份密码与文件，切勿放入仓库）：

```powershell
mkdir "$env:USERPROFILE\.keys" -Force
keytool -genkeypair -v `
  -keystore "$env:USERPROFILE\.keys\sve-release.jks" `
  -alias sve-release `
  -keyalg RSA -keysize 2048 -validity 10000
```

> 后续发布升级包必须使用同一把正式签名密钥，否则用户手机无法直接覆盖升级。

### 2. 配置本地签名文件（已被 `.gitignore` 排除）

复制 `android/keystore.properties.example` 为 `android/keystore.properties`（或改用环境变量 `SVE_KEYSTORE_FILE`、`SVE_KEYSTORE_PASSWORD`、`SVE_KEY_ALIAS`、`SVE_KEY_PASSWORD`）：

```properties
storeFile=C:\\Users\\you\\.keys\\sve-release.jks
storePassword=<你的密钥库口令>
keyAlias=sve-release
keyPassword=<你的密钥别名口令>
```

> 安全提醒：`android/keystore.properties`、`*.jks`、`*.keystore` 与 `android/private.properties` 均已被 `.gitignore` 与 `scripts/security/scan_repository.py` 拦截，严禁提交到 Git。

### 3. 构建通用签名版 `app-release.apk`

确保**不要**传入 `-PsveUsePrivateUrl=true`（Gradle 也会在任何包含 `release` 的任务中强制拦截该参数，并将 `BuildConfig.SVE_BASE_URL` 固定为空串）：

```powershell
$env:ANDROID_HOME = 'D:\Android\Sdk'
Set-Location android
& 'D:\Android\gradle\gradle-8.10.2\bin\gradle.bat' --no-daemon assembleRelease
```

产物路径：`android/app/build/outputs/apk/release/app-release.apk`。

### 4. 出包前核查产物与计算 SHA-256

1. 核对 `android/app/build/generated/source/buildConfig/release/com/svetool/monitor/BuildConfig.java` 中 `SVE_BASE_URL` 为 `""`。
2. 核对 APK 签名与条目中不包含任何私人域名（例如用 `apksigner verify --print-certs` 与离线字符串检索确认）：
   ```powershell
   & 'D:\Android\Sdk\build-tools\34.0.0\apksigner.bat' verify --print-certs app\build\outputs\apk\release\app-release.apk
   Get-FileHash -Algorithm SHA256 app\build\outputs\apk\release\app-release.apk
   ```

### 5. 创建 Tag 并上传 GitHub Release

确认分支已合入 `main`（或指定发布提交）后，打 Tag 并推送（推送与合入需单独授权）：

```bash
git tag v1.6.0
git push origin v1.6.0
```

然后在 GitHub 网页进入 **Releases → Draft a new release**：
- **Choose a tag**：`v1.6.0`
- **Release title**：`v1.6.0 — 通用公开版 Android 客户端（运行时配置服务器地址）`
- **Attach binaries**：上传 `app-release.apk`（切勿误传含私人地址的私有调试 APK 或 `.jks` 文件）
- **Release notes（纯中文模板）**：

```markdown
## 更新内容

- **通用签名版 Android 客户端（v1.6 / versionCode 3）**：不再内置任何服务器地址与私人配置，首次启动时由用户填写自己的 HTTPS 服务地址（例如 `https://sve.example.com` ），支持校验、断网兜底切换、设置页「更换服务器地址」及长按桌面图标快捷方式修改。
- **Web / PWA 界面公开化整理**：移除侧边栏过时的「私有仓库需 GitHub 登录及访问权限」提示；Service Worker 升级至 `sve-v28-public-repo` 自动刷新离线壳。
- **文档泛化整理**：完善 `README.md` 与 `deploy/Android-App.md` 的自部署、HTTPS 入口及 Release APK 使用指引。

## 使用说明

1. 请先按仓库 `README.md` 与 `deploy/README.md` 部署你自己的服务端并接入 HTTPS（如 Cloudflare Tunnel + Access 或 Tailscale Serve）；本 APK 不自带服务器或通知凭据。
2. 安装 `app-release.apk` 后，按引导输入服务端 HTTPS 根地址并完成认证即可进入主界面。
3. 通知渠道（PushPlus / 邮件）与 `SVE_APP_KEY` 需在自部署环境或登录后「设置」页自行配置。
4. 兼容现有服务端 `/api/login/cookie` 与既有工作流；普通后台网页更新仍无需重装 APK。
5. 既有通过旧版 `assembleDebug` 自建安装的用户无需被迫切换；若保留本地 WebView / Cloudflare Access Cookie 原地升级原生端，请在原构建机继续运行 `assembleDebug`（`versionCode = 3`）；若首次切换为本公开正式签名版 `app-release.apk`，需先卸载旧 `debug` 包（服务端账号与订阅不会丢失，仅需重新填写服务器地址并过一次 Access 验证），此后同签名 Release 均可直接覆盖升级。

## 校验信息

- 文件名：`app-release.apk`
- SHA-256：`<填入本地 Get-FileHash 输出>`
```

---

## 三、离线验证范围与未实测边界

- **本地自动化验证范围**：
  - Web UI / SW 离线回归（`tests/test_identity_ui.js`）与 HTML/JS 静态校验（`scripts/recon/check_ui.py`）；
  - 后端/调度/发布 Python 单元测试（`tests/test_m*.py`）及脚本/行尾/PowerShell/越界写静态检查（`check_scripts.py`、`check_lineendings.py`、`check_ps1.py`、`audit_paths.py`）；
  - Android 单元测试（`LoginGateTest` 与 `ServerUrlValidatorTest`）、Debug 构建（`testDebugUnitTest assembleDebug`）、`release` 防私有地址构建拦截，以及临时测试 keystore 的 `assembleRelease` 签名与产物域名洁净度检查；
  - `scripts/security/test_scan_repository.py` 与 `scripts/security/scan_repository.py`（`working` / `index` / `tree --ref HEAD` / `range --ref main..HEAD`）全量隐私门禁。
- **既有安装与升级边界**：
  - 本发布版沿用 `AGENTS.md` 兼容性约束：既有 v1.5 `app-debug.apk` 不要求重装即可兼容服务端与 Web 更新；需保留本地 WebView Cookie 升级原生端时可沿用同机 `assembleDebug`（`versionCode = 3`）；首次切换至公开正式签名 `app-release.apk` 渠道需先卸载旧 `debug` 包（不丢失任何服务端账号/订阅/记录，回退同理装回原 `app-debug.apk`），详见 [Android-App.md](Android-App.md#升级与渠道迁移说明保留兼容性与数据)。
- **未实测边界（需发布前/真机人工验收）**：
  - 实体 Android 手机侧载 `app-release.apk` 后的首次冷启动引导、错误兜底页触发、长按桌面图标快捷方式、设置页 `sve://server` 跳转、以及在真实 Cloudflare Access 401/403 跳转链与网易官方登录页下的端到端操作；
  - 浏览器/PWA 在真实手机上从 `sve-v27-m22-batch-cancel` 升级至 `sve-v28-public-repo` 的实际联网刷新；
  - GitHub 网页端 About 栏填写、Tag 推送及 Release 附件上传（本机未安装 `gh` CLI 且按规则未执行远端推送/部署）。
