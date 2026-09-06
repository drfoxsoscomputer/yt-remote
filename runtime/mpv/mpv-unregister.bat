@echo off
setlocal

REM Anula el registro del mpv en el sistema (quita las asociaciones).
"%~dp0/mpv" --unregister
if %errorlevel% neq 0 (
    "%~dp0..\python\python.exe" -c "print('No se pudo anular el registro. Aseg\u00farate de que mpv est\u00e9 en la misma carpeta que este script.')\"
    pause
    exit /b %errorlevel%
)

pause
endlocal
