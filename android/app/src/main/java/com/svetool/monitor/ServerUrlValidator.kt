package com.svetool.monitor

import java.net.URI

/**
 * 服务器根地址（HTTPS origin）校验与规范化。
 *
 * 保持纯 Kotlin（无 Android 框架依赖），便于 JVM 单元测试覆盖各种边界输入。
 */
object ServerUrlValidator {

    sealed class ValidationResult {
        data class Valid(val origin: String) : ValidationResult()
        data class Invalid(val error: String) : ValidationResult()
    }

    fun validateAndNormalize(raw: String): ValidationResult {
        val trimmed = raw.trim()
        if (trimmed.isEmpty()) {
            return ValidationResult.Invalid("请输入服务器地址")
        }

        val normalizedInput = trimmed.trimEnd('/')
        val uri = runCatching { URI(normalizedInput) }.getOrNull()
            ?: return ValidationResult.Invalid("地址格式无效，请输入完整的 HTTPS 地址")

        val scheme = uri.scheme?.lowercase()
        if (scheme != "https") {
            return ValidationResult.Invalid("仅支持 HTTPS 服务器地址（请以 https:// 开头）")
        }

        if (uri.rawUserInfo != null) {
            return ValidationResult.Invalid("服务器地址不能包含用户名或密码")
        }

        if (uri.rawAuthority?.endsWith(":") == true) {
            return ValidationResult.Invalid("服务器端口号无效")
        }

        val host = uri.host?.trim()?.lowercase()
        if (host.isNullOrEmpty()) {
            return ValidationResult.Invalid("请输入有效的服务器域名或地址")
        }

        if (host == "example.com" || host.endsWith(".example.com")) {
            return ValidationResult.Invalid("请填写实际服务器地址，不能使用示例域名")
        }

        if (uri.port != -1 && uri.port !in 1..65535) {
            return ValidationResult.Invalid("服务器端口号必须在 1~65535 之间")
        }

        if (uri.rawQuery != null || uri.rawFragment != null) {
            return ValidationResult.Invalid("请只填写服务器根地址（不要包含查询参数或 # 片段）")
        }

        if (!uri.rawPath.isNullOrEmpty()) {
            return ValidationResult.Invalid("请只填写服务器根地址（不要包含子路径）")
        }

        val origin = if (uri.port != -1 && uri.port != 443) {
            "https://$host:${uri.port}"
        } else {
            "https://$host"
        }
        return ValidationResult.Valid(origin)
    }
}
