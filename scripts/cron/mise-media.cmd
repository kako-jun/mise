@echo off
REM Dedicated checkout: do not reset it while this worker runs. ASCII only.
setlocal
for %%I in ("%~dp0..\..") do set "MISE_CODE=%%~fI"
cd /d "%MISE_CODE%"
if errorlevel 1 exit /b 1
set PYTHONUTF8=1
where uv.exe >nul 2>nul
if errorlevel 1 (set "MISE_UV=%USERPROFILE%\.local\bin\uv.exe") else (set "MISE_UV=uv.exe")
"%MISE_UV%" run --with pillow python scripts\media_worker.py --favicon-limit 100 --thumbnail-limit 10
exit /b %ERRORLEVEL%
