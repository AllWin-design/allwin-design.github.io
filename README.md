# AI 籃球賽事預測

用整季資料重建球隊評分，算出每場比賽的勝率與分差分佈。

## 這個資料夾裡有什麼

| 檔案 | 用途 |
|---|---|
| `index.html` | 網站本體（由 app_builder.py 產生，不要手動改） |
| `app_builder.py` | 產生器：讀預測日誌 → 產生 index.html |
| `manifest.json` | PWA 設定，決定加入主畫面後的名稱與圖示 |
| `sw.js` | 離線快取 |
| `privacy.html` | 隱私權政策（上架必要） |
| `icon-*.png` | 應用程式圖示 |
| `feature-1024x500.png` | Google Play 宣傳大圖 |
| `deploy.bat` | 每日自動更新用 |

## 每天更新

```
deploy.bat
```

## 手動更新

```
python app_builder.py 你的wnba_prediction_log.json index.html
git add -A && git commit -m "更新" && git push
```
