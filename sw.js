// 離線快取策略（v3）
//
// v1：index.html 也用快取優先 → 「更新完打開還是昨天的，要開第二次才會變」。
// v2：改成網路優先，資料新了，但每次開 App 都要等整份 HTML 下載完才看得到
//     東西 —— 手機剛連上網的那幾秒特別明顯，畫面一片空白。
// v3：改成 stale-while-revalidate：先把快取的畫面端出來（瞬間開啟），
//     同時在背景抓新的；抓回來發現有變，才通知頁面顯示「有新資料」。
//     兼顧「開得快」和「看得到新的」，兩者不再互斥。
//
// 判斷有沒有變是比對 ETag（GitHub Pages 會送），沒有就退而比對長度。
const CACHE = "hoops-v3";
const ASSETS = ["./manifest.json", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function tagOf(res) {
  if (!res) return null;
  return res.headers.get("etag") || res.headers.get("last-modified")
      || res.headers.get("content-length");
}

function tellClients() {
  return self.clients.matchAll({ type: "window" }).then(list => {
    list.forEach(c => c.postMessage({ type: "content-updated" }));
  });
}

self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;

  const isPage = e.request.mode === "navigate" ||
                 e.request.destination === "document" ||
                 e.request.url.endsWith(".html") ||
                 e.request.url.endsWith("/");

  if (isPage) {
    e.respondWith(
      caches.open(CACHE).then(c =>
        c.match(e.request).then(hit => {
          // 背景更新：不擋住畫面，抓回來再說
          const net = fetch(e.request).then(res => {
            if (res && res.status === 200) {
              const changed = hit && tagOf(hit) && tagOf(res) &&
                              tagOf(hit) !== tagOf(res);
              c.put(e.request, res.clone());
              if (changed) tellClients();
            }
            return res;
          }).catch(() => null);

          // 有快取就先給快取（開啟是瞬間的），沒有才等網路
          return hit || net.then(r => r || c.match("./index.html"));
        })
      )
    );
    return;
  }

  // 靜態檔：快取優先，反正不會變
  e.respondWith(
    caches.match(e.request).then(hit =>
      hit || fetch(e.request).then(res => {
        if (res && res.status === 200) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return res;
      })
    )
  );
});