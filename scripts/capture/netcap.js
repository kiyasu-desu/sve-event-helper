/* Frida payload for com.shadowversecard — in-app network capture (no proxy).
 *
 * Why this approach instead of mitmproxy:
 *   The proxy solution broke whenever the app restarted without hooks (every
 *   HTTPS request then failed certificate validation, and the app hung), and it
 *   dragged system traffic such as NTP through the proxy. Hooking the app's own
 *   network stack needs no proxy, no certificate trust changes, and leaves the
 *   app on a direct connection.
 *
 * What it does:
 *   1. hooks OkHttp's interceptor chain and logs request/response bodies;
 *   2. as a fallback, hooks HttpURLConnection and Android's SSLSocket, where the
 *      encrypted request/response is visible in the clear (TLS lives elsewhere
 *      in Conscrypt, so these bytes are still plaintext HTTP);
 *   3. tags every record so the analysis step can tell which hook produced it.
 *
 * Records are sent to the host as JSON via send(), and the host writes them to
 * capture/net_capture.jsonl. Nothing is written on the device.
 */
import bridge from 'frida-java-bridge';

const Java = bridge.default || bridge;

const TAG = 'netcap';
const TARGET = 'com.shadowversecard';
const MAX_BODY = 4000;

/* Host names worth recording; set to null to record everything. */
const HOST_FILTER = [
    'nie.netease.com'
];

/* Classes that produce nothing but noise. */
const IGNORE_URL = [
    '/manifest', 'manifest_asset', 'update_check', '.png', '.jpg', '.jpeg',
    '.webp', '.mp3', '.wav', '.ttf', '.otf', '.mp4'
];

function log(msg) {
    try { send(TAG + ': ' + msg); } catch (e) { /* ignore */ }
}

function emit(record) {
    try { send(JSON.stringify(record)); } catch (e) { /* ignore */ }
}

function isInteresting(url) {
    if (!url) { return false; }
    if (HOST_FILTER && !HOST_FILTER.some(h => url.indexOf(h) !== -1)) { return false; }
    return !IGNORE_URL.some(s => url.indexOf(s) !== -1);
}

function truncate(text, limit) {
    const s = String(text === null || text === undefined ? '' : text);
    if (s.length <= limit) { return s; }
    return s.substring(0, limit) + '...<TRUNCATED ' + (s.length - limit) + ' chars>';
}

function javaString(value) {
    try { return value === null ? null : String(value); } catch (e) { return null; }
}

