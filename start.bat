@echo off
title JarvisLT
cd /d "%~dp0"

:: Check Docker is running
docker info >nul 2>&1
if errorlevel 1 (
    echo Starting Docker Desktop...
    start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
    echo Waiting for Docker to start (up to 60s)...
    for /L %%i in (1,1,12) do (
        timeout /t 5 /nobreak >nul
        docker info >nul 2>&1
        if not errorlevel 1 goto :docker_ready
    )
    echo ERROR: Docker did not start. Please open Docker Desktop manually and try again.
    pause
    exit /b 1
)

:docker_ready
:: Start containers if not running
docker compose up -d >nul 2>&1

:: Run Jarvis
pipenv run python main.py

pause
