@echo off
cd /d "%~dp0"
python sync_tool.py
if errorlevel 1 pause
