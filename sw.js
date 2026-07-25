// 離線快取策略（v2）
//
// 這個APP的內容每天換，但圖示、manifest 幾乎不動，所以兩者要分開處理：
//   index.html → 網路優先。有網路就一定拿最新的，沒網路才回頭用快取。
//   圖示等靜態檔 → 快取優先。省流量，反正不會變。
//
// v1 曾經對 index.html 也用快取優先，結果是「更新完打開還是看到昨天的，
// 要關掉再開第二次才會變」。每天更新的內容不能這樣做。
const CACHE = "hoops-v2";
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

self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;

  const isPage = e.request.mode === "navigate" ||
                 e.request.destination === "document" ||
                 e.request.url.endsWith(".html") ||
                 e.request.url.endsWith("/");

  if (isPage) {
    // 網路優先：抓得到就用新的，順便更新快取；抓不到才用舊的
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
          return res;
        })
        .catch(() => caches.match(e.request).then(hit => hit || caches.match("./index.html")))
    );
    return;
  }

  // 靜態檔：快取優先
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
