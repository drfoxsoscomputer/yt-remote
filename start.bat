@echo off
REM Arranca el bot de YT-Remote usando el Python embebido de la carpeta
cd /d "%~dp0"

if not exist "runtime\python\python.exe" (
    echo ERROR: No se encontro el Python portable en runtime\python\
    echo Ejecuta setup.bat primero o descargalo.
    pause
    exit /b 1
)

if not exist "runtime\mpv\mpv.exe" (
    echo ERROR: No se encontro mpv en runtime\mpv\mpv.exe
    echo Ejecuta setup.bat primero o descargalo.
    pause
    exit /b 1
)

echo Iniciando YT-Remote...
"runtime\python\python.exe" src\main.py
pause
