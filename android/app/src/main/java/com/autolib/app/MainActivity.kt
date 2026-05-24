package com.autolib.app

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.webkit.*
import android.widget.ProgressBar
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.isVisible
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.autolib.app.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var webView: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        webView = binding.webView

        // 记录系统栏高度，页面加载完后注入 CSS 变量
        var navInsetPx = 0
        var statusInsetPx = 0
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { _, insets ->
            navInsetPx = insets.getInsets(WindowInsetsCompat.Type.navigationBars()).bottom
            statusInsetPx = insets.getInsets(WindowInsetsCompat.Type.statusBars()).top
            insets
        }

        if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true)

        // Cookie 持久化——保证 Flask session 重启后还在
        CookieManager.getInstance().apply {
            setAcceptCookie(true)
            setAcceptThirdPartyCookies(webView, true)
        }

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            useWideViewPort = true
            loadWithOverviewMode = true
            builtInZoomControls = false
            displayZoomControls = false
            setSupportZoom(false)
            // 网络可用时遵守 HTTP 缓存头；离线时自动回落到缓存
            cacheMode = WebSettings.LOAD_DEFAULT
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            // 开启数据库缓存（兼容旧版 WebView）
            databaseEnabled = true
        }

        webView.webViewClient = object : WebViewClient() {
            private val serverHost = Uri.parse(BuildConfig.SERVER_URL).host ?: ""

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val uri = request.url
                // 同域链接留在 WebView 里
                if (uri.host == serverHost) return false
                // mailto: / 其它外部链接走系统
                runCatching { startActivity(Intent(Intent.ACTION_VIEW, uri)) }
                return true
            }

            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                binding.progressBar.isVisible = true
                binding.swipeRefresh.isRefreshing = false
            }

            override fun onPageFinished(view: WebView, url: String) {
                binding.progressBar.isVisible = false
                CookieManager.getInstance().flush()
                // 页面加载完后注入系统栏高度，确保 CSS 变量生效
                view.evaluateJavascript(
                    "document.documentElement.style.setProperty('--nav-inset','${navInsetPx}px');" +
                    "document.documentElement.style.setProperty('--status-inset','${statusInsetPx}px');",
                    null
                )
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                // 只对主框架（非子资源）的错误显示离线页
                if (request.isForMainFrame) {
                    view.loadUrl("file:///android_asset/offline.html")
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(msg: ConsoleMessage): Boolean {
                Log.d("AutoLib/JS", "${msg.message()} [${msg.sourceId()}:${msg.lineNumber()}]")
                return true
            }

            override fun onProgressChanged(view: WebView, newProgress: Int) {
                binding.progressBar.progress = newProgress
            }
        }

        binding.swipeRefresh.setOnRefreshListener {
            webView.reload()
        }

        if (savedInstanceState != null) {
            webView.restoreState(savedInstanceState)
        } else {
            webView.loadUrl(BuildConfig.SERVER_URL)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }
}
