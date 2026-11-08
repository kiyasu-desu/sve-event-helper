package com.svetool.monitor

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.floatingactionbutton.ExtendedFloatingActionButton

/**
 * 主界面：一个 WebView 加载服务器上的 Web UI。
 *
 * 之所以不重写界面：现有 PWA 已经完整可用（账号、赛事列表、订阅、通知设置、
 * 远端执行位置……），原生壳只需要把它装进来，再额外提供「新增账号」这一个
 * 网页做不到的能力。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView

    private val loginLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK) {
            val msg = result.data?.getStringExtra(LoginActivity.EXTRA_MESSAGE).orEmpty()
            Toast.makeText(this, msg.ifEmpty { "账号已添加" }, Toast.LENGTH_LONG).show()
            // 账号变了，重新加载让界面拿到新列表
            web.reload()
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = FrameLayout(this)
        web = WebView(this)
        root.addView(
            web,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        )
        // 原生悬浮按钮已移除：入口改回**网页里账号区顶部的「新增账号」按钮**
        // （用户要求与浏览器端一致）。网页点它 → `sve://login` → 本 Activity 拦截
        // → 打开 LoginActivity。见下方 shouldOverrideUrlLoading。
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
                    }
                    return true   // 已处理，WebView 不要再去加载它
                }
                return false      // 其余一律在应用内打开
            }

            override fun onPageFinished(view: WebView, url: String) {
                // 页面每次加载完都补注入一次：这样刷新/跳转后标记仍在。
                // 两个用途：① 让网页知道自己在 App 内（决定「新增账号」走原生还是提示）
                //           ② 把版本号带上界面，解决"重建多次却分不清装了哪个版本"
                val js = "window.__SVE_APP__=true;" +
                        "window.__SVE_VER__={name:'${BuildConfig.VERSION_NAME}'," +
                        "code:${BuildConfig.VERSION_CODE}};"
                view.evaluateJavascript(js, null)
            }
        }

        // 系统返回键：网页里能后退就后退，否则退出 App
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) {
                    web.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })

        if (savedInstanceState == null) {
            web.loadUrl(Cfg.BASE_URL)
        } else {
            web.restoreState(savedInstanceState)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
    }

    override fun onDestroy() {
        // 干净的 WebView 生命周期，避免泄漏
        (web.parent as? ViewGroup)?.removeView(web)
        web.destroy()
        super.onDestroy()
    }
}
