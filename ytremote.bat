@echo off
REM YT-Remote: arranca el bot. Si es la primera vez, guia para configurarlo.
cd /d "%~dp0"

:inicio
REM Comprobar que esta bien instalado (sin mostrar detalles tecnicos)
if not exist "runtime\python\python.exe" goto :no_instalado
if not exist "runtime\mpv\mpv.exe" goto :no_instalado

REM Ver si ya esta configurado
set "CFG_FILE=%TEMP%\ytremote_cfg.txt"
"runtime\python\python.exe" "src\setup_cli.py" is-configured > "%CFG_FILE%" 2>nul
set /p CONFIGURADO=<"%CFG_FILE%"
if /i "%CONFIGURADO%"=="SI" goto :arrancar

:configurar
echo.
echo ==================================================
echo   YT-Remote - Primera configuracion
echo ==================================================
echo.
echo Faltan tus datos. Solo se hace esto una vez.
echo.
echo El TOKEN es la llave de tu bot. Para conseguirlo:
echo   1. Abre Telegram y busca a @BotFather
echo   2. Escribele  /newbot   y sigue los pasos
echo   3. Te dara un codigo tipo:  123456:AAHxh...
echo      Ese es tu TOKEN. Pega el codigo completo aqui.
echo.
set /p TOKEN=  Pega tu TOKEN aqui: 
if "%TOKEN%"=="" goto :token_vacio
if /i "%TOKEN%"=="TU_TOKEN_AQUI" goto :token_vacio

echo.
echo Tu ID numerico de Telegram (para que te reconozca como dueno):
echo   1. Abre Telegram y busca a @userinfobot
echo   2. Escribele cualquier mensaje (por ejemplo: hola)
echo   3. Te dira tu ID, tipo:  123456789
echo      Ese es tu ID. Escribelo aqui.
echo.
set /p OWNER=  Escribe tu ID aqui: 
if "%OWNER%"=="" goto :owner_vacio

REM Guardar los datos en el archivo de configuracion
"runtime\python\python.exe" "src\setup_cli.py" save "%TOKEN%" "%OWNER%" >nul 2>&1
if errorlevel 1 goto :error_guardado

echo.
echo Listo! Tus datos quedaron guardados.
echo Arrancando el bot...
echo.
goto :arrancar

:arrancar
echo Iniciando YT-Remote...
"runtime\python\python.exe" src\main.py
echo.
echo El bot se detuvo.
pause
exit /b 0

:token_vacio
echo.
echo [Error] El TOKEN no puede estar vacio. Volve a intentar.
echo.
goto :configurar

:owner_vacio
echo.
echo [Error] El ID no puede estar vacio. Volve a intentar.
echo.
goto :configurar

:error_guardado
echo.
echo [Error] No se pudieron guardar los datos. Revisa la guia GUIA.txt
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
echo Algo no esta bien instalado en esta carpeta.
echo Vuelve a descargar la version completa desde el repositorio
echo y descomprimela de nuevo. La guia esta en GUIA.txt
echo.
pause
exit /b 1
