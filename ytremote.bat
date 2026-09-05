@echo off
chcp 65001 >nul
REM YT-Remote: arranca el bot. Si es la primera vez, guía para configurarlo.
cd /d "%~dp0"

:inicio
REM Comprobar que está bien instalado (sin mostrar detalles técnicos)
if not exist "runtime\python\python.exe" goto :no_instalado
if not exist "runtime\mpv\mpv.exe" goto :no_instalado

REM Ver si ya está configurado
set "CFG_FILE=%TEMP%\ytremote_cfg.txt"
"runtime\python\python.exe" "src\setup_cli.py" is-configured > "%CFG_FILE%" 2>nul
set /p CONFIGURADO=<"%CFG_FILE%"
if /i "%CONFIGURADO%"=="SI" goto :arrancar

:configurar
echo.
echo ==================================================
echo   YT-Remote - Primera configuración
echo ==================================================
echo.
echo Faltan tus datos. Solo se hace esto una vez.
echo.
echo El TOKEN es la llave de tu bot. Para conseguirlo:
echo   1. Abre Telegram y busca a @BotFather
echo   2. Escríbele  /newbot   y sigue los pasos
echo   3. Te dará un código tipo:  123456:AAHxh...
echo      Ese es tu TOKEN. Pega el código completo aquí.
echo.
set /p TOKEN=  Pega tu TOKEN aquí: 
if "%TOKEN%"=="" goto :token_vacio
if /i "%TOKEN%"=="TU_TOKEN_AQUI" goto :token_vacio

echo.
echo Tu ID numérico de Telegram (para que te reconozca como dueño):
echo   1. Abre Telegram y busca a @userinfobot
echo   2. Escríbele cualquier mensaje (por ejemplo: hola)
echo   3. Te dirá tu ID, tipo:  123456789
echo      Ese es tu ID. Escríbelo aquí.
echo.
set /p OWNER=  Escribe tu ID aquí: 
if "%OWNER%"=="" goto :owner_vacio

REM Guardar los datos en el archivo de configuración
"runtime\python\python.exe" "src\setup_cli.py" save "%TOKEN%" "%OWNER%" >nul 2>&1
if errorlevel 1 goto :error_guardado

echo.
echo ¡Listo! Tus datos quedaron guardados.
echo Arrancando el bot...
echo.
goto :arrancar

:arrancar
echo Iniciando YT-Remote...
set "PYTHONIOENCODING=utf-8"
"runtime\python\python.exe" src\main.py
echo.
echo El bot se detuvo.
pause
exit /b 0

:token_vacio
echo.
echo [Error] El TOKEN no puede estar vacío. Vuelve a intentar.
echo.
goto :configurar

:owner_vacio
echo.
echo [Error] El ID no puede estar vacío. Vuelve a intentar.
echo.
goto :configurar

:error_guardado
echo.
echo [Error] No se pudieron guardar los datos. Revisa la guía GUIA.txt
echo y verifica que la carpeta no tenga permisos de solo lectura.
echo.
pause
exit /b 1

:no_instalado
echo.
echo ==================================================
echo   YT-Remote
echo ==================================================
echo.
echo Algo no está bien instalado en esta carpeta.
echo Vuelve a descargar la versión completa desde el repositorio
echo y descomprímela de nuevo. La guía está en GUIA.txt
echo.
pause
exit /b 1