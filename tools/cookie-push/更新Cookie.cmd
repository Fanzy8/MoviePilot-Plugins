@echo off
REM 推送 ego 浏览器里四站 Cookie 到 MoviePilot「自用签到」插件
REM 需要：Python 3、ego-browser 在 PATH 里、本目录有 config.json
setlocal
cd /d "%~dp0"
python push-cookies.py %*
echo.
pause
