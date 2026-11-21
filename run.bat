
@echo off
title Face Attendance System

cd /d "%~dp0"

call venv\Scripts\activate.bat

echo Starting Face Attendance System...
python app.py

pause
