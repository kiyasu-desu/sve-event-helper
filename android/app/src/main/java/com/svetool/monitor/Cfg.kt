package com.svetool.monitor

/**
 * 全局配置。
 *
 * 设计取舍：**App 不内置 appKey、不内置任何凭据**。
 * 它只是服务器 Web UI 的外壳，所有签名与交换都在服务器完成，
 * 因此这里只有地址常量。
 */
object Cfg {

    /** 服务器 Web UI（经 Cloudflare Tunnel + Access）。 */
    const val BASE_URL = BuildConfig.SVE_BASE_URL

    /** 官方登录页（与电脑端 weblogin.py 用的是同一个地址）。 */
    const val LOGIN_PAGE = "https://login-betasystem.nie.netease.com/ma68/#/pages/index/index"

    /**
     * 网页 → 原生的自定义 URL scheme。
     *
     * 网页里「新增账号」按钮执行 `location.href = "sve://login"`，
     * 由 MainActivity 的 WebViewClient 拦截并打开 LoginActivity。
     *
     * 为什么不用 `addJavascriptInterface`：那个方案要在 WebView 上暴露一个
     * Java 对象给页面里的任意脚本（含第三方脚本）调用，注入面更大；而 URL
     * scheme 的拦截点在我们自己的 WebViewClient 里，只认固定字符串，天然白名单。
     */
    const val NATIVE_SCHEME = "sve://"

    const val PROJECT = "yingzhishi"

    /**
     * 读取 Cookie 时覆盖的域。
     *
     * 实测「手机号+验证码」登录后，会话 Cookie 出现在 login-betasystem 与
     * .nie.netease.com 下；这里都取一遍再合并，避免漏掉。
     */
    val COOKIE_URLS = listOf(
        "https://login-betasystem.nie.netease.com",
        "https://nie.netease.com",
        "https://api-sve.nie.netease.com",
        "https://www.163.com",
    )

    /**
     * 清理网易域 Cookie 时使用的 URL。
     *
     * ⚠️ **绝不能包含 [BASE_URL]** —— 我们自己的域上有 Cloudflare Access 的
     * `CF_Authorization` JWT，把它清掉会让主界面掉登录，用户得重新过一遍 Access。
     *
     * 为什么需要清理：`CookieManager` 是 **App 级且跨启动持久**的。上一次登录
     * （或失败的尝试）留下的 `NTES_YD_SESS` 会一直躺在 cookie 库里，于是用户
     * **还没开始登录**就能读到它 —— 见 LoginActivity.clearNetEaseCookies() 的说明。
     */
    val COOKIE_CLEAR_URLS = listOf(
        // 带 /ma68 路径，便于按 path 过期登录页写在子路径上的 Cookie
        "https://login-betasystem.nie.netease.com/ma68/",
        "https://login-betasystem.nie.netease.com/",
        "https://nie.netease.com/",
        "https://api-sve.nie.netease.com/",
        "https://www.163.com/",
    )

    /**
     * 清理时逐个尝试过期的 path。
     *
     * path 是 Cookie 身份的一部分：在 `path=/` 上把同名 Cookie 置为过期，
     * **并不会**删掉另一个 path（如 `/ma68`）上的同名 Cookie，所以两个都过一遍。
     */
    val COOKIE_CLEAR_PATHS = listOf("/", "/ma68")

    /**
     * 可用于换取 SVE 会话的 Cookie 名。
     *
     * 注意不能只认 NTES_SESS —— 手机号+验证码这条路出来的是 **NTES_YD_SESS**。
     * 这与电脑端 weblogin.py 的 SESSION_COOKIE_NAMES 保持一致。
     */
    val SESSION_COOKIE_NAMES = listOf("NTES_SESS", "NTES_YD_SESS")

    /**
     * 会话 Cookie 需要**连续稳定**这么久，才认为登录真的完成、可以提交。
     *
     * 为什么必须这样：轮询里原本"一看到会话 Cookie 就提交"，但登录是分步的
     * （输手机号 → 发码 → 输码 → 可能的滑块），会话 Cookie 可能在**中间步骤**
     * 就被下发。此时提交的是一个"还在飞"的值——服务器会放行，但 /urs/login
     * 换不出 session_id，用户看到的就是"弹回主界面并报错：响应中没有 session_id"。
     *
     * 实测踩过：同一套代码，01:52 成功、18:50 连续 3 次失败，差别只在抓取时机。
     * 用一个"稳定期"兜底，不必去猜登录流程到底在哪一步才真正完成。
     */
    const val SESSION_STABLE_MS = 3000L

    // ⚠️ 这里**刻意不含 l_yd_sign**。
    //    它是辅助 Cookie，出现得比真正的会话 Cookie 早；用它判定"登录完成"
    //    会导致过早提交——服务器校验能通过，但 /urs/login 换不出 session_id，
    //    用户看到的就是"弹回主界面并报错"。实测踩过：
    //    同一套代码，01:52 成功、18:50 连续 3 次失败，差别只在抓取时机。

    /** 登录页 URL 上携带的设备信息（与电脑端保持一致即可，签名不依赖它）。 */
    const val APP_VER = "1.2.0"
    const val MODEL_NAME = "YOK-AN10"

    /**
     * 登录 URL 里的 udid 取值。
     *
     * 刻意用一个**固定值**而不用 Settings.Secure.ANDROID_ID：
     *   * 它只是作为参数传给登录页，服务端并不据此校验设备
     *   * 用真实设备标识会把 App 安装与设备绑定，带来隐私成本却无收益
     * 服务端最终写进账号的 deviceid 由服务端自己决定（App 不传它）。
     */
    const val LOGIN_UDID = "5f3a9c1d7e2b4806"

    /** 轮询登录完成状态的间隔（毫秒）。 */
    const val POLL_INTERVAL_MS = 1500L

    /** 登录超时（毫秒）。 */
    const val LOGIN_TIMEOUT_MS = 10 * 60 * 1000L
}
