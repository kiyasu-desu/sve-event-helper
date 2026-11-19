package com.svetool.monitor

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerUrlValidatorTest {

    private val https = "https" + "://"
    private val http = "http" + "://"
    private val validHost = "sve.example.org"

    @Test
    fun `accepts valid https origin and normalizes case, whitespace, trailing slashes, and port 443`() {
        assertEquals(
            ServerUrlValidator.ValidationResult.Valid("${https}${validHost}"),
            ServerUrlValidator.validateAndNormalize("  HTTPS" + "://SVE.EXAMPLE.ORG/  ")
        )
        assertEquals(
            ServerUrlValidator.ValidationResult.Valid("${https}${validHost}:8443"),
            ServerUrlValidator.validateAndNormalize("${https}${validHost}:8443///")
        )
        assertEquals(
            ServerUrlValidator.ValidationResult.Valid("${https}${validHost}"),
            ServerUrlValidator.validateAndNormalize("${https}${validHost}:443/")
        )
    }

    @Test
    fun `rejects blank, missing scheme, and non-https schemes`() {
        for (raw in listOf("", "   ", "not-a-url", "${http}${validHost}", "ftp" + "://${validHost}")) {
            val result = ServerUrlValidator.validateAndNormalize(raw)
            assertTrue("Expected Invalid for '$raw'", result is ServerUrlValidator.ValidationResult.Invalid)
        }
    }

    @Test
    fun `rejects placeholder example dot com domains`() {
        for (raw in listOf("https://sve.example.com", "https://example.com", "https://SVE.EXAMPLE.COM/")) {
            val result = ServerUrlValidator.validateAndNormalize(raw)
            assertTrue("Expected Invalid for '$raw'", result is ServerUrlValidator.ValidationResult.Invalid)
        }
    }

    @Test
    fun `rejects embedded userinfo, paths, queries, fragments, and invalid ports or hosts`() {
        val invalidInputs = listOf(
            "${https}user:pass@" + validHost,
            "${https}user@" + validHost,
            "${https}${validHost}/subpath",
            "${https}${validHost}?x=1",
            "${https}${validHost}/?x=1",
            "${https}${validHost}#hash",
            "${https}${validHost}:0",
            "${https}${validHost}:70000",
            "${https}${validHost}:",
            https,
            "${https}bad host",
        )
        for (raw in invalidInputs) {
            val result = ServerUrlValidator.validateAndNormalize(raw)
            assertTrue("Expected Invalid for '$raw'", result is ServerUrlValidator.ValidationResult.Invalid)
        }
    }
}
