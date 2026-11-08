/* ===================================================================
 *  sw.js —— Service Worker（保守策略）
 *
 *  这是一个 24/7 的控制面板，缓存出错的代价很高：
 *    * 缓存了旧界面 -> "改了界面却看不到"
 *    * 缓存了 /api/* -> 看到过期的名额与订阅状态（可能据此做出错误决定）
 *  所以策略刻意保守：
 *
 *    /api/*            -> 网络优先，**绝不写入缓存**。离线时直接失败，
 *                         绝不返回上一次的旧数据冒充实时状态。
 *    导航请求(HTML)     -> 网络优先，失败才回退缓存（离线壳）。
 *                         这样只要在线，界面永远是最新的。
 *    其它同源 GET       -> 陈旧内容先回、后台更新（图标/manifest 变动少）
 *
 *  版本号变更时在 activate 里清掉所有旧缓存。
 *  改动界面后**记得把 VERSION 加一**，否则老客户端可能继续用缓存的壳。
 * =================================================================== */

const VERSION = 'sve-v25-new-repository';
const SHELL_CACHE = `${VERSION}-shell`;
const ASSET_CACHE = `${VERSION}-assets`;
const API_PATH = new URL('./api/', self.registration.scope).pathname;

// 离线兜底用的最小外壳（不含业务数据）
const SHELL = [
  './',
  './index.html',
  './subs',
  './subs.html',
  './settings',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './icon-maskable-512.png',
  './apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // 逐个 add：某一个失败不应让整个安装失败
    await Promise.all(SHELL.map((u) =>
      cache.add(new Request(u, { cache: 'reload' })).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter((k) => !k.startsWith(VERSION))
      .map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // 只处理同源 GET
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 1) 接口数据：网络优先，绝不缓存
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith(API_PATH)) {
    event.respondWith(fetch(req));
    return;
  }

  // 2) 页面导航：网络优先，失败回退缓存的壳
  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        const cache = await caches.open(SHELL_CACHE);
        cache.put(req, fresh.clone()).catch(() => {});
        return fresh;
      } catch (err) {
        const cached = await caches.match(req, { ignoreSearch: true });
        if (cached) return cached;
        const shell = await caches.match('./index.html');
        if (shell) return shell;
        return new Response('离线，且没有可用的缓存界面。', {
          status: 503,
          headers: { 'content-type': 'text/plain; charset=utf-8' },
        });
      }
    })());
    return;
  }

  // 3) 其它静态资源：先给缓存，同时后台拉新（stale-while-revalidate）
  event.respondWith((async () => {
    const cache = await caches.open(ASSET_CACHE);
    const cached = await cache.match(req);
    const network = fetch(req).then((res) => {
      if (res && res.ok) cache.put(req, res.clone()).catch(() => {});
      return res;
    }).catch(() => null);
    return cached || (await network) ||
      new Response('', { status: 504, statusText: 'offline' });
  })());
});

// 允许页面主动要求立即更新（见 index.html 的注册代码）
self.addEventListener('message', (event) => {
  if (event.data === 'skip-waiting') self.skipWaiting();
});
