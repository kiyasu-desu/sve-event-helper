/* 探测设备信息原生模块：找出提供 getUniqueId 的类与方法。
 * 用 Frida RPC 在宿主侧调用，避免反复重启 APP。
 */
'use strict';

import bridge from 'frida-java-bridge';
const Java = bridge.default || bridge;

rpc.exports = {
    list: function () {
        const out = [];
        Java.perform(function () {
            let all = [];
            try { all = Java.enumerateLoadedClassesSync(); } catch (e) { return; }
            const cands = all.filter(function (n) {
                return /deviceinfo|uniqueid|device_id|DeviceId/i.test(n) && !n.startsWith('[');
            });
            cands.slice(0, 40).forEach(function (n) {
                const info = { name: n, methods: [] };
                try {
                    const C = Java.use(n);
                    const ms = C.class.getDeclaredMethods();
                    ms.forEach(function (m) {
                        const mn = m.getName();
                        if (/unique|id|udid/i.test(mn)) { info.methods.push(mn); }
                    });
                } catch (e) {
                    info.methods = ['<use failed: ' + e + '>'];
                }
                out.push(info);
            });
        });
        return out;
    }
};
