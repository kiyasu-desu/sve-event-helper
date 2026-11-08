/* Frida payload for com.shadowversecard on Android 15.
 *
 * Frida 17 no longer ships the Java bridge inside the runtime, so this file is
 * written as a CommonJS module that imports the bridge explicitly, and is then
 * bundled (bridge included) by scripts/capture/build_payload.js.
 *
 * What it does, so mitmproxy can read the app's HTTPS traffic:
 *   1. TrustManager / SSLContext: every checkServerTrusted() succeeds;
 *   2. OkHttp CertificatePinner: neutered;
 *   3. Conscrypt TrustManagerImpl.verifyChain(): neutered;
 *   4. HttpsURLConnection: permissive socket factory + allow-all hostnames.
 *
 * Every step is independent and guarded: one failing class cannot disable the
 * others. Progress is reported to the host as "send(...)" messages.
 */
'use strict';

// frida-java-bridge 7.x is an ES module, so the bundled namespace keeps the API
// under .default; accept both shapes for robustness.
import bridge from 'frida-java-bridge';
const Java = bridge.default || bridge;

const TAG = 'unpin';
const TARGET = 'com.shadowversecard';

function log(msg) {
    try { send(TAG + ': ' + msg); } catch (e) { /* ignore */ }
}

function targetLoadedClasses() {
    try {
        return Java.enumerateLoadedClassesSync();
    } catch (e) {
        return [];
    }
}

/* 1. generic trust managers.
 *
 * Overloads must be handled by return type: Android's TrustManagerImpl has a
 * checkServerTrusted() that returns java.util.List, and returning undefined for
 * it makes Frida raise "expected return value compatible with java.util.List".
 * Returning an empty ArrayList keeps the permissive behaviour without breaking
 * the type contract.
 */
function emptyList() {
    return Java.use('java.util.ArrayList').$new();
}

function hookTrustManagers() {
    let hooked = 0;
    targetLoadedClasses().forEach(function (name) {
        if (!/TrustManager/i.test(name) || name.indexOf('$') !== -1) { return; }
        try {
            const C = Java.use(name);
            const methods = C.class.getDeclaredMethods();
            methods.forEach(function (m) {
                const mn = m.getName();
                if (mn !== 'checkServerTrusted' && mn !== 'checkClientTrusted' && mn !== 'verifyChain') {
                    return;
                }
                const ret = String(m.getReturnType().getName());
                try {
                    C[mn].overloads.forEach(function (ov) {
                        ov.implementation = function () {
                            if (ret === 'void') { return; }
                            if (ret === 'java.util.List') { return emptyList(); }
                            if (ret === 'boolean') { return true; }
                            return null;
                        };
                    });
                    hooked += 1;
                } catch (e) { /* overloads not hookable */ }
            });
        } catch (e) { /* class not usable */ }
    });
    log('TrustManager hooks: ' + hooked);
}

/* Java.registerClass name must be unique per process, so make one per call. */
let registeredCount = 0;
function permissiveTrustManagerClass() {
    registeredCount += 1;
    return Java.registerClass({
        name: 'com.dsh.PermissiveTM' + registeredCount,
        implements: [Java.use('javax.net.ssl.X509TrustManager')],
        methods: {
            checkClientTrusted: function () { },
            checkServerTrusted: function () { },
            getAcceptedIssuers: function () { return []; }
        }
    });
}

function hookSslContext() {
    try {
        const SSLContext = Java.use('javax.net.ssl.SSLContext');
        const overload = SSLContext.init.overload(
            '[Ljavax.net.ssl.KeyManager;',
            '[Ljavax.net.ssl.TrustManager;',
            'java.security.SecureRandom'
        );
        overload.implementation = function (km, tm, sr) {
            const TM = permissiveTrustManagerClass();
            log('SSLContext.init() -> permissive trust manager');
            return overload.call(this, km, [TM.$new()], sr);
        };
        log('SSLContext.init hooked');
    } catch (e) {
        log('SSLContext.init hook failed: ' + e.message);
    }
}

