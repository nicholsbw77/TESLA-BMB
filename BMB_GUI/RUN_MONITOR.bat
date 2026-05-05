@echo off
cd /d "%~dp0"
echo Tesla BMB Live Monitor
echo ========================
set /p MOD="Enter module label (e.g. M05): "
"%USERPROFILE%\Desktop\TESLA\Scripts\python.exe" bmb_monitor.py --label %MOD%
pause
