@echo off
cd /d "%~dp0"
python videoplayer_converter.py %*
if errorlevel 1 pause
