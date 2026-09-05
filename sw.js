// 離線快取策略（v4）
//
// v1：index.html 也用快取優先 → 「更新完打開還是昨天的，要開第二次才會變」。
// v2：改成網路優先，資料新了，但每次開 App 都要等整份 HTML 下載完才看得到
//     東西 —— 手機剛連上網的那幾秒特別明顯，畫面一片空白。
// v3：改成 stale-while-revalidate：先把快取的畫面端出來（瞬間開啟），
//     同時在背景抓新的；抓回來發現有變，才通知頁面顯示「有新資料」。
//     兼顧「開得快」和「看得到新的」，兩者不再互斥。
// v4：補離線 fallback。v3 在「沒有快取又沒有網路」時會回到 undefined，
//     瀏覽器直接顯示錯誤頁 —— 在 TWA 裡就是 Chrome 恐龍，看起來像 App 壞了。
//     第一次開就沒訊號的人只會看到那個。策略本身不動，只是把破口補上。
//
// 判斷有沒有變是比對 ETag（GitHub Pages 會送），沒有就退而比對長度。
const CACHE = "hoops-v4";
// v4 把 index.html 與 offline.html 加進預快取。
// index.html 進來不會回到 v1 的問題 —— 頁面那條走的是 stale-while-revalidate，
// 預快取只是保證「第一次就離線」的人至少有東西可看，之後照樣每次背景更新。
const ASSETS = ["./index.html", "./offline.html", "./manifest.json",
                "./icon-192.png", "./icon-512.png"];
const OFFLINE = "./offline.html";

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE)
      // 逐檔加入，不用 addAll：addAll 只要有一個檔案 404 就整批失敗，
      // service worker 會完全裝不起來 —— 比少快取一個檔案嚴重得多。
      // 新增靜態檔而忘記改 daily.yml 的複製清單時，就是這種情況。
      .then(c => Promise.all(ASSETS.map(u =>
        c.add(u).catch(() => console.warn("[sw] 快取不到，略過：" + u))
      )))
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

function lastResort() {
  return new Response(
    "<!doctype html><meta charset=utf-8>" +
    "<body style=\"background:#0E151C;color:#E6EDF3;font-family:system-ui;" +
    "padding:40px;text-align:center;line-height:1.8\">" +
    "<p>目前沒有連線，也還沒有可以顯示的內容。</p>" +
    "<p>連上網路後重新開啟即可。</p>",
    { headers: { "Content-Type": "text/html; charset=utf-8" }, status: 503 });
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
          if (hit) return hit;
          return net
            .then(r => r || c.match("./index.html"))
            // v4：最後兩道。前面都落空 = 沒快取又沒網路，
            // 這時回 undefined 就是恐龍頁，所以給離線說明頁。
            .then(r => r || c.match(OFFLINE))
            .then(r => r || lastResort());
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
      // v4：沒有這個 catch 的話，離線時 fetch 直接 reject，
      // respondWith 收到 rejected promise 一樣是錯誤畫面。
      }).catch(() => new Response("", { status: 503, statusText: "offline" }))
    )
  );
});
