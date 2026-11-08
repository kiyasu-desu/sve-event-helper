package com.svetool.monitor

/**
 * 「什么时候可以把 Cookie 交给服务器换取会话」的判定逻辑。
 *
 * 抽成**纯函数**，是为了能用单元测试证明它挡得住**陈旧 Cookie** —— 那正是真机上
 * 反复失败的根因，必须可验证，而不是"看起来对"。
 *
 * 两道关卡，缺一不可：
 *
 *   ① **必须与本次登录的起点不同** —— 挡「上次遗留的旧会话」
 *   ② 连续稳定 [stableMs] —— 挡「值还在飞」的中间态
 *
 * 为什么只有②不够（实测踩过）：**陈旧 Cookie 是最"稳定"的东西** —— 它一动不动，
 * 所以"稳定 3 秒"对它天然通过。现象是：登录页还是空表单（手机号都没填），
 * App 就已经显示"已检测到登录凭据"，随后必然换不出 session_id 而失败。
 */
object LoginGate {

    /** 判定结果。 */
    sealed class Decision {
        /** 当前没有会话 Cookie：继续等。 */
        object Waiting : Decision()

        /** 有会话 Cookie，但与起点**相同** = 上次遗留的旧值：**不可提交**。 */
        object Stale : Decision()

        /** 已是新值，但还没稳定够久：继续等。 */
        object Settling : Decision()

        /** 是本次登录新产生、且已稳定：可以提交。 */
        object Ready : Decision()
    }

    /**
     * 判定一次。
     *
     * 返回 `(判定结果, 调用方应保存的状态)`；状态是 `(lastSig, sinceMs)`，
     * 即"上一次看到的指纹"与"它首次出现的时刻"。调用方每次都要把新状态存回去。
     *
     * @param baselineSig 本次登录**开始前**的指纹（清理后取的）。null = 起点没有会话 Cookie。
     * @param currentSig  当前指纹。null = 当前没有会话 Cookie。
     * @param lastSig     上一次判定时保存的指纹。
     * @param sinceMs     上一次判定时保存的"首次出现时刻"。
     * @param nowMs       当前时刻。
     * @param stableMs    需要连续稳定的时长。
     */
    fun decide(
        baselineSig: String?,
        currentSig: String?,
        lastSig: String?,
        sinceMs: Long,
        nowMs: Long,
        stableMs: Long,
    ): Pair<Decision, Pair<String?, Long>> {
        // 当前没有会话 Cookie：重置稳定期计时
        if (currentSig == null) return Decision.Waiting to (null to 0L)

        // 关卡①：与起点相同 —— 本次登录没有产生新的会话凭据。
        //
        // ⚠️ 这一步**必须排在关卡②之前**：旧值一动不动，若先判"稳定"就会放它过去
        //    （这正是修复前的缺陷）。
        if (currentSig == baselineSig) return Decision.Stale to (null to 0L)

        // 指纹变了：重新开始计时
        if (currentSig != lastSig) return Decision.Settling to (currentSig to nowMs)

        // 关卡②：连续稳定够久
        return if (nowMs - sinceMs >= stableMs) {
            Decision.Ready to (currentSig to sinceMs)
        } else {
            Decision.Settling to (currentSig to sinceMs)
        }
    }
}
