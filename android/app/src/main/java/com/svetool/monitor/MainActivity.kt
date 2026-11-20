package com.svetool.monitor

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity

/**
 * 主界面：一个 WebView 加载用户配置的服务器 Web UI。
 *
 * 之所以不重写界面：现有 PWA 已经完整可用（账号、赛事列表、订阅、通知设置、
 * 远端执行位置……），原生壳只需要把它装进来，再额外提供「新增账号」与
 * 「服务器地址配置/切换」等网页做不到的能力。
 */
class MainActivity : AppCompatActivity() {

    companion object {
        const val ACTION_SWITCH_SERVER = "com.svetool.monitor.ACTION_SWITCH_SERVER"
        private const val STATE_BASE_URL = "state_base_url"
        private const val STATE_PENDING_HISTORY_CLEAR = "state_pending_history_clear"
    }

    private lateinit var web: WebView
    private lateinit var errorOverlay: View
    private var currentBaseUrl: String = ""
    private var pendingHistoryClear: Boolean = false

    private val loginLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK && ::web.isInitialized) {
            val msg = result.data?.getStringExtra(LoginActivity.EXTRA_MESSAGE).orEmpty()
            Toast.makeText(this, msg.ifEmpty { "账号已添加" }, Toast.LENGTH_LONG).show()
            // 账号变了，重新加载让界面拿到新列表
            web.reload()
        }
    }

    private val setupLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK && ::web.isInitialized) {
            val updated = ServerConfig.getBaseUrl(this)
            if (updated.isEmpty()) return@registerForActivityResult
            hideErrorOverlay()
            if (updated != currentBaseUrl) {
                currentBaseUrl = updated
                pendingHistoryClear = true
                web.stopLoading()
                web.loadUrl(updated)
            } else {
                web.reload()
            }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val initialUrl = ServerConfig.getBaseUrl(this)
        if (initialUrl.isEmpty()) {
            intent?.action = null
            startActivity(
                Intent(this, ServerSetupActivity::class.java)
                    .putExtra(ServerSetupActivity.EXTRA_ALLOW_CANCEL, false)
            )
            finish()
            return
        }
        currentBaseUrl = ServerConfig.restoreTrackedBaseUrl(
            savedInstanceState?.getString(STATE_BASE_URL),
            initialUrl
        )
        pendingHistoryClear = savedInstanceState?.getBoolean(STATE_PENDING_HISTORY_CLEAR, false) ?: false

        val root = FrameLayout(this)
        web = WebView(this)
        root.addView(
            web,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        )
        errorOverlay = buildErrorOverlay()
        root.addView(
            errorOverlay,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        )
        setContentView(root)

        web.setBackgroundColor(Color.parseColor("#0f1116"))
        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            loadWithOverviewMode = true
            useWideViewPort = true
            // Cloudflare Access 会设 Cookie；跨站场景下需要显式允许第三方 Cookie
            CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)
        }
        CookieManager.getInstance().setAcceptCookie(true)

        web.webChromeClient = WebChromeClient()
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView,
                request: WebResourceRequest
            ): Boolean {
                val u = request.url.toString()
                // 网页 → 原生通道：只认我们自己的固定 scheme。
                // 用 scheme 而不是 addJavascriptInterface，是因为前者不需要在
                // WebView 上暴露任何 Java 对象，没有注入面；白名单也天然成立。
                if (u.startsWith(Cfg.NATIVE_SCHEME)) {
                    when (u.removePrefix(Cfg.NATIVE_SCHEME).trimEnd('/')) {
                        "login" -> loginLauncher.launch(
                            Intent(this@MainActivity, LoginActivity::class.java)
                        )
                        "server" -> launchServerSetup(allowCancel = true)
                        "logout" -> performLogout()
                    }
                    return true   // 已处理，WebView 不要再去加载它
                }
                return false      // 其余一律在应用内打开
            }

            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                hideErrorOverlay()
            }

            override fun doUpdateVisitedHistory(view: WebView, url: String, isReload: Boolean) {
                if (pendingHistoryClear) {
                    pendingHistoryClear = false
                    view.clearHistory()
                }
            }

            override fun onReceivedError(
                view: WebView,
                request: WebResourceRequest,
                error: WebResourceError
            ) {
                // 仅拦截主框架底层网络/DNS/TLS 错误；绝不拦截 HTTP 401/403，
                // 以免干扰 Cloudflare Access 登录或重定向流程。
                if (request.isForMainFrame) {
                    showErrorOverlay()
                }
            }

            override fun onPageFinished(view: WebView, url: String) {
                // 页面每次加载完都补注入一次：这样刷新/跳转后标记仍在。
                // 两个用途：① 让网页知道自己在 App 内（决定「新增账号」与「更换服务器地址」展示）
                //           ② 把版本号带上界面，同时刷新 App 模式开关函数
                val js = "window.__SVE_APP__=true;" +
                    "window.__SVE_VER__={name:'${BuildConfig.VERSION_NAME}'," +
                    "code:${BuildConfig.VERSION_CODE}};" +
                    "if(typeof configureAppServerSwitch==='function')configureAppServerSwitch();"
                view.evaluateJavascript(js, null)
            }
        }

        // 系统返回键：网页里能后退就后退，否则退出 App
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (errorOverlay.visibility != View.VISIBLE && web.canGoBack()) {
                    web.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })

        if (savedInstanceState == null) {
            web.loadUrl(initialUrl)
        } else {
            web.restoreState(savedInstanceState)
        }

        if (intent?.action == ACTION_SWITCH_SERVER) {
            intent.action = null
            launchServerSetup(allowCancel = true)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (intent.action == ACTION_SWITCH_SERVER) {
            intent.action = null
            val configured = ServerConfig.getBaseUrl(this)
            if (configured.isEmpty()) {
                startActivity(
                    Intent(this, ServerSetupActivity::class.java)
                        .putExtra(ServerSetupActivity.EXTRA_ALLOW_CANCEL, false)
                )
                finish()
            } else {
                launchServerSetup(allowCancel = true)
            }
        }
    }

    private fun launchServerSetup(allowCancel: Boolean, returnResult: Boolean = true) {
        setupLauncher.launch(
            Intent(this, ServerSetupActivity::class.java)
                .putExtra(ServerSetupActivity.EXTRA_ALLOW_CANCEL, allowCancel)
                .putExtra(ServerSetupActivity.EXTRA_RETURN_RESULT, returnResult)
        )
    }

    private fun buildErrorOverlay(): View {
        val dp = { v: Int ->
            TypedValue.applyDimension(
                TypedValue.COMPLEX_UNIT_DIP,
                v.toFloat(),
                resources.displayMetrics
            ).toInt()
        }

        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#0f1116"))
            setPadding(dp(24), dp(24), dp(24), dp(24))
            visibility = View.GONE
            isClickable = true
        }

        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(22), dp(20), dp(22))
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#171a21"))
                cornerRadius = dp(12).toFloat()
                setStroke(dp(1), Color.parseColor("#2a2f3a"))
            }
        }

        val title = TextView(this).apply {
            text = getString(R.string.server_error_title)
            setTextColor(Color.parseColor("#dfe3ea"))
            textSize = 18f
            setPadding(0, 0, 0, dp(8))
        }
        card.addView(title)

        val detail = TextView(this).apply {
            text = getString(R.string.server_error_detail)
            setTextColor(Color.parseColor("#8b93a3"))
            textSize = 14f
            setPadding(0, 0, 0, dp(18))
        }
        card.addView(detail)

        val retryBtn = Button(this).apply {
            text = getString(R.string.server_error_retry)
            isAllCaps = false
            setTextColor(Color.parseColor("#06121f"))
            textSize = 15f
            setPadding(dp(16), dp(12), dp(16), dp(12))
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#3b82f6"))
                cornerRadius = dp(8).toFloat()
            }
            setOnClickListener {
                hideErrorOverlay()
                val target = ServerConfig.getBaseUrl(this@MainActivity)
                if (target.isEmpty()) {
                    launchServerSetup(allowCancel = false)
                } else {
                    currentBaseUrl = target
                    web.loadUrl(target)
                }
            }
        }
        card.addView(
            retryBtn,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        )

        val changeBtn = Button(this).apply {
            text = getString(R.string.server_error_change)
            isAllCaps = false
            setTextColor(Color.parseColor("#dfe3ea"))
            textSize = 14f
            setPadding(dp(16), dp(10), dp(16), dp(10))
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#1d2129"))
                cornerRadius = dp(8).toFloat()
                setStroke(dp(1), Color.parseColor("#2a2f3a"))
            }
            setOnClickListener { launchServerSetup(allowCancel = true) }
        }
        card.addView(
            changeBtn,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                topMargin = dp(10)
            }
        )

        container.addView(
            card,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        )
        return container
    }

    private fun showErrorOverlay() {
        if (::errorOverlay.isInitialized) {
            errorOverlay.visibility = View.VISIBLE
        }
    }

    private fun hideErrorOverlay() {
        if (::errorOverlay.isInitialized) {
            errorOverlay.visibility = View.GONE
        }
    }

    private fun performLogout() {
        hideErrorOverlay()
        if (::web.isInitialized) {
            web.stopLoading()
        }
        val cookies = CookieManager.getInstance()
        cookies.removeAllCookies {
            cookies.flush()
            runOnUiThread {
                if (isFinishing || isDestroyed || !::web.isInitialized) return@runOnUiThread
                pendingHistoryClear = true
                val target = ServerConfig.getBaseUrl(this@MainActivity).ifEmpty { currentBaseUrl }
                if (target.isNotEmpty()) {
                    currentBaseUrl = target
                    web.loadUrl(target)
                } else {
                    launchServerSetup(allowCancel = false)
                }
            }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_BASE_URL, currentBaseUrl)
        outState.putBoolean(STATE_PENDING_HISTORY_CLEAR, pendingHistoryClear)
        if (::web.isInitialized) {
            web.saveState(outState)
        }
    }

    override fun onDestroy() {
        if (::web.isInitialized) {
            // 干净的 WebView 生命周期，避免泄漏
            (web.parent as? ViewGroup)?.removeView(web)
            web.destroy()
        }
        super.onDestroy()
    }
}
