@echo off
REM Run from a dedicated checkout, NOT the checkout reset by mise-nightly.cmd.
REM The Windows state directory is shared with the existing nightly job.
REM KEEP ASCII ONLY. The API key is read into the environment and never printed.
setlocal
set "MISE_CODE=%USERPROFILE%\repos\mise-continuous"
set "MISE_VAR=%USERPROFILE%\repos\mise\var"
cd /d "%MISE_CODE%"
if errorlevel 1 exit /b 1
set PYTHONUTF8=1
for /f "usebackq tokens=1,* delims==" %%a in ("%USERPROFILE%\.config\mise\anthropic.env") do set "%%a=%%b"
"%USERPROFILE%\.local\bin\uv.exe" run python scripts\continuous.py --apply-prod --var-dir "%MISE_VAR%" --db "%MISE_VAR%\frontier.sqlite" >> "%MISE_VAR%\nightly\continuous.log" 2>&1
exit /b %ERRORLEVEL%
