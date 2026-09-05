@echo off
chcp 65001 >nul
setlocal

REM Anula el registro del mpv en el sistema (quita las asociaciones).
"%~dp0/mpv" --unregister
if %errorlevel% neq 0 (
    echo No se pudo anular el registro. Asegúrate de que mpv esté en la misma carpeta que este script.
    pause
    exit /b %errorlevel%
)

pause
endlocal