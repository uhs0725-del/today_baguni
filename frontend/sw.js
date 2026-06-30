/* Service worker for "장보기 친구" PWA.
 *
 * Strategy: network-first for GET navigations and same-origin static/shell
 * (live data stays fresh, offline still loads the app shell). API responses
 * (/api/) are NEVER cached — always network; offline lets them fail naturally.
 * Everything is wrapped defensively so a SW error never breaks the site.
 */
const CACHE = "baguni-v28";

const SHELL = [
  "/",
  "/static/style.css?v=28",
  "/static/app.js?v=28",
  "/manifest.json",
  "/static/icons/icon-192.png?v=26",
  "/static/icons/icon-512.png?v=26",
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches
      .open(CACHE)
      .then(function (cache) {
        return cache.addAll(SHELL);
      })
      .catch(function () {
        /* precache best-effort — don't block install on a bad asset */
      })
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches
      .keys()
      .then(function (names) {
        return Promise.all(
          names.map(function (name) {
            if (name !== CACHE) {
              return caches.delete(name);
            }
            return null;
          })
        );
      })
      .then(function () {
        return self.clients.claim();
      })
      .catch(function () {
        /* ignore cleanup errors */
      })
  );
});

self.addEventListener("fetch", function (event) {
  var req = event.request;

  // Only handle same-origin GETs; everything else falls through to network.
  if (req.method !== "GET") {
    return;
  }

  var url;
  try {
    url = new URL(req.url);
  } catch (e) {
    return;
  }
  if (url.origin !== self.location.origin) {
    return;
  }

  // Never cache API calls — always go to the network.
  if (url.pathname.indexOf("/api/") === 0) {
    return;
  }

  var isNavigation =
    req.mode === "navigate" ||
    (req.headers.get("accept") || "").indexOf("text/html") !== -1;

  // Network-first, fall back to cache (shell stays available offline).
  event.respondWith(
    fetch(req)
      .then(function (res) {
        if (res && res.ok) {
          var copy = res.clone();
          caches
            .open(CACHE)
            .then(function (cache) {
              cache.put(req, copy);
            })
            .catch(function () {});
        }
        return res;
      })
      .catch(function () {
        return caches.match(req).then(function (hit) {
          if (hit) {
            return hit;
          }
          if (isNavigation) {
            return caches.match("/");
          }
          return Response.error();
        });
      })
  );
});