/* ---------- 1. OkHttp interceptor chain ---------- */
function hookOkHttp() {
    let names = [];
    let allOkhttp = [];
    try {
        allOkhttp = Java.enumerateLoadedClassesSync().filter(function (n) {
            return n.indexOf('okhttp') !== -1;
        });
        names = allOkhttp.filter(function (n) {
            return /InterceptorChain/i.test(n);
        });
    } catch (e) { /* ignore */ }
    if (!hookOkHttp.reported) {
        hookOkHttp.reported = true;
        log('okhttp classes loaded: ' + allOkhttp.length);
        allOkhttp.slice(0, 25).forEach(function (n) { log('  okhttp: ' + n); });
        log('interceptor chain candidates: ' + (names.join(', ') || 'none'));
    }
    if (names.length === 0) { return 0; }

    let hooked = 0;
    names.forEach(function (name) {
        try {
            const C = Java.use(name);
            C.class.getDeclaredMethods().forEach(function (m) {
                if (m.getName() !== 'proceed') { return; }
                const params = m.getParameterTypes();
                if (params.length !== 1 || String(params[0].getName()) !== 'okhttp3.Request') { return; }
                try {
                    C.proceed.overload('okhttp3.Request').implementation = function (request) {
                        let url = null, method = null, reqBody = null, headers = null, rawUrl = null, cookie = null;
                        try {
                            url = javaString(request.url().toString());
                            rawUrl = url;
                            try {
                                const q = request.url().encodedQuery();
                                if (q !== null && q !== undefined) { rawUrl = url + '?' + q; }
                            } catch (e) { /* ignore */ }
                            method = javaString(request.method());
                            headers = truncate(javaString(request.headers().toString()), 1200);
                            try { cookie = javaString(request.headers().get('Cookie')); } catch (e) { /* ignore */ }
                        } catch (e) { /* ignore */ }

                        let response = null;
                        try {
                            response = this.proceed(request);
                        } catch (e) {
                            emit({ tag: 'okhttp', url: rawUrl, method: method, req_body: globalThis.__netcapLastBody,
                                   cookie: cookie, error: String(e) });
                            throw e;
                        }

                        try {
                            if (isInteresting(url)) {
                                let status = null, respBody = null;
                                try { status = response.code(); } catch (e) { /* ignore */ }
                                try {
                                    // peekBody buffers a clone, so the app's own read is unaffected
                                    const peeked = response.peekBody(1024 * 1024);
                                    respBody = truncate(javaString(peeked.string()), MAX_BODY);
                                } catch (e) {
                                    respBody = '<peekBody failed: ' + e + '>';
                                }
                                emit({
                                    tag: 'okhttp',
                                    url: url,
                                    raw_url: rawUrl,
                                    method: method,
                                    status: status,
                                    req_headers: headers,
                                    req_body: globalThis.__netcapLastBody,
                                    cookie: cookie,
                                    resp_body: respBody
                                });
                                globalThis.__netcapLastBody = null;
                            }
                        } catch (e) {
                            log('record failed: ' + e);
                        }
                        return response;
                    };
                    hooked += 1;
                } catch (e) { /* overload not hookable */ }
            });
        } catch (e) { /* class not usable */ }
    });
    return hooked;
}

/* ---------- 2. HttpURLConnection ---------- */
function hookUrlConnection() {
    const targets = [
        'com.android.okhttp.internal.huc.HttpURLConnectionImpl',
        'com.android.okhttp.internal.huc.HttpsURLConnectionImpl',
        'com.android.okhttp.internal.huc.JavaApiConverter'
    ];
    let hooked = 0;
    targets.forEach(function (name) {
        try {
            const C = Java.use(name);
            C.class.getDeclaredMethods().forEach(function (m) {
                if (m.getName() !== 'getInputStream') { return; }
                try {
                    C.getInputStream.overload().implementation = function () {
                        const stream = this.getInputStream();
                        try {
                            const url = javaString(this.getURL().toString());
                            if (isInteresting(url)) {
                                emit({ tag: 'urlconnection', url: url, note: 'getInputStream called' });
                            }
                        } catch (e) { /* ignore */ }
                        return stream;
                    };
                    hooked += 1;
                } catch (e) { /* ignore */ }
            });
        } catch (e) { /* class absent */ }
    });
    return hooked;
}

/* ---------- 3. plaintext HTTP inside the TLS socket ---------- */
function hookSslSockets() {
    const targets = [
        'com.android.org.conscrypt.ConscryptEngineSocket',
        'com.android.org.conscrypt.ConscryptFileDescriptorSocket',
        'com.android.org.conscrypt.OpenSSLSocketImpl'
    ];
    let hooked = 0;
    targets.forEach(function (name) {
        try {
            const C = Java.use(name);
            try {
                const out = Java.use('java.io.OutputStream');
                C.getOutputStream.overload().implementation = function () {
                    const stream = this.getOutputStream();
                    log('ssl output stream via ' + name);
                    return stream;
                };
                hooked += 1;
            } catch (e) { /* ignore */ }
        } catch (e) { /* class absent */ }
    });
    return hooked;
}

/* ---------- 1b. request bodies ----------
 * OkHttp writes a JSON body through one of the BufferedSink writers. Which one
 * varies by OkHttp/okio build, so hook all three rather than guessing:
 *   writeUtf8(String)      - RequestBody.create(JSON, string)
 *   writeString(String,..) - same, with an explicit charset
 *   write(ByteString)      - pre-encoded bodies
 * Every JSON-looking string is also logged, which makes it possible to tell
 * "hook never fired" apart from "hook fired with a different shape".
 */
