package io.github.justinguillaume.sourcenews

import android.graphics.Color
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Bundle
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.browser.customtabs.CustomTabColorSchemeParams
import androidx.browser.customtabs.CustomTabsIntent
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView
    private lateinit var refresh: SwipeRefreshLayout
    private var pageReady = false
    private var triedCache = false

    override fun onCreate(savedInstanceState: Bundle?) {
        val splash = installSplashScreen()
        super.onCreate(savedInstanceState)
        splash.setKeepOnScreenCondition { !pageReady }
        setContentView(R.layout.activity_main)

        refresh = findViewById(R.id.refresh)
        webView = findViewById(R.id.web)
        val settings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.loadsImagesAutomatically = true
        settings.useWideViewPort = true
        settings.loadWithOverviewMode = true
        settings.cacheMode = if (isOnline()) WebSettings.LOAD_DEFAULT else WebSettings.LOAD_CACHE_ELSE_NETWORK
        settings.mediaPlaybackRequiresUserGesture = true
        settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                return handleUrl(request.url)
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                refresh.isRefreshing = false
                if (url != null && !url.startsWith("data:")) pageReady = true
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (!request.isForMainFrame) return
                refresh.isRefreshing = false
                if (!triedCache && !isOnline()) {
                    triedCache = true
                    view.settings.cacheMode = WebSettings.LOAD_CACHE_ELSE_NETWORK
                    view.loadUrl(HOME)
                    return
                }
                pageReady = true
                view.loadDataWithBaseURL(HOME, OFFLINE_HTML, "text/html", "utf-8", null)
            }

            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, errorResponse: WebResourceResponse) {
                if (request.isForMainFrame) refresh.isRefreshing = false
            }
        }

        refresh.setColorSchemeColors(Color.parseColor("#2b6cb0"))
        refresh.setOnRefreshListener {
            triedCache = false
            webView.settings.cacheMode = if (isOnline()) WebSettings.LOAD_DEFAULT else WebSettings.LOAD_CACHE_ELSE_NETWORK
            webView.reload()
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })

        webView.postDelayed({ pageReady = true }, 8000)
        webView.loadUrl(HOME)
    }

    private fun handleUrl(uri: Uri): Boolean {
        if (uri.scheme == "sourcenews" && uri.host == "retry") {
            triedCache = false
            webView.settings.cacheMode = if (isOnline()) WebSettings.LOAD_DEFAULT else WebSettings.LOAD_CACHE_ELSE_NETWORK
            webView.loadUrl(HOME)
            return true
        }
        val scheme = uri.scheme?.lowercase()
        if (scheme != "http" && scheme != "https") return true
        if (isInApp(uri)) return false
        openExternal(uri)
        return true
    }

    private fun isInApp(uri: Uri): Boolean {
        val host = uri.host?.lowercase() ?: return false
        if (host != "justinguillaume-beep.github.io") return false
        val path = uri.path ?: "/"
        return path == "/" || path.startsWith("/SourceNews")
    }

    private fun openExternal(uri: Uri) {
        val colors = CustomTabColorSchemeParams.Builder()
            .setToolbarColor(Color.parseColor("#111111"))
            .build()
        val tabs = CustomTabsIntent.Builder()
            .setShowTitle(true)
            .setDefaultColorSchemeParams(colors)
            .build()
        try {
            tabs.launchUrl(this, uri)
        } catch (_: Exception) {
            Toast.makeText(this, "Couldn't open the link", Toast.LENGTH_SHORT).show()
        }
    }

    private fun isOnline(): Boolean {
        val cm = getSystemService(ConnectivityManager::class.java) ?: return false
        val network = cm.activeNetwork ?: return false
        val caps = cm.getNetworkCapabilities(network) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }

    companion object {
        private const val HOME = "https://justinguillaume-beep.github.io/SourceNews/"
        private val OFFLINE_HTML = """
            <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
            <style>body{margin:0;background:#111;color:#fff;font-family:sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;text-align:center;padding:24px}
            a{color:#111;background:#fff;padding:12px 16px;border-radius:8px;text-decoration:none;display:inline-block;margin-top:16px}</style></head>
            <body><div><h1>SourceNews</h1><p>You're offline. If this phone has opened the app before, pull to refresh to load the last saved copy.</p>
            <a href="sourcenews://retry">Try again</a></div></body></html>
        """.trimIndent()
    }
}
