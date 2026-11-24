@echo off
REM mise nightly wrapper (LIVE copy lives OUTSIDE the repo, in the user profile directory).
REM This file is the reference copy. Docs: docs/operations/nightly.md. Keep both in sync.
REM KEEP THIS FILE ASCII-ONLY. cmd parses batch files in the session codepage, which differs
REM between SSH paths; non-ASCII comments break line boundaries.
REM Extra arguments are passed to nightly.py (e.g. mise-nightly.cmd --dry-run).
REM Do NOT add --apply-prod until a human has checked the first few nights.
setlocal
set "LOG=%USERPROFILE%\mise-nightly.log"
cd /d %USERPROFILE%\repos\mise
echo ===== %DATE% %TIME% mise-nightly start >> "%LOG%"
git fetch origin >> "%LOG%" 2>&1
git reset --hard origin/main >> "%LOG%" 2>&1
set PYTHONUTF8=1
REM Load KEY=VALUE lines into the environment. Values are never echoed.
for /f "usebackq tokens=1,* delims==" %%a in ("%USERPROFILE%\.config\mise\anthropic.env") do set "%%a=%%b"
"%USERPROFILE%\.local\bin\uv.exe" run python scripts\nightly.py --fetch-limit 300 --judge-limit 300 %* >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo ===== %DATE% %TIME% mise-nightly end rc=%RC% >> "%LOG%"
exit /b %RC%
