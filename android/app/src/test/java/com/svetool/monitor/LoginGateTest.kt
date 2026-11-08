package com.svetool.monitor

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * `LoginGate` 的单元测试。
 *
 * 为什么值得写：真机上的失败根因是「**陈旧 Cookie 通过了稳定期判定**」，
 * 而这条路径**无法在本机复现**（没有设备）。把它抽成纯函数后，这里就能
 * 直接断言"旧值永不触发提交"，而不是靠"看起来对"。
 *
 * 对应真机现象：登录页还是空表单（手机号都没填），App 却已显示
 * 「已检测到登录凭据」→ 提交旧会话 → `/urs/login` 换不出 session_id。
 */
class LoginGateTest {

    private val stable = 3000L
    private val old = "NTES_YD_SESS=OLD_VALUE"
    private val new = "NTES_YD_SESS=NEW_VALUE"

    /** 模拟轮询：从 0 开始，每 1.5 秒一次，跑 [durationMs]，返回出现过的判定。 */
    private fun run(
        baselineSig: String?,
        readings: (Long) -> String?,
        durationMs: Long,
        pollMs: Long = 1500L,
    ): List<LoginGate.Decision> {
        var lastSig: String? = null
        var since = 0L
        val out = ArrayList<LoginGate.Decision>()
        var t = 0L
        while (t <= durationMs) {
            val (d, next) = LoginGate.decide(baselineSig, readings(t), lastSig, since, t, stable)
            lastSig = next.first
            since = next.second
            out.add(d)
            t += pollMs
        }
        return out
    }

    // ------------------------------------------------------------------ 关卡①：挡陈旧 Cookie

    @Test
    fun `起点就有旧会话、且一直不变时_永远不提交`() {
        // 这正是真机上失败的那条路：cookie 库里有上次遗留的 NTES_YD_SESS。
        // 它一动不动 —— 若只看"稳定期"，3 秒后就会被放行（修复前的缺陷）。
        val decisions = run(baselineSig = old, readings = { old }, durationMs = 60_000)

        assertEquals("必须始终判为 Stale", 1, decisions.toSet().size)
        assertEquals(LoginGate.Decision.Stale, decisions.first())
        assertEquals(
            "陈旧 Cookie 绝不能被判为 Ready（哪怕等了 60 秒）",
            false, decisions.contains(LoginGate.Decision.Ready)
        )
    }

    @Test
    fun `起点无会话_Cookie_时_旧值不会出现_所以不受影响`() {
        // 起点为空（清理成功）：任何会话 Cookie 都是"新"的，稳定后即可提交。
        val decisions = run(baselineSig = null, readings = { new }, durationMs = 10_000)
        assertEquals(LoginGate.Decision.Ready, decisions.last())
    }

    // ------------------------------------------------------------------ 关卡②：挡中间态

    @Test
    fun `新值出现后_要等满稳定期才提交`() {
        // 0 秒时为新值 → 尚未稳定；3000ms 起才是 Ready
        val before = run(baselineSig = null, readings = { new }, durationMs = 1500)
        assertEquals(false, before.contains(LoginGate.Decision.Ready))

        val after = run(baselineSig = null, readings = { new }, durationMs = 3000)
        assertEquals(LoginGate.Decision.Ready, after.last())
    }

    @Test
    fun `值在飞_稳定期不断被重置_不会提前提交`() {
        // 每次读数都不同（模拟"值还在飞"）
        val decisions = run(
            baselineSig = null,
            readings = { t -> "NTES_YD_SESS=FLY_$t" },
            durationMs = 30_000,
        )
        assertEquals(
            "值一直变时不应提交",
            false, decisions.contains(LoginGate.Decision.Ready)
        )
    }

    // ------------------------------------------------------------------ 组合与实际路径

    @Test
    fun `旧值变成新值后_仍需稳定期才提交`() {
        // 真机上的正常路径：起点有旧值 → 用户登录出新值 → 稳定后提交
        val decisions = run(
            baselineSig = old,
            readings = { t -> if (t < 4500) old else new },
            durationMs = 15_000,
        )
        // 新值在 4500ms 出现，稳定期 3000ms → 最早 7500ms 才 Ready
        assertEquals(LoginGate.Decision.Stale, decisions[0])
        assertEquals(LoginGate.Decision.Ready, decisions.last())
        val firstReady = decisions.indexOfFirst { it == LoginGate.Decision.Ready }
        val firstNew = decisions.indexOfFirst { it == LoginGate.Decision.Settling }
        assertEquals(
            "Ready 至少要比新值出现晚 2 个轮询周期",
            true, firstReady - firstNew >= 2
        )
    }

    @Test
    fun `会话_Cookie_消失后重现_稳定期重新计时`() {
        // 用户返回上一步 / 页面跳转导致 Cookie 短暂消失
        val decisions = run(
            baselineSig = null,
            readings = { t -> if (t in 3000..6000) null else new },
            durationMs = 15_000,
        )
        // 消失期间应判 Waiting（计时重置），而不是沿用之前的计时直接 Ready
        assertEquals(true, decisions.contains(LoginGate.Decision.Waiting))
        assertEquals(LoginGate.Decision.Ready, decisions.last())
    }

    @Test
    fun `无会话_Cookie_时判为_Waiting`() {
        val decisions = run(baselineSig = null, readings = { null }, durationMs = 10_000)
        assertEquals(LoginGate.Decision.Waiting, decisions.first())
        assertEquals(1, decisions.toSet().size)
    }

    // ------------------------------------------------------------------ 配置层的安全约束

    @Test
    fun `清理列表绝不能包含自家域`() {
        // 这是硬约束：我们的域上有 Cloudflare Access 的 CF_Authorization JWT。
        // 一旦把自家域放进清理列表，主界面就会掉登录，用户得重新过一遍 Access。
        val bad = Cfg.COOKIE_CLEAR_URLS.filter { it.startsWith(Cfg.BASE_URL) }
        assertEquals("清理列表不得含自家域，实际含: $bad", emptyList<String>(), bad)

        // 反向确认：读 Cookie 的列表也不该碰自家域（避免把 Access 的 Cookie 当网易凭据提交）
        val bad2 = Cfg.COOKIE_URLS.filter { it.startsWith(Cfg.BASE_URL) }
        assertEquals("取 Cookie 列表不得含自家域，实际含: $bad2", emptyList<String>(), bad2)
    }

    @Test
    fun `会话_Cookie_名单不含辅助_Cookie`() {
        // l_yd_sign 出现得比真正的会话 Cookie 早，用它判定"登录完成"会导致过早提交。
        assertEquals(false, Cfg.SESSION_COOKIE_NAMES.contains("l_yd_sign"))
        assertEquals(true, Cfg.SESSION_COOKIE_NAMES.contains("NTES_YD_SESS"))
    }
}