function looksLikeJson(text) {
    if (!text || text.length < 2) { return false; }
    const c = text.charAt(0);
    return c === '{' || c === '[';
}

function utf8Decode(bytes) {
    let out = '';
    for (let i = 0; i < bytes.length; i++) {
        const b = bytes[i];
        if (b < 0x80) {
            out += String.fromCharCode(b);
        } else if (b >= 0xC0 && b < 0xE0 && i + 1 < bytes.length) {
            out += String.fromCharCode(((b & 0x1F) << 6) | (bytes[i + 1] & 0x3F));
            i += 1;
        } else if (b >= 0xE0 && b < 0xF0 && i + 2 < bytes.length) {
            out += String.fromCharCode(((b & 0x0F) << 12) | ((bytes[i + 1] & 0x3F) << 6) | (bytes[i + 2] & 0x3F));
            i += 2;
        } else if (b >= 0xF0 && i + 3 < bytes.length) {
            const cp = ((b & 0x07) << 18) | ((bytes[i + 1] & 0x3F) << 12) |
                       ((bytes[i + 2] & 0x3F) << 6) | (bytes[i + 3] & 0x3F);
            out += String.fromCodePoint(cp);
            i += 3;
        }
    }
    return out;
}

function rememberBody(text, via) {
    try {
        if (!looksLikeJson(text)) { return; }
        if (globalThis.__netcapLastBody) { return; }
        globalThis.__netcapLastBody = text;
        log('body captured via ' + via + ' (' + text.length + ' chars): ' + text.substring(0, 160));
    } catch (e) { /* ignore */ }
}

function hookRequestBody() {
    let hooked = 0;

    // Report what is actually loaded: guessing at okio classes wasted two rounds.
    try {
        const all = Java.enumerateLoadedClassesSync();
        const okio = all.filter(function (n) { return n.indexOf('okio') !== -1; });
        const reqb = all.filter(function (n) { return /RequestBody/i.test(n); });
        log('okio classes: ' + okio.length + ' | ' + okio.slice(0, 30).join(', '));
        log('RequestBody classes: ' + reqb.length + ' | ' + reqb.slice(0, 20).join(', '));
    } catch (e) { /* ignore */ }

    // Hook every loaded okio writer implementation, not just the abstract
    // interface: writes are dispatched on the concrete class, so hooking
    // `okio.BufferedSink` alone never fires.
    let okioNames = [];
    try {
        okioNames = Java.enumerateLoadedClassesSync().filter(function (n) {
            return n.indexOf('okio') !== -1 && !n.startsWith('[');
        });
    } catch (e) { /* ignore */ }

    okioNames.forEach(function (name) {
        let C;
        try { C = Java.use(name); } catch (e) { return; }

        try {
            C.writeUtf8.overload('java.lang.String').implementation = function (s) {
                rememberBody(String(s), name + '.writeUtf8');
                return this.writeUtf8(s);
            };
            hooked += 1;
        } catch (e) { /* this class has no such overload */ }

        try {
            C.writeString.overload('java.lang.String', 'java.nio.charset.Charset').implementation = function (s, cs) {
                rememberBody(String(s), name + '.writeString');
                return this.writeString(s, cs);
            };
            hooked += 1;
        } catch (e) { /* ignore */ }

        try {
            C.write.overload('okio.ByteString').implementation = function (bs) {
                try {
                    if (bs.size() < 8192) {
                        rememberBody(utf8Decode(new Uint8Array(bs.toByteArray())), name + '.write(ByteString)');
                    }
                } catch (e) { /* ignore */ }
                return this.write(bs);
            };
            hooked += 1;
        } catch (e) { /* ignore */ }

        try {
            C.write.overload('[B', 'int', 'int').implementation = function (arr, off, len) {
                try {
                    if (len > 0 && len < 8192) {
                        const slice = new Uint8Array(len);
                        for (let i = 0; i < len; i++) { slice[i] = arr[off + i]; }
                        rememberBody(utf8Decode(slice), name + '.write([B,int,int)');
                    }
                } catch (e) { /* ignore */ }
                return this.write(arr, off, len);
            };
            hooked += 1;
        } catch (e) { /* ignore */ }
    });

    return hooked;
}

