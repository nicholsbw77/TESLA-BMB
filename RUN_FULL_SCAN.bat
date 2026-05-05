@echo off
cd /d "%~dp0"
echo Tesla BMB Full Pack Scanner
echo =============================
echo This will walk you through all 16 modules one at a time.
echo Connect each module BMB connector when prompted.
echo.
"%USERPROFILE%\Desktop\TESLA\Scripts\python.exe" bmb_scan_all.py
pause
