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
// v5（1.44）：加上 push / notificationclick 骨架。這版沒有發送端，
//     所以這兩段永遠不會被觸發 —— 放進來的理由是 9/18 之後沒有補版本的
//     機會，等十月接上伺服器推播時，才不必為了「service worker 少了
//     handler」再推一次殼。快取策略完全沒動，CACHE 名稱刻意維持 v4：
//     改名會讓所有人重新下載一整包，為了兩個用不到的 handler 不值得。
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

// ─────────────────────────────────────────────────────────────
// 推播（骨架，這版沒有發送端）
// ─────────────────────────────────────────────────────────────

// 通知的預設內容。伺服器沒送 payload、或 payload 壞掉時用這組，
// 總比跳出一則空白通知好 —— 空白通知會直接被使用者關掉通知權限。
const NOTIFY_FALLBACK = {
  title: "賽事更新",
  body: "點開看今天的預測。",
  url: "./#tab=games"
};

function parsePush(event) {
  if (!event.data) return {};
  // 送 JSON 是計畫中的格式，但純文字也要接得住：
  // 發送端寫錯格式時，退成「標題用那段文字」比整則不顯示好。
  try {
    return event.data.json() || {};
  } catch (e) {
    try {
      return { title: event.data.text() };
    } catch (e2) {
      return {};
    }
  }
}

// 只接受同源的目的地。payload 是外部來的，直接拿去 openWindow 等於讓
// 發送端把使用者導去任何網站；萬一金鑰外流，那是最好用的一條路。
function safeUrl(raw) {
  if (!raw) return NOTIFY_FALLBACK.url;
  try {
    const u = new URL(raw, self.registration.scope);
    if (u.origin !== self.location.origin) return NOTIFY_FALLBACK.url;
    return u.href;
  } catch (e) {
    return NOTIFY_FALLBACK.url;
  }
}

self.addEventListener("push", event => {
  const d = parsePush(event);
  const title = d.title || NOTIFY_FALLBACK.title;
  const opts = {
    body: d.body || NOTIFY_FALLBACK.body,
    icon: "./icon-192.png",
    // Android 狀態列的小圖示。只吃 alpha 通道、會被系統染成單色，
    // 所以一定要用單色圖，拿 icon-192 來充數會變成一坨黑塊。
    badge: "./icon-monochrome-512.png",
    tag: d.tag || "hoops",          // 同 tag 會取代舊的，不會疊成一串
    renotify: false,
    data: { url: safeUrl(d.url) }
  };
  event.waitUntil(
    self.registration.showNotification(title, opts).catch(
      () => console.warn("[sw] showNotification 失敗，略過這則"))
  );
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url)
    || NOTIFY_FALLBACK.url;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then(list => {
        // App 已經開著就切過去並帶到指定分頁，不要再開一個新視窗。
        for (const c of list) {
          if (c.url.indexOf(self.location.origin) === 0 && "focus" in c) {
            if ("navigate" in c) { try { c.navigate(target); } catch (e) {} }
            return c.focus();
          }
        }
        if (self.clients.openWindow) return self.clients.openWindow(target);
        return null;
      })
  );
});

// 訂閱失效時瀏覽器會發這個事件（金鑰輪替、系統清資料）。
// 這版沒有訂閱可以重新註冊，先留位置與紀錄；十月接上發送端時，
// 這裡要重新 subscribe 並把新的 endpoint 送回伺服器 ——
// 少了這段的症狀是「某些使用者某天起再也收不到通知，而且不會有人察覺」。
self.addEventListener("pushsubscriptionchange", () => {
  console.warn("[sw] 推播訂閱已失效，尚未實作重新註冊");
});