/* 2. OkHttp pinner */
function hookOkHttpPinner() {
    let hooked = 0;
    targetLoadedClasses().forEach(function (name) {
        if (!/CertificatePinner$/i.test(name)) { return; }
        try {
            const C = Java.use(name);
            C.check.overloads.forEach(function (ov) {
                ov.implementation = function () {
                    log('CertificatePinner.check() bypassed (' + name + ')');
                    return;
                };
                hooked += 1;
            });
        } catch (e) { /* ignore */ }
    });
    if (hooked === 0) { log('CertificatePinner: not loaded yet'); }
    return hooked;
}

/* 3. Conscrypt — verifyChain returns a List, so return an empty one */
function hookConscrypt() {
    let hooked = 0;
    targetLoadedClasses().forEach(function (name) {
        if (!/TrustManagerImpl|ConscryptEngineSocket|ConscryptFileDescriptorSocket/i.test(name)) { return; }
        try {
            const C = Java.use(name);
            try {
                C.verifyChain.overloads.forEach(function (ov) {
                    ov.implementation = function () { return emptyList(); };
                    hooked += 1;
                });
            } catch (e) { /* no verifyChain here */ }
        } catch (e) { /* ignore */ }
    });
    log('Conscrypt verifyChain hooks: ' + hooked);
}

/* 4. HttpsURLConnection */
function hookHttpsUrlConnection() {
    try {
        const H = Java.use('javax.net.ssl.HttpsURLConnection');
        H.setDefaultHostnameVerifier.implementation = function () {
            log('setDefaultHostnameVerifier ignored');
            return;
        };
        H.setSSLSocketFactory.implementation = function () {
            log('setSSLSocketFactory ignored');
            return;
        };
        log('HttpsURLConnection hooked');
    } catch (e) {
        log('HttpsURLConnection hook failed: ' + e.message);
    }
}

let installed = false;
function installAll() {
    if (installed) { return; }
    installed = true;
    log('STAGE install start (arch=' + Process.arch + ')');
    hookTrustManagers();
    log('STAGE trust managers done');
    hookSslContext();
    log('STAGE ssl context done');
    hookOkHttpPinner();
    log('STAGE okhttp done');
    hookConscrypt();
    log('STAGE conscrypt done');
    hookHttpsUrlConnection();
    log('STAGE https url connection done');
    log('hooks installed');

    // OkHttp / Conscrypt may load after us: keep retrying briefly.
    let tries = 0;
    const timer = setInterval(function () {
        tries += 1;
        hookOkHttpPinner();
        hookConscrypt();
        if (tries >= 20) { clearInterval(timer); }
    }, 500);
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

/* Wait until Java is up AND the Application object exists.
 *
 * Two traps learned the hard way:
 *   - with the Frida 17 bundled bridge, Java.available is undefined until
 *     perform() has run, so it cannot be used as a readiness probe;
 *   - when spawning, Java.perform() already succeeds before the Application
 *     object is constructed, and currentPackage() then returns null. Treating
 *     that null as "wrong app" silently disables the payload, so poll instead.
 */
function whenAppReady(callback, attemptsLeft) {
    const left = (attemptsLeft === undefined) ? 120 : attemptsLeft;
    let pkg = null;
    try {
        Java.perform(function () {
            try {
                pkg = currentPackage();
            } catch (e) {
                pkg = null;
            }
        });
    } catch (e) {
        pkg = null;
    }
    if (pkg === TARGET) {
        log('STAGE app ready after polling, package=' + pkg);
        callback();
        return;
    }
    if (left <= 0) {
        log('app never became ready (last package=' + pkg + '); payload idle');
        return;
    }
    setTimeout(function () { whenAppReady(callback, left - 1); }, 500);
}

whenAppReady(function () {
    installAll();
});
