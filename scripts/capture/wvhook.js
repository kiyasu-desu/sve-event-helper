/*
 * WebView 登录回调探针
 *
 * 目的：弄清 APP 的 WebView 登录是"如何把登录结果交回 React Native"的。
 * 这决定自建 WebView 登录（方案 3）是否可行：
 *   - 若回调是一个可识别的 URL / JS 消息 → 我们能在自己的 WebView 里复刻
 *   - 若是封闭的私有协议 / 强设备指纹 → 方案 3 不可行
 *
 * 挂钩点（都只在 URL / 短字符串上打日志，绝不打印 HTML 源码）：
 *   WebView.loadUrl / postUrl / evaluateJavascript
 *   WebViewClient.shouldOverrideUrlLoading / onPageStarted / onPageFinished
 *   addJavascriptInterface（桥）
 *   WebChromeClient.onConsoleMessage（登录页常用 console 传参）
 *   CookieManager（登录页设的 cookie）
 *
 * 注意：Frida 17 不再自动注入 Java bridge，且 payload 侧没有 require，
 * 所以这里显式 import，并由 build_payload.js 打包成 bundle 后再加载。
 */
import bridge from 'frida-java-bridge';

const Java = bridge.default || bridge;

var PREFIX = '[wvhook]';
var seenKeys = {};

function log(msg) {
    send({ kind: 'wvhook', msg: String(msg) });
}

function short(s, n) {
    // 不截断，只在超长时附上长度：回调 URL 里的 data 参数本身就是我们要分析的内容，
    // 截断会让它无法解析（曾经因此拿不到完整 payload）。
    return String(s);
}

// 只报告"像 URL 或像 token 回调"的字符串，避免被 HTML 淹没
function interesting(s) {
    if (typeof s !== 'string') { return false; }
    if (s.length > 4000) { return false; }        // HTML 源码，跳过
    if (s.indexOf('http') >= 0) { return true; }
    if (s.indexOf('token') >= 0) { return true; }
    if (s.indexOf('session') >= 0) { return true; }
    if (s.indexOf('login') >= 0) { return true; }
    if (s.indexOf('callback') >= 0) { return true; }
    if (s.indexOf('code=') >= 0) { return true; }
    if (s.indexOf('ntes') >= 0) { return true; }
    return false;
}

function once(key, fn) {
    if (seenKeys[key]) { return; }
    seenKeys[key] = true;
    fn();
}

function hookWebView() {
    var WebView = null;
    try { WebView = Java.use('android.webkit.WebView'); } catch (e) { log('WebView 类不可用: ' + e); return false; }

    // ---- 加载 URL ----
    var loadUrl = WebView.loadUrl.overload('java.lang.String');
    loadUrl.implementation = function (url) {
        if (interesting(url)) { log('WebView.loadUrl  -> ' + short(url)); }
        return loadUrl.call(this, url);
    };

    var loadUrl2 = WebView.loadUrl.overload('java.lang.String', 'java.util.Map');
    loadUrl2.implementation = function (url, headers) {
        if (interesting(url)) { log('WebView.loadUrl(+headers) -> ' + short(url)); }
        return loadUrl2.call(this, url, headers);
    };

    try {
        var postUrl = WebView.postUrl.overload('java.lang.String', '[B');
        postUrl.implementation = function (url, data) {
            if (interesting(url)) { log('WebView.postUrl -> ' + short(url)); }
            return postUrl.call(this, url, data);
        };
    } catch (e) { /* 某些版本签名不同，忽略 */ }

    // ---- 执行 JS（回调可能通过 evaluateJavascript 注入）----
    try {
        var evalJs = WebView.evaluateJavascript.overload('java.lang.String', 'android.webkit.ValueCallback');
        evalJs.implementation = function (script, cb) {
            if (interesting(script)) { log('WebView.evaluateJavascript -> ' + short(script, 300)); }
            return evalJs.call(this, script, cb);
        };
    } catch (e) { }

    // ---- JS 桥 ----
    try {
        var addIface = WebView.addJavascriptInterface.overload('java.lang.Object', 'java.lang.String');
        addIface.implementation = function (obj, name) {
            var cls = '?';
            try { cls = obj.getClass().getName(); } catch (e) { }
            log('addJavascriptInterface name=' + name + ' class=' + cls);
            return addIface.call(this, obj, name);
        };
    } catch (e) { }

    log('WebView 挂钩完成');
    return true;
}

function hookWebViewClient() {
    var WVC = null;
    try { WVC = Java.use('android.webkit.WebViewClient'); } catch (e) { log('WebViewClient 不可用: ' + e); return false; }

    var so = WVC.shouldOverrideUrlLoading.overload('android.webkit.WebView', 'java.lang.String');
    so.implementation = function (view, url) {
        if (interesting(url)) { log('shouldOverrideUrlLoading(String) -> ' + short(url)); }
        return so.call(this, view, url);
    };
    try {
        var so2 = WVC.shouldOverrideUrlLoading.overload('android.webkit.WebView', 'android.webkit.WebResourceRequest');
        so2.implementation = function (view, req) {
            var u = '';
            try { u = req.getUrl().toString(); } catch (e) { }
            if (interesting(u)) { log('shouldOverrideUrlLoading(Req) -> ' + short(u)); }
            return so2.call(this, view, req);
        };
    } catch (e) { }

    var ops = WVC.onPageStarted.overload('android.webkit.WebView', 'java.lang.String', 'android.graphics.Bitmap');
    ops.implementation = function (view, url, fav) {
        if (interesting(url)) { log('onPageStarted -> ' + short(url)); }
        return ops.call(this, view, url, fav);
    };

    var opf = WVC.onPageFinished.overload('android.webkit.WebView', 'java.lang.String');
    opf.implementation = function (view, url) {
        if (interesting(url)) { log('onPageFinished -> ' + short(url)); }
        return opf.call(this, view, url);
    };

    log('WebViewClient 挂钩完成');
    return true;
}

function hookChromeClient() {
    try {
        var WCC = Java.use('android.webkit.WebChromeClient');
        var ocm = WCC.onConsoleMessage.overload('android.webkit.ConsoleMessage');
        ocm.implementation = function (m) {
            var t = '';
            try { t = m.message(); } catch (e) { }
            if (interesting(t)) { log('console -> ' + short(t, 300)); }
            return ocm.call(this, m);
        };
        log('WebChromeClient 挂钩完成');
    } catch (e) { log('WebChromeClient 跳过: ' + e); }
}

function hookCookieManager() {
    try {
        var CM = Java.use('android.webkit.CookieManager');
        var sc = CM.setCookie.overload('java.lang.String', 'java.lang.String');
        sc.implementation = function (url, cookie) {
            log('CookieManager.setCookie url=' + short(url, 120) + ' cookie=' + short(cookie, 160));
            return sc.call(this, url, cookie);
        };
        log('CookieManager 挂钩完成');
    } catch (e) { log('CookieManager 跳过: ' + e); }
}

Java.perform(function () {
    log('=== WebView 探针启动 ===');
    hookWebView();
    hookWebViewClient();
    hookChromeClient();
    hookCookieManager();
    log('=== 全部挂钩完成，等待登录操作 ===');
});
