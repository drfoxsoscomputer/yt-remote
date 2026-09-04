@echo off
REM Setup de YT-Remote - primera ejecucion (portable, no toca el sistema)
echo ====================================
echo  YT-Remote - Instalacion inicial
echo ====================================
echo.

REM Crear virtualenv local
echo [1/3] Creando entorno virtual...
python -m venv venv
if errorlevel 1 goto :error

REM Activar e instalar dependencias
echo [2/3] Instalando dependencias...
call venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto :error

REM Verificar mpv portable dentro de la carpeta
echo [3/3] Verificando mpv portable...
if not exist "runtime\mpv\mpv.exe" (
    echo.
    echo [!] Falta el mpv portable en: runtime\mpv\mpv.exe
    echo.
    echo     Para que esto sea portable (funcione sin instalar nada en el
    echo     sistema), descarga el zip portable de mpv de:
    echo     https://sourceforge.net/projects/mpv-player-windows/files/
    echo.
    echo     Descomprime TODO el contenido del zip dentro de la carpeta:
    echo     yt-remote\runtime\mpv\
    echo     (debe quedar runtime\mpv\mpv.exe)
    echo.
    goto :error
)
echo     mpv portable encontrado en runtime\mpv\mpv.exe

echo.
echo ====================================
echo  Instalacion completada.
echo  Configura tu token en config.json
echo ====================================
pause
exit /b 0

:error
echo.
echo [!] Error durante la instalacion.
pause
exit /b 1
