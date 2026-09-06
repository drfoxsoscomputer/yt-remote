@echo off
setlocal

REM Registra el mpv en el sistema (asociaciones de archivo).
"%~dp0/mpv" --register
if %errorlevel% neq 0 (
    "%~dp0..\python\python.exe" -c "print('El registro fall\u00f3. Aseg\u00farate de que mpv est\u00e9 en la misma carpeta que este script.')"
    pause
    exit /b %errorlevel%
)

pause
endlocal
