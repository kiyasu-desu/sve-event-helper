# GitHub 上传前脱敏检查

工具只读取 Git 与文件，不导入业务模块、不登录、不报名、不发送通知。支持 Python 3.10。输出只有类别、文件位置及数量，不输出匹配内容；错误详情也隐藏，退出码 0 表示无规则命中，1 表示发现问题，2 表示扫描未完成。

## 本地准备

复制 `config/private-scan.example.json` 为忽略文件 `config/private-scan.json`，在 `known_private` 数组中填入已知真实凭据、账号 UID/昵称、个人地址、SSH 用户/路径等原文。空数组只能进行通用规则检查，不能替代项目私有标识检查。配置应存放在受保护目录；可用 `--private-config` 指向仓库外文件。不要将实际列表、原始抓包或私人构建日志提交。

规则覆盖凭据赋值、私钥、手机号/邮箱、账号字面量、个人目录、未知 URL 主机、服务器 IP、抓包签名和私有文件。公开官方地址使用精确白名单；文档地址使用 example.com 或 RFC 5737 网段。合成账号范围与少数既有离线 Cookie 夹具显式列在规则中，不能把真实值加到白名单。扫描器无法判断任意昵称或混淆/拆分的密钥，仍需人工审阅代码、注释和提交说明。

## 每次提交与推送

```powershell
python -B -m unittest discover -s scripts/security -v
python scripts/security/scan_repository.py --mode working
python scripts/security/scan_repository.py --mode index
python scripts/security/scan_repository.py --mode tree --ref HEAD
python scripts/security/scan_repository.py --mode range --ref origin/main..HEAD
```

`working` 检查全部受跟踪的工作文件，不包含未跟踪文件；新增文件须显式暂存后运行 `index`。`index` 检查完整暂存树，不只是 diff；`tree` 检查完整提交树；`range` 对范围内每个提交检查完整树，能发现中途加入又删除的敏感数据。提交树、范围及历史扫描也检查作者/提交者和提交正文；个人邮箱应改用经确认的 noreply 地址或明确的合成维护者身份，不修改全局身份配置。必须先确认 origin/main 已更新且范围与实际推送一致。对照 `git diff --cached` 和候选提交，确认没有夹带私人数据或其他任务。图像需人工查看；其他二进制默认阻止，APK/密钥文件也不能上传。

首次检查或变更基线时：

```powershell
python scripts/security/scan_repository.py --mode history --ref HEAD
```

`history` 检查指定 tip 的全部可达历史；若需所有本地分支，分别指定各 tip。历史命中不应通过删除当前文件来掩盖。报告按类别/文件整理，不记录原值；真实匹配列表与扫描日志保留本地受保护目录。当前分支正常推送不会消除远端已有暴露。历史改写、强制推送及凭据轮换需要另行授权。

## Android 与抓包配置

Android 从环境变量 `SVE_BASE_URL` 或忽略文件 `android/private.properties` 读取现有 HTTPS origin。示例配置不能直接构建；缺配置明确失败。私人 APK 含地址，不能放在 PR、Releases 或仓库。此改动不修改既有手机应用和服务端协议。

三个签名探针从 `SVE_SIGNING_SAMPLES_FILE` 或忽略的 `config/signing-samples.json` 读取样本，格式见合成模板。不要把真实请求样本重新写回源码。探针本身会读取私人密钥，不应把其输出当作可公开附件。
