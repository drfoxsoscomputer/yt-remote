@echo off
REM Setup de YT-Remote - portable (no toca el sistema)
REM Todo el entorno vive DENTRO de la carpeta: runtime\python y runtime\mpv
echo ====================================
echo  YT-Remote - Verificacion portable
echo ====================================
echo.

cd /d "%~dp0"

echo [1/3] Verificando Python portable...
if not exist "runtime\python\python.exe" (
    echo.
    echo [!] Falta el Python portable en: runtime\python\python.exe
    echo.
    echo     Para que sea portable (funcione sin instalarlo en el sistema),
    echo     descarga el "Windows embeddable package" de python.org:
    echo     https://www.python.org/downloads/windows/
    echo.
    echo     Descomprime TODO el contenido del zip dentro de la carpeta:
    echo     yt-remote\runtime\python\
    echo     (debe quedar runtime\python\python.exe)
    echo.
    goto :error
)
echo     Python portable encontrado.

echo [2/3] Verificando dependencias...
"runtime\python\python.exe" -c "import yt_dlp, telegram" >nul 2>&1
if errorlevel 1 (
    echo     Instalando dependencias (primera vez)...
    "runtime\python\python.exe" -m pip install --no-warn-script-location yt-dlp python-telegram-bot
    if errorlevel 1 goto :error
) else (
    echo     Dependencias ya instaladas.
)

echo [3/3] Verificando mpv portable...
if not exist "runtime\mpv\mpv.exe" (
    echo.
    echo [!] Falta el mpv portable en: runtime\mpv\mpv.exe
    echo.
    echo     Descarga la build estatica de mpv para Windows y descomprime
    echo     su contenido en: yt-remote\runtime\mpv\
    echo     (debe quedar runtime\mpv\mpv.exe)
    echo.
    goto :error
)
echo     mpv portable encontrado.

echo.
echo ====================================
echo  Todo listo. Arranca con start.bat
echo ====================================
pause
exit /b 0

:error
echo.
echo [!] Error durante la verificacion.
pause
exit /b 1
