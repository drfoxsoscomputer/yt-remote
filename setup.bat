@echo off
REM Setup de YT-Remote - primera ejecucion
echo ====================================
echo  YT-Remote - Instalacion inicial
echo ====================================
echo.

REM Crear virtualenv
echo [1/3] Creando entorno virtual...
python -m venv venv
if errorlevel 1 goto :error

REM Activar e instalar dependencias
echo [2/3] Instalando dependencias...
call venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto :error

REM Verificar mpv
echo [3/3] Verificando mpv...
where mpv >nul 2>&1
if errorlevel 1 (
    echo [!] mpv no encontrado. Instalado con: winget install mpv
)

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
