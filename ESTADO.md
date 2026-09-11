# ESTADO — YT-Remote

> Proyecto de control rem YouTube con ventana nativa y interfaz web-based.

## Qué hace
Launcher multiplataforma que muestra un formulario HTML servido por Flask (puerto 8081) en una ventana nativa pywebview + WebView2. El usuario configura token del bot, ID de admin y horas kick, y la app minimiza al sistema tray cuando el bot está conectado.

## Cómo correr

```powershell
# Modo desarrollo (Flask + HTML directo):
python -m src.launcher_web

# Compilado a .exe (onedir, sin splash):
dist\ytremote\ytremote.exe
```

## Estructura
- `src/main_launcher.py` — Entry point: mutex instancia única, check WebView2, hilo Flask daemon, `webview.create_window` (420x640, centrada en el área de trabajo), `LauncherApi`, events.closing → _on_closing, `webview.start()`, `_apagar_todo` en finally
- `src/launcher_web.py` — Flask API routes: `/launcher` (GET), `/api/session` (GET), `/api/connect` (POST), `/api/status` (GET), `/shutdown` (POST)
- `templates/launcher.html` — Formulario 420×520: token, admin ID, horas kick, botones en fila (Conectar azul / Salir fantasma / Detener / Cerrar sesión rojo); logo `static/img/logo-ytremote.png`; Tailwind + JS `pywebview.api`
- `static/css/tailwind.css` + `tema.css` — Paleta Telegram (#229ed9 azul, #31c471 verde, #e53935 rojo; sin dorado); recompilar con `npx tailwindcss -i static/css/input.css -o static/css/tailwind.css --minify`
- `static/js/launcher.js` — Form submit, toggle eye, `pywebview.api` calls, refleja `/api/status`, error handling
- `static/img/logo-ytremote.png` — Copia del logo (servida por Flask; entra al bundle vía `static`)
- `src/bot_process.py` — BotProcess class (start/stop/is_running), reutilizable
- `build.py` — PyInstaller onedir build; `--add-data` runtimes WebView2 desde `assets/webview2/runtimes` (vendeados, sin rutas de la máquina); `--manifest` dpiAware; `--icon` ytremote.ico; `--splash` QUITADO
- `dist/ytremote/ytremote.exe` — Ejecutable (18.0 MB, onedir, sin splash)
- `ytremote.manifest` — `dpiAware=true` + Microsoft Windows Common Controls 6.0 (evita re-escala ~2.5s)
- `assets/logo-ytremote.png` — Logo oficial (fuente para `static/img/logo-ytremote.png` y branding)
- `assets/webview2/runtimes/` — WebView2Loader.dll vendeados (win-x64/x86/arm64) para el bundle

## Estado actual
Crash "Main window failed to start" resuelto de raíz: el launcher ya NO llama a `show()`/`hide()` antes de que la GUI arranque. La auto-conexión con sesión se movió a `webview.start(func)` (se ejecuta con la GUI viva) y `start()` quedó en `try/except` con MessageBox del error real. `launcher.js` dejó de re-conectar (solo consulta `/api/status`), eliminando el doble start. Los runtimes de WebView2 se vendieron en `assets/webview2/runtimes` y `build.py` ya no depende de rutas de Laragon (build portable).

UI aprobada y aplicada: paleta Telegram (azul #229ed9 primario, fantasma secundario, rojo #e53935 destructivo, verde #31c471 conectado; nada de dorado), botones en línea (Conectar+Salir y Detener+Cerrar sesión en fila), logo `logo-ytremote.png` en el encabezado del formulario e icono del exe desde `ytremote.ico` en ambas builds (`build.py` y `build_exe.py`).

Ventana subida a 420x640 y centrada en el **área de trabajo** (SPI_GETWORKAREA: pantalla menos barra de inicio) horizontal y verticalmente, con corrección DPI. Formulario compactado y CSS anti-scroll (`overflow:hidden` en html/body + `#form-container` con scroll interno): el logo nunca se corta ni aparece barra de scroll.

## Pendientes / ToDo
- [ ] Emparejar con token real: verificar Conectar → bot corre + tray, cerrar X → diálogo nativo (Sí=tray/No=salir)
- [ ] Definir layout del dist onedir: el bot en modo frozen necesita `runtime\python`, `src\main.py` y `config.json` al lado del exe para auto-conectar (hoy no están en `dist\ytremote`)
- [ ] Revisar release_rules.json / zips existentes (0.4.0 y 0.5.0) vs estructura onedir nueva
- [ ] Empaquetado release ZIP portable (`python build.py --zip`)

## Decisiones recientes
- 11/sep/2026: Crash "Main window failed to start" = `show()` prematuro. Fix: mover auto-conexión a `webview.start(func)` y quitar `show()` del flujo sin sesión en `src/main_launcher.py`. Sin sesión la ventana nace visible (`hidden=False`); con sesión el bot arranca post-GUI y la ventana se oculta al tray.
- 11/sep/2026: Build portable: runtimes WebView2 vendedados en `assets/webview2/runtimes/`; `build.py` apunta ahí (antes hardcodeaba `D:\laragon\...\webview\lib\runtimes`, lo que rompía el exe sin WebView2Loader.dll fuera de esa máquina).
- 11/sep/2026: Eliminada la doble auto-conexión: Python arranca el bot (sesión), `launcher.js` solo refleja `/api/status` (antes hacía POST /api/connect con token truncado).
- 11/sep/2026: Paleta Telegram en el launcher. Se eliminó el acento dorado (--oro) que molestaba al usuario; Conectar pasó de rojo YouTube a azul Telegram #229ed9 y Salir/Detener a botón fantasma (para no quedar iguales en fila). Logo `assets/logo-ytremote.png` copiado a `static/img/` (Flask sirve `static/` al lado del exe; la ruta `assets` no se sirve). `build_exe.py` ahora asigna `ytremote.ico` al exe onefile (antes `icon=''`).
- 11/sep/2026: Logo se cortaba por overflow: ventana 420x520 no alcanzaba (~636px de contenido). Fix: ventana → 420x640, centrado contra el área de trabajo con `SystemParametersInfoW(SPI_GETWORKAREA)` (antes `GetSystemMetrics` de pantalla completa, que incluía la barra de inicio), y CSS anti-scroll (`html,body{overflow:hidden}` + `#form-container` scrollea interno). Medido en 100% DPI: T=96, B=736, barra en 834.
- 11/sep/2026: UI aprobada por el usuario y primer release del launcher (v1.0.0). Documentación actualizada al flujo real del exe: `README.md` y `GUIA.txt` descartan el wizard `ytremote.bat`/`.env` y explican la ventana del launcher (token + ID admin + horas kick, botones Conectar/Salir/Detener/Cerrar sesión, `session.enc`, tray, WebView2). Convención: artefactos escritos en español neutro (tono Venezuela).

## Tests / verificación
- ✅ Rebuild `python build.py` (PyInstaller 6.22, Python 3.13) → `dist\ytremote\ytremote.exe` con `assets/webview2/runtimes` en el bundle (win-x64/x86/arm64)
- ✅ Sin sesión (camino que crasheaba): exe levanta Flask, proceso vivo >25s, `/api/status` `{"bot_running":false}`, formulario servido — sin crash
- ✅ Con sesión: exe detecta `session.enc`, spawn del bot (`runtime\python python.exe src\main.py`), `bot_running=true`, ventana oculta + tray, exe estable — sin crash
- ✅ Errores de arranque ahora saltan MessageBox (antes: `--windowed` sin consola = errores invisibles)
- ✅ `runtime\python` (portable) + `src\main.py` + `config.json` copiados al lado del exe durante la prueba y retirados al final (dist quedó limpio)
- ✅ UI: `/launcher`, `/static/img/logo-ytremote.png` (PNG válido), `/static/css/tailwind.css` y `/static/js/launcher.js` = 200 en el exe nuevo; CSS servido sin `oro`, con azul #229ed9, `flex-1` y `gap-3`; HTML sin `titulo-oro`/`ring-oro` y con `<img>` del logo
- ✅ Bundle: `_internal\static\img\logo-ytremote.png`, `templates`, `ytremote.ico`, runtimes win-x64/x86/arm64 presentes
- ✅ Aprobado visualmente por el usuario (11/sep/2026). UI aceptada tal como quedó
- ✅ Tamaño/centrado: ventana 420x640, L=558 (centro horizontal de 1536), T=96 (centro vertical del área de trabajo 834), B=736 < 834 = no pisa la barra de inicio; proceso estable, HTTP 200
- ✅ Logo completo + sin scroll: contenido ~600px < ventana 640px (antes ~636px en 520px = overflow/corte); red de seguridad CSS activa en el bundle