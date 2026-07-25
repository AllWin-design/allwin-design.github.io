@echo off
chcp 65001 >nul
REM ============================================================
REM  每天跑一次：預測 → 產生網頁 → 推上 GitHub Pages
REM
REM  ⚠️ 只有「本機自己跑」時才需要這個檔。
REM     已經搬上 GitHub Actions 的話，雲端會自動跑，不必再用它。
REM
REM  第一次使用前要改下面三個路徑。
REM ============================================================

set PREDICTOR=C:\Users\etet7\OneDrive\桌面\python WNBA espn.txt
set SITEDIR=C:\Users\etet7\hoops-site
set LOGDIR=C:\Users\etet7\OneDrive\桌面

set BUILDER=%SITEDIR%\app_builder.py
set LOGFILE=%LOGDIR%\wnba_prediction_log.json

echo [1/3] 跑今日預測...
python "%PREDICTOR%"
if errorlevel 1 echo    預測失敗，仍會用現有日誌產生網頁

echo [2/3] 產生網頁...
REM 第二個參數是「輸出資料夾」，不是檔名 —— 產生器會在裡面寫
REM index.html（免費版）與 premium.json（付費版）兩個檔。
python "%BUILDER%" "%LOGFILE%" "%SITEDIR%"
if errorlevel 1 goto :fail

REM 全聯盟表在日誌旁邊，一起複製過去給網頁用
copy /Y "%LOGDIR%\wnba_league.json" "%SITEDIR%\" >nul 2>&1

echo [3/3] 推上 GitHub...
cd /d "%SITEDIR%"
git add -A
git commit -m "更新預測 %date%" || echo    沒有變更，略過
git push
if errorlevel 1 goto :fail

echo 完成。網站約一分鐘後更新。
exit /b 0

:fail
echo 失敗，請看上面的訊息。
exit /b 1
