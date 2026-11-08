/* Bundle a Frida payload together with the frida-java-bridge source into one
 * standalone script that frida-python can load directly.
 *
 * Why: Frida 17 removed the built-in Java bridge and payload-side `require`, so
 * the bridge has to be inlined by a bundle step. frida-compile cannot be used
 * here because its install pulls the native frida npm package (flaky download),
 * while esbuild is a plain dependency.
 *
 * Usage:
 *   node scripts/capture/build_payload.js                     # payload.js -> capture/payload.bundle.js
 *   node scripts/capture/build_payload.js diag.js capture/diag.bundle.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const projectRoot = path.resolve(__dirname, '..', '..');
const buildDir = path.join(projectRoot, 'vendor', 'frida-build');
const entryArg = process.argv[2] || 'payload.js';
const outArg = process.argv[3] || path.join('capture', 'payload.bundle.js');

const entryFile = path.isAbsolute(entryArg) ? entryArg : path.join(__dirname, entryArg);
const outFile = path.isAbsolute(outArg) ? outArg : path.join(projectRoot, outArg);

let esbuild;
try {
    esbuild = require(path.join(buildDir, 'node_modules', 'esbuild'));
} catch (e) {
    console.error('[build] esbuild not found under ' + buildDir);
    console.error('[build] run: (cd vendor/frida-build) npm install --ignore-scripts frida-java-bridge esbuild');
    process.exit(1);
}

const bridgeEntry = path.join(buildDir, 'node_modules', 'frida-java-bridge', 'index.js');
const bufferShim = path.join(buildDir, 'buffer-shim.js');
if (!fs.existsSync(bridgeEntry)) {
    console.error('[build] frida-java-bridge not found at ' + bridgeEntry);
    process.exit(1);
}
if (!fs.existsSync(bufferShim)) {
    console.error('[build] buffer shim not found at ' + bufferShim);
    process.exit(1);
}
if (!fs.existsSync(entryFile)) {
    console.error('[build] payload entry not found at ' + entryFile);
    process.exit(1);
}

fs.mkdirSync(path.dirname(outFile), { recursive: true });

try {
    const result = esbuild.buildSync({
        entryPoints: [entryFile],
        bundle: true,
        outfile: outFile,
        platform: 'neutral',
        format: 'iife',
        target: 'es2020',
        legalComments: 'none',
        logLevel: 'warning',
        absWorkingDir: buildDir,
        nodePaths: [path.join(buildDir, 'node_modules')],
        // frida-java-bridge imports Node's 'buffer'; the Frida runtime has no
        // such module, so substitute a shim implementing the used subset.
        alias: { buffer: bufferShim }
    });
    if (result.warnings && result.warnings.length) {
        result.warnings.forEach(function (w) {
            console.warn('[build] warning: ' + w.text);
        });
    }
} catch (e) {
    console.error('[build] esbuild failed: ' + (e.message || e));
    process.exit(1);
}

const size = fs.statSync(outFile).size;
console.log('[build] wrote ' + outFile + ' (' + size + ' bytes)');
