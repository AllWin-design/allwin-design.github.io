// 離線快取：先給快取、背景更新（stale-while-revalidate）
// 這樣使用者在通勤沒訊號時也看得到昨天抓好的預測。
//
// 【v2】三個修正：
//   1. 加離線頁。原本快取沒命中又斷網時 respondWith 會拿到 undefined，
//      瀏覽器直接顯示錯誤頁 —— 在 TWA 裡就是 Chrome 恐龍，看起來像 App 壞了。
//   2. 補上 postMessage。頁面那邊一直在監聽 "content-updated" 並準備顯示
//      「有新資料，點一下更新」，但這支從來沒發過那個訊息，橫幅等於不存在。
//   3. 快取名稱改 v2，讓舊版的快取在 activate 時被清掉（含沒有離線頁的那份）。
const CACHE = "hoops-v2";
const OFFLINE = "./offline.html";
const ASSETS = ["./", "./index.html", "./manifest.json", OFFLINE,
                "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

// 這一份跟上一份是不是同一個東西。
// 用 ETag 優先、Last-Modified 次之 —— GitHub Pages 兩個都會給。
// 都拿不到時退回比長度：不精確，但只會少報不會誤報，
// 而誤報（每次開都跳「有新資料」）比少報煩人得多。
function changed(oldRes, newRes) {
  if (!oldRes || !newRes) return false;
  const a = oldRes.headers, b = newRes.headers;
  const ea = a.get("etag"), eb = b.get("etag");
  if (ea && eb) return ea !== eb;
  const ma = a.get("last-modified"), mb = b.get("last-modified");
  if (ma && mb) return ma !== mb;
  const la = a.get("content-length"), lb = b.get("content-length");
  if (la && lb) return la !== lb;
  return false;
}

async function tellClients() {
  const cs = await self.clients.matchAll({ type: "window" });
  for (const c of cs) c.postMessage({ type: "content-updated" });
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  // 只管自己網域的東西。外部資源（例如字型 CDN）交給瀏覽器自己處理，
  // 快取它們只會讓快取膨脹，而且 opaque 回應也判斷不了新舊。
  if (new URL(req.url).origin !== self.location.origin) return;

  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req);

    const net = fetch(req).then(async res => {
      if (res && res.status === 200) {
        // 內容真的變了才通知。每次都通知的話橫幅會一直跳，
        // 使用者兩天就學會無視它。
        if (hit && changed(hit, res)) {
          // 等一下再發：讓畫面先渲染完，不要一進來就跳橫幅。
          e.waitUntil(tellClients());
        }
        await cache.put(req, res.clone());
      }
      return res;
    }).catch(() => null);

    if (hit) return hit;                  // 有快取就秒開，網路在背景跑
    const res = await net;
    if (res) return res;

    // 走到這裡代表：沒有快取、而且網路也拿不到。
    // 導覽請求給離線頁，其餘給一個乾淨的 503 —— 都比恐龍頁好。
    if (req.mode === "navigate") {
      const off = await cache.match(OFFLINE);
      if (off) return off;
    }
    return new Response("", { status: 503, statusText: "offline" });
  })());
});