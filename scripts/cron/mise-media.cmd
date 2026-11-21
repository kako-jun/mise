@echo off
REM Dedicated checkout: do not reset it while this worker runs. ASCII only.
setlocal
set "MISE_CODE=C:\Users\kako-jun\repos\mise-media"
set "MISE_VAR=C:\Users\kako-jun\repos\mise\var"
cd /d "%MISE_CODE%"
if errorlevel 1 exit /b 1
set PYTHONUTF8=1
"C:\Users\kako-jun\.local\bin\uv.exe" run --with pillow python scripts\media_worker.py --var "%MISE_VAR%" --wsl-distro Arch --wsl-playwright .local/share/mise-media/node_modules/playwright --wsl-chromium .local/share/mise-media/chromium >> "%MISE_VAR%\nightly\media-worker.log" 2>&1
exit /b %ERRORLEVEL%
