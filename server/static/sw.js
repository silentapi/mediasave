/* Media Saver service worker — cache the app shell only. Never /share, /login, /v1. */
const VERSION = 'ms-v0.1.0-dev';
const SHELL = ['/', '/static/app.css', '/static/app.js', '/static/share.js', '/manifest.webmanifest',
  '/static/icons/192.png', '/static/icons/512.png', '/static/icons/maskable.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL).catch(() => {})).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

function networkOnly(url) {
  return url.pathname.startsWith('/share') || url.pathname.startsWith('/login') || url.pathname.startsWith('/logout') || url.pathname.startsWith('/v1/');
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || networkOnly(url)) return; // browser handles it
  const isShell = url.pathname.startsWith('/static/') || url.pathname === '/' || url.pathname === '/manifest.webmanifest';
  if (!isShell) return;
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(e.request, copy)); }
      return res;
    }).catch(() => hit))
  );
});
