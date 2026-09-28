@echo off
chcp 65001 >nul
rem Start without requesting administrator rights.
rem GPU temp / VRAM / loads / memory all work; only CPU die temp needs admin.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0scripts\launch.ps1" -NoElevate
