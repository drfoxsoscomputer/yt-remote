@echo off
chcp 65001 >nul
setlocal

REM Registra el mpv en el sistema (asociaciones de archivo).
"%~dp0/mpv" --register
if %errorlevel% neq 0 (
    echo El registro falló. Asegúrate de que mpv esté en la misma carpeta que este script.
    pause
    exit /b %errorlevel%
)

pause
endlocal