/* ---------- 4. expose the device unique id ----------
 * The sign algorithm needs the app's own `udid` (see generateSign in the JS
 * bundle: it reads getUniqueId() and stores it under AtomKey.SysInfoRn).
 * rpc.exports lets the host ask for it directly instead of guessing.
 */
let cachedUdid = null;

rpc.exports = {
    udid: function () {
        let value = null;
        try {
            Java.perform(function () {
                if (cachedUdid) { value = cachedUdid; return; }
                const DeviceInfo = Java.use('com.learnium.RNDeviceInfo.RNDeviceInfo');
                try {
                    const module = DeviceInfo.$new();
                    value = String(module.getUniqueId());
                    cachedUdid = value;
                } catch (e) {
                    value = 'ERR:' + e;
                }
            });
        } catch (e) {
            value = 'ERR:' + e;
        }
        return value;
    },
    ping: function () { return 'pong'; },
    /* 读取 app 的 AsyncStorage（会话恢复的备选路径）。
     * 主路径是直接读设备的 RKStorage，这里作为 Frida 可用时的替代方案。 */
    read_storage: function (key) {
        let out = null;
        try {
            Java.perform(function () {
                const ReactDatabaseSupplier = Java.use('com.facebook.react.modules.storage.ReactDatabaseSupplier');
                const db = ReactDatabaseSupplier.getInstance(Java.use('android.app.ActivityThread').currentApplication()).get();
                const cursor = db.rawQuery('SELECT value FROM catalystLocalStorage WHERE key = ?', Java.array('java.lang.String', [String(key)]));
                try {
                    if (cursor.moveToFirst()) { out = String(cursor.getString(0)); }
                } finally {
                    cursor.close();
                }
            });
        } catch (e) {
            out = 'ERR:' + e;
        }
        return out;
    }
};

/* ---------- install ---------- */
let installed = false;
function installAll() {
    if (installed) { return; }
    installed = true;
    log('installing network hooks');
    const nOk = hookOkHttp();
    log('okhttp chains hooked: ' + nOk);
    const nUrl = hookUrlConnection();
    log('urlconnection hooked: ' + nUrl);
    const nBody = hookRequestBody();
    log('request-body hooks: ' + nBody);
    log('hooks installed');

    // OkHttp may load later; retry the cheap parts for a while.
    let tries = 0;
    const timer = setInterval(function () {
        tries += 1;
        if (nOk === 0) { hookOkHttp(); }
        if (nBody === 0) { hookRequestBody(); }
        if (tries >= 40) { clearInterval(timer); }
    }, 1000);
}

function currentPackage() {
    try {
        const ActivityThread = Java.use('android.app.ActivityThread');
        const app = ActivityThread.currentApplication();
        if (app === null) { return null; }
        return String(app.getPackageName());
    } catch (e) {
        return null;
    }
}

function whenAppReady(callback, attemptsLeft) {
    const left = (attemptsLeft === undefined) ? 120 : attemptsLeft;
    let pkg = null;
    try {
        Java.perform(function () {
            try { pkg = currentPackage(); } catch (e) { pkg = null; }
        });
    } catch (e) { pkg = null; }

    if (pkg === TARGET) { callback(); return; }
    if (left <= 0) { log('app never became ready (last package=' + pkg + ')'); return; }
    setTimeout(function () { whenAppReady(callback, left - 1); }, 500);
}

whenAppReady(function () {
    log('app ready; waiting for network classes');
    installAll();
});
