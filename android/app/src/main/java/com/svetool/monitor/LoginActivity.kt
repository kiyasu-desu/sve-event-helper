package com.svetool.monitor

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONObject
import java.net.URLEncoder

/**
 * 「新增账号」——这是网页（PWA）**做不到**的那一件事。
 *
 * 为什么只有原生能做：
 *   换取 SVE 会话需要网易的会话 Cookie（NTES_YD_SESS 等），而它们是
 *   **HttpOnly** 的 —— 网页里的 JS 读不到；跨域也拿不到别的域的 Cookie。
 *   只有原生 WebView 的 CookieManager 能在 App 内读到它们。
 *
 * 流程：
 *   1. 本 Activity 的 WebView 打开**官方登录页**，用户正常登录
 *      （手机号+验证码 / 密码 / 扫码，取决于页面提供什么）
 *   2. 轮询 CookieManager，直到出现会话 Cookie
 *   3. 把 WebView **切到我们自己的域名**（Access 的 Cookie 是 App 级共享的，
 *      所以这时已通过 Access 认证）
 *   4. 在**同源**下用 fetch 把 Cookie POST 给 /api/login/cookie —— 交换会话、
 *      写入账号、推送到服务器代理，全部在服务器完成
 *
 * 关键点：**凭据不落在 App 里**。App 只是搬运一次 Cookie，不保存 sessionid，
 * 也不内置 appKey。
 */
class LoginActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_MESSAGE = "message"
        private const val TAG = "SveLogin"
        private const val EXCHANGE_TIMEOUT_MS = 60_000L
        private const val EXCHANGE_POLL_MS = 500L
    }

    private enum class Phase { LOGIN, EXCHANGE, DONE }

    private lateinit var web: WebView
    private lateinit var status: TextView

    private var phase = Phase.LOGIN
    private var pendingCookie: String = ""
    /**
     * 本次登录**开始前**的会话 Cookie 指纹（清理后取的）。
     *
     * 提交的必要条件之一是"当前指纹 ≠ 它" —— 用来挡住**上次遗留的旧会话**。
     * 见 LoginGate 的说明。
     */
    private var baselineSig: String? = null
    /** 上一次看到的会话 Cookie 指纹 + 它首次出现的时间，用于「稳定期」判定。 */
    private var lastSessionSig: String? = null
    private var sessionSigSince: Long = 0L
    private val handler = Handler(Looper.getMainLooper())
    private val startedAt = System.currentTimeMillis()

    private val pollLogin = object : Runnable {
        override fun run() {
            if (phase != Phase.LOGIN) return
            if (System.currentTimeMillis() - startedAt > Cfg.LOGIN_TIMEOUT_MS) {
                fail("登录超时，请重试")
                return
            }
            val cookie = readSessionCookie()
            // 判定交给 LoginGate（纯函数，有单元测试）：
            //   ① 必须与本次登录的起点不同（挡陈旧 Cookie）
            //   ② 连续稳定 SESSION_STABLE_MS（挡"值还在飞"）
            val (decision, next) = LoginGate.decide(
                baselineSig = baselineSig,
                currentSig = sessionSignature(cookie),
                lastSig = lastSessionSig,
                sinceMs = sessionSigSince,
                nowMs = System.currentTimeMillis(),
                stableMs = Cfg.SESSION_STABLE_MS,
            )
            lastSessionSig = next.first
            sessionSigSince = next.second

            when (decision) {
                is LoginGate.Decision.Waiting ->
                    status.text = getString(R.string.login_hint)

                is LoginGate.Decision.Stale ->
                    // 读到的是上次遗留的旧会话 —— **绝不提交**。
                    // 这正是真机上反复失败的那条路：用户还没登录，稳定期却已通过。
                    status.text = getString(R.string.login_waiting_new)

                is LoginGate.Decision.Settling ->
                    status.text = getString(R.string.login_confirming)

                is LoginGate.Decision.Ready -> {
                    Log.i(TAG, "会话 Cookie 是新值且已稳定 ${Cfg.SESSION_STABLE_MS}ms，开始换取会话")
                    if (cookie != null) startExchange(cookie)
                    return
                }
            }
            handler.postDelayed(this, Cfg.POLL_INTERVAL_MS)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        status = TextView(this).apply {
            setPadding(28, 22, 28, 22)
            setTextColor(Color.parseColor("#e5e7eb"))
            setBackgroundColor(Color.parseColor("#1f2937"))
            textSize = 13f
            text = getString(R.string.login_hint)
        }
        root.addView(status, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))

        web = WebView(this)
        root.addView(web, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))

        setContentView(root)

        web.setBackgroundColor(Color.parseColor("#ffffff"))
        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            useWideViewPort = true
            loadWithOverviewMode = true
            // 登录页是移动端 SPA，用移动 UA 才走对分支
            userAgentString = userAgentString
                .replace("; wv", "")
            CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)
        }
        CookieManager.getInstance().setAcceptCookie(true)

        web.webChromeClient = WebChromeClient()
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                v: WebView, request: WebResourceRequest
            ): Boolean = false

            override fun onPageFinished(v: WebView, url: String) {
                // 交换阶段：已切到自家域名，同源 fetch 才带得上 Access Cookie
                if (phase == Phase.EXCHANGE && url.startsWith(Cfg.BASE_URL)) {
                    postCookie(pendingCookie)
                }
            }
        }

        // ── 关键顺序：先清理、再记起点、最后才打开登录页 ──
        //
        // 为什么必须先清理：CookieManager 是 **App 级且跨启动持久**的。上次登录
        // （或失败的尝试）留下的 NTES_YD_SESS 会一直躺在 cookie 库里。不清理的话，
        // 用户**还没开始登录**就能读到它 —— 实测现象是：登录页还是空表单
        // （手机号都没填），App 就已经显示"已检测到登录凭据"，随后必然换不出
        // session_id 而失败。这就是真机上一直失败的那条路。
        clearNetEaseCookies()

        // 起点快照。**在清理之后取**：即使清理因 path 不匹配而没有完全生效，
        // 起点也已包含那些残留值，后面的"必须与起点不同"依然成立 —— 这是兜底。
        baselineSig = sessionSignature(readSessionCookie())
        Log.i(TAG, if (baselineSig == null)
            "登录起点：无会话 Cookie"
        else
            "登录起点：已存在会话 Cookie（陈旧），将要求出现新值")

        web.loadUrl(buildLoginUrl())
        handler.postDelayed(pollLogin, Cfg.POLL_INTERVAL_MS)
    }

    /**
     * 清掉**网易域**的 Cookie —— 只清它，**绝不碰自家域**。
     *
     * 为什么不能用 `removeAllCookies()`：那会把我们自家域（[Cfg.BASE_URL]）的
     * Cookie 一起清掉，其中包括 **Cloudflare Access 的 `CF_Authorization` JWT**
     * —— 主界面会掉登录，用户得重新过一遍 Access。所以只能按 URL 逐个把
     * Cookie 置为过期（CookieManager 没有"按域删除"的 API）。
     */
    private fun clearNetEaseCookies() {
        val cm = CookieManager.getInstance()
        var expired = 0
        for (u in Cfg.COOKIE_CLEAR_URLS) {
            val raw = cm.getCookie(u) ?: continue
            for (part in raw.split(";")) {
                val name = part.trim().substringBefore("=").trim()
                if (name.isEmpty()) continue
                // path 是 Cookie 身份的一部分：在 path=/ 上过期删不掉 path=/ma68 上的
                // 同名 Cookie，所以两个都过一遍。
                for (p in Cfg.COOKIE_CLEAR_PATHS) {
                    cm.setCookie(u, "$name=; Max-Age=0; path=$p")
                }
                expired++
            }
        }
        cm.flush()
        Log.i(TAG, "已清理网易域 Cookie：$expired 个（未触碰自家域）")
    }

    // ------------------------------------------------------------------ 登录页 URL
    /**
     * 与电脑端 weblogin.build_login_url() 保持一致的构造方式。
     * 登录页拿到这些参数后会把登录结果 POST 给 SVE 的 /urs/login；
     * 我们不依赖那个回调——只要 Cookie 出现，就自己完成交换。
     */
    private fun buildLoginUrl(): String {
        val data = JSONObject()
            .put("app_ver", Cfg.APP_VER)
            .put("device_height", 800)
            .put("device_width", 450)
            .put("model_name", Cfg.MODEL_NAME)
            .put("network_state", "wifi")
            .put("os_version", "android " + Build.VERSION.RELEASE)
            .put("platform", "android")
            .put("udid", Cfg.LOGIN_UDID)

        val q = listOf(
            "url" to "https://api-sve.nie.netease.com/sve/api/v1/urs/login",
            "proxy" to "",
            "method" to "post",
            "data" to data.toString(),
            "useNative" to "true",
            "project" to Cfg.PROJECT
        ).joinToString("&") { (k, v) ->
            URLEncoder.encode(k, "UTF-8") + "=" + URLEncoder.encode(v, "UTF-8")
        }
        return Cfg.LOGIN_PAGE + "?" + q
    }

    // ------------------------------------------------------------------ Cookie 读取
    /**
     * 在若干个域下取 Cookie 并合并；只要含会话 Cookie 名就返回整串。
     * 返回 null 表示"还没登录成功"。
     */
    /**
     * 只提取**会话 Cookie 的值**做指纹（返回 null 表示"没有会话 Cookie"）。
     *
     * 不整串比对的原因：登录页会持续写埋点/风控类 Cookie，
     * 整串每次都变，稳定期就永远不会到期。
     */
    private fun sessionSignature(cookie: String?): String? {
        if (cookie.isNullOrEmpty()) return null
        val wanted = cookie.split(";")
            .map { it.trim() }
            .filter { seg -> Cfg.SESSION_COOKIE_NAMES.any { seg.startsWith("$it=") } }
            .sorted()
        return if (wanted.isEmpty()) null else wanted.joinToString(";")
    }

    private fun readSessionCookie(): String? {
        val cm = CookieManager.getInstance()
        val all = LinkedHashMap<String, String>()
        for (u in Cfg.COOKIE_URLS) {
            val raw = cm.getCookie(u) ?: continue
            for (part in raw.split(";")) {
                val t = part.trim()
                if (t.isEmpty() || !t.contains("=")) continue
                val k = t.substringBefore("=").trim()
                val v = t.substringAfter("=").trim()
                if (k.isNotEmpty() && v.isNotEmpty()) all[k] = v
            }
        }
        if (all.isEmpty()) return null
        val hasSession = Cfg.SESSION_COOKIE_NAMES.any { !all[it].isNullOrEmpty() }
        if (!hasSession) return null
        return all.entries.joinToString("; ") { "${it.key}=${it.value}" }
    }

    // ------------------------------------------------------------------ 交换
    private fun startExchange(cookie: String) {
        phase = Phase.EXCHANGE
        pendingCookie = cookie
        status.text = getString(R.string.login_exchanging)
        // 切到自家域名：Access 的 Cookie 是 App 级共享的，此处已通过认证；
        // 在同源下 fetch 才不会被 CORS/SameSite 拦住。
        web.loadUrl(Cfg.BASE_URL)
    }

    /**
     * 用**同源 fetch** 把 Cookie 交给服务器。
     *
     * 为什么不在原生代码里直接发 HTTP：`/api/login/cookie` 受 Cloudflare Access
     * 保护，Access 的 JWT 在 WebView 的 Cookie 里；原生 HTTP 客户端拿不到它。
     * 走 WebView 的 fetch 就自然带上了。
     */
    private fun postCookie(cookie: String) {
        val quoted = JSONObject.quote(cookie)
        val js = """
            (function(){
              window.__sveResult = '';
              fetch('/api/login/cookie', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                credentials: 'same-origin',
                body: JSON.stringify({cookies: $quoted})
              }).then(function(r){ return r.json(); })
                .then(function(d){ window.__sveResult = JSON.stringify(d); })
                .catch(function(e){ window.__sveResult = JSON.stringify({ok:false,error:'网络错误: '+e}); });
              return true;
            })();
        """.trimIndent()
        web.evaluateJavascript(js, null)
        pollExchange(System.currentTimeMillis())
    }

    private fun pollExchange(t0: Long) {
        if (phase != Phase.EXCHANGE) return
        if (System.currentTimeMillis() - t0 > EXCHANGE_TIMEOUT_MS) {
            fail("换取会话超时")
            return
        }
        web.evaluateJavascript("window.__sveResult || ''") { raw ->
            val s = raw?.trim()?.removeSurrounding("\"")?.replace("\\\"", "\"")
            if (!s.isNullOrEmpty() && s.startsWith("{")) {
                try {
                    val d = JSONObject(s)
                    if (d.optBoolean("ok")) {
                        ok(d.optString("nickname").ifEmpty { d.optString("name") })
                    } else {
                        fail(d.optString("error").ifEmpty { "未知错误" })
                    }
                } catch (e: Exception) {
                    fail("响应解析失败: ${e.message}")
                }
                return@evaluateJavascript
            }
            handler.postDelayed({ pollExchange(t0) }, EXCHANGE_POLL_MS)
        }
    }

    private fun ok(name: String) {
        phase = Phase.DONE
        handler.removeCallbacksAndMessages(null)
        status.text = getString(R.string.login_ok, name)
        Log.i(TAG, "新增账号成功: $name")
        setResult(RESULT_OK, Intent().putExtra(EXTRA_MESSAGE, "已添加账号：$name"))
        handler.postDelayed({ finish() }, 1200)
    }

    private fun fail(msg: String) {
        phase = Phase.DONE
        handler.removeCallbacksAndMessages(null)
        status.text = getString(R.string.login_failed, msg)
        Log.w(TAG, "新增账号失败: $msg")
        setResult(RESULT_CANCELED, Intent().putExtra(EXTRA_MESSAGE, msg))
        // 失败时留在页面上，让用户能重试；不自动关闭
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        (web.parent as? ViewGroup)?.removeView(web)
        web.destroy()
        super.onDestroy()
    }
}
