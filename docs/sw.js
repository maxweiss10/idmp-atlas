/* IDMP Atlas service worker: shell precache, network-first pages with offline fallback, optional whole-site download. */
var VER = '8a352293';
var SHELL = 'idmp-shell-' + VER, PAGES = 'idmp-pages-' + VER;
var CORE = ['./', './index.html', './offline.html', './assets/site.css?v=' + VER, './assets/site.js?v=' + VER, './index.json', './manifest.webmanifest', './assets/icon-192.png'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(SHELL).then(function (c) {
    return Promise.all(CORE.map(function (u) { return c.add(u).catch(function () {}); }));
  }).then(function () { return self.skipWaiting(); }));
});
self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.indexOf('idmp-') === 0 && k.indexOf(VER) < 0; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});
function isPage(req) { return req.mode === 'navigate' || (req.headers.get('accept') || '').indexOf('text/html') >= 0; }
self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== self.location.origin) return;
  if (isPage(req)) {
    e.respondWith(fetch(req).then(function (res) {
      var copy = res.clone(); caches.open(PAGES).then(function (c) { c.put(req, copy); }); return res;
    }).catch(function () {
      return caches.match(req, { ignoreSearch: true }).then(function (hit) { return hit || caches.match('./offline.html'); });
    }));
    return;
  }
  if (/status\.json|antibiogram\.json|index\.json/.test(req.url)) {
    e.respondWith(fetch(req).then(function (res) { var copy = res.clone(); caches.open(SHELL).then(function (c) { c.put(req, copy); }); return res; })
      .catch(function () { return caches.match(req); }));
    return;
  }
  e.respondWith(caches.match(req).then(function (hit) {
    var net = fetch(req).then(function (res) { if (res.ok) { var copy = res.clone(); caches.open(PAGES).then(function (c) { c.put(req, copy); }); } return res; }).catch(function () { return hit; });
    return hit || net;
  }));
});
self.addEventListener('message', function (e) {
  var msg = e.data || {};
  if (msg.type !== 'precache' || !msg.pages) return;
  var client = e.source, list = msg.pages.slice(), done = 0, total = list.length;
  caches.open(PAGES).then(function (c) {
    function next() {
      var batch = list.splice(0, 12);
      if (!batch.length) { client && client.postMessage({ type: 'precache-done', total: total }); return; }
      return Promise.all(batch.map(function (u) { return c.add('./' + u).catch(function () {}); })).then(function () {
        done += batch.length; client && client.postMessage({ type: 'precache-progress', done: done, total: total }); return next();
      });
    }
    return next();
  });
});
