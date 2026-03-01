@echo off
cd /d "%~dp0"
pip install -r requirements.txt
python videoplayer_converter.py %*
if errorlevel 1 pause
