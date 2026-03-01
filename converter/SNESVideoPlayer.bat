@echo off
cd /d "%~dp0"
where uv >nul 2>&1
if not %errorlevel%==0 (
    echo Installing uv...
    powershell -ExecutionPolicy ByPass -NoProfile -c "irm https://astral.sh/uv/install.ps1 | iex"
)
uv run videoplayer_converter.py %*
if errorlevel 1 pause
