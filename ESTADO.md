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
- `src/main_launcher.py` — Entry point: mutex instancia única, check WebView2, hilo Flask daemon, `webview.create_window`, `LauncherApi`, events.closing → _on_closing, `webview.start()`, `_apagar_todo` en finally
- `src/launcher_web.py` — Flask API routes: `/launcher` (GET), `/api/session` (GET), `/api/connect` (POST), `/api/status` (GET), `/shutdown` (POST)
- `templates/launcher.html` — Formulario 420×520: token, admin ID, horas kick, botones Conectar/Salir; Tailwind + JS `pywebview.api`
- `static/css/tailwind.css` + `tema.css` — Colores YT-Remote: #1a1a2e, #ff0000, #4a90e2, etc.
- `static/js/launcher.js` — Form submit, toggle eye, `pywebview.api` calls, auto-connect, error handling
- `src/bot_process.py` — BotProcess class (start/stop/is_running), reutilizable
- `build.py` — PyInstaller onedir build; `--add-data` runtimes WebView2; `--manifest` dpiAware; `--icon` ytremote.ico; `--splash` QUITADO
- `dist/ytremote/ytremote.exe` — Ejecutable (18.0 MB, onedir, sin splash)
- `ytremote.manifest` — `dpiAware=true` + Microsoft Windows Common Controls 6.0 (evita re-escala ~2.5s)
- `assets/photo_2026-09-11_01-28-39.jpg` — Logo robot (origen .ico 16/32/48/64/256)

## Estado actual
WebView2 integration fix aplicado: `--add-data` incluye las carpetas `runtimes` de WebView2 en el bundle PyInstaller, lo que permite que `interop_dll_path()` encuentre `WebView2Loader.dll` al ejecutarse desde `.exe`. El exe levanta Flask en localhost:8081, sirve el formulario Tailwind y la ventana WebView2 se muestra sin errores. `hidden=False` en `create_window` (cambiar de `hidden=True` tras aprobación del usuario).

## Pendientes / ToDo
- [x] Fix WebView2 "Main window failed to start" — Añadido `--add-data` runtimes en build.py
- [ ] Testeo exhaustivo: formulario Conectar → bot arranca + minimizar tray, cerrar X → diálogo nativo (Sí=tray/No=salir)
- [ ] Verificar release rules.json para onedir + nueva estructura
- [ ] Empaquetado release ZIP portable (`python build.py --zip`)

## Decisiones recientes
- 11/sep/2026: Arquitectura pywebview + Flask + Tailwind aprobada tras revisar AlbionHelper. Usuario rechazó rediseño CTk 2026 y versión ttl gris restaurada.
- 11/sep/2026: Splash nativo PyInstaller `--splash` quitado por conflicto con WebView2 ("Main window failed to start"). Splash ahora corre solo en HTML/JS si es necesario.
- 11/sep/2026: WebView2 runtime v133.0.3065.69 instalado; fix técnico: `--add-data` rutas `webview/lib/runtimes` en build.py para que los DLLs sean accesibles en modo frozen.

## Tests / verificación
- Levantar exe: `dist\ytremote\ytremote.exe` → Flask en http://127.0.0.1:8081, ventana WebView2 abierta, formulario HTML servido
- Rutas API verificadas: `GET /launcher` 200 OK, `GET /api/status` retorna `{"bot_running": false}`
- El fix consiste en añadir `("D:\laragon\bin\python\python-3.13\Lib\site-packages\webview\lib\runtimes", "webview/lib/runtimes")` al array `DATAS` en `build.py`