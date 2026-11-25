@echo off
chcp 65001 >nul
title AlbumWorkbench
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3 not found. Please install Python 3.8+ first.
  pause
  exit /b 1
)
%PY% "%~dp0AlbumWorkbench.py" %*
if errorlevel 1 (
  echo.
  echo [ERROR] Something went wrong. Please copy the text above.
  pause
)
