@echo off
chcp 65001 >nul 2>&1
title iWorks - Novel Toolkit
cd /d "%~dp0"

echo.
echo  iWorks Novel Toolkit v1.4.1
echo  Starting server...
echo.

python server.py

pause
