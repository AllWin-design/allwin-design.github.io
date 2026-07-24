@echo off
chcp 65001 >nul
REM ============================================================
REM  每天跑一次：預測 → 產生網頁 → 推上 GitHub Pages
REM
REM  第一次使用前要改的兩個路徑：
REM    PREDICTOR = 你的主程式檔案
REM    SITEDIR   = 你 clone 下來的 GitHub 專案資料夾
REM ============================================================

set PREDICTOR=C:\Users\etet7\OneDrive\桌面\python WNBA espn.txt
set SITEDIR=C:\Users\etet7\OneDrive\桌面\hoops-site
set BUILDER=%SITEDIR%\app_builder.py
set LOGFILE=C:\Users\etet7\OneDrive\桌面\wnba_prediction_log.json

echo [1/3] 跑今日預測...
python "%PREDICTOR%"
if errorlevel 1 echo    預測失敗，仍會用現有日誌產生網頁

echo [2/3] 產生網頁...
python "%BUILDER%" "%LOGFILE%" "%SITEDIR%\index.html"
if errorlevel 1 goto :fail

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
