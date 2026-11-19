package com.svetool.monitor

import android.content.Context

/**
 * 运行时服务器地址持久化配置。
 *
 * 公开 Release APK 不内置服务器地址（BuildConfig.SVE_BASE_URL 为空串），
 * 由用户首次启动时填写并保存在应用私有 SharedPreferences 中。
 */
object ServerConfig {

    private const val PREFS_NAME = "sve_prefs"
    private const val KEY_BASE_URL = "server_base_url"

    fun getBaseUrl(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val saved = prefs.getString(KEY_BASE_URL, "").orEmpty().trim()
        if (saved.isNotEmpty()) {
            val validated = ServerUrlValidator.validateAndNormalize(saved)
            if (validated is ServerUrlValidator.ValidationResult.Valid) {
                return validated.origin
            }
        }
        val builtIn = BuildConfig.SVE_BASE_URL.trim()
        if (builtIn.isNotEmpty()) {
            val validated = ServerUrlValidator.validateAndNormalize(builtIn)
            if (validated is ServerUrlValidator.ValidationResult.Valid) {
                return validated.origin
            }
        }
        return ""
    }

    fun setBaseUrl(context: Context, raw: String): ServerUrlValidator.ValidationResult {
        val result = ServerUrlValidator.validateAndNormalize(raw)
        if (result is ServerUrlValidator.ValidationResult.Valid) {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            prefs.edit().putString(KEY_BASE_URL, result.origin).commit()
        }
        return result
    }

    fun clearBaseUrl(context: Context) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit().remove(KEY_BASE_URL).commit()
    }
}
