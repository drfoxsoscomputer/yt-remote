@echo off
rem Escanea el arbol de trabajo por patrones de credenciales antes de pushear.
rem Salida 0 = limpio, 1 = hay coincidencias (NO pushear).
rem Uso: scan-secrets.bat
setlocal
cd /d "%~dp0.."
python "%CD%\scripts\scan_secrets.py"
exit /b %ERRORLEVEL%