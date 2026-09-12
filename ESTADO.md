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
- `build.py` — PyInstaller onedir build; `--add-data` runtimes WebView2 desde `assets/webview2/runtimes` (vendeados, sin rutas de la máquina); `--manifest` dpiAware; `--icon` ytremote.ico; `--splash` QUITADO. El ZIP (`--zip`) lo delega en `release_zip.py`
- `release_zip.py` — Módulo compartido que arma el ZIP portable para ambos builders; consume las reglas de `release_rules.json` (única fuente: versión, allowlist de site-packages, `include_top_files`, `folders`); modo onedir (exe + `_internal`) o onefile (`exe_file`)
- `release_rules.json` — Versión 1.0.0; allowlist de runtime recortada a lo que el bot importa de verdad (telegram+httpx+apscheduler, etc.); sin basura de desarrollo
- `dist/ytremote/ytremote.exe` — Ejecutable (18.0 MB, onedir, sin splash)
- `ytremote.manifest` — `dpiAware=true` + Microsoft Windows Common Controls 6.0 (evita re-escala ~2.5s)
- `assets/logo-ytremote.png` — Logo oficial (fuente para `static/img/logo-ytremote.png` y branding)
- `assets/webview2/runtimes/` — WebView2Loader.dll vendeados (win-x64/x86/arm64) para el bundle

## Estado actual
Pack 12/sep/2026 tarde (sin commitear): plan de 7 fixes tras reportes del usuario ("No se pudo iniciar el bot. Revisa token e ID.", skeleton no visible, solo Ctrl+V pega).

Diagnóstico: `dist\ytremote\ytremote.exe` directo NO arrancaba el bot porque faltaban `runtime\`, `src\` y `config.json` junto al exe; y en el ZIP, **causa raíz del "conectado" falso**: `release_rules.json` no incluía `security.py` en la allowlist de `src/` → `config.py` moría con `ModuleNotFoundError`; y el ZIP iba sin `data/` → `sqlite3.OperationalError: unable to open database file` al crear `roles.db`. Ambas mataban el bot a los ~2s, después del chequeo de 0.6s.

Fix aplicado: (1) `bot_process.py` ahora valida existencia de python/runtime, escribe `data/bot.log` (stdout+stderr del subproceso vía hilo vigía) y expone `last_error()`; (2) token se valida con **getMe de Telegram ANTES** de guardar sesión/arrancar → respuesta honesta "Token inválido: {motivo}" (nunca más "conectado" falso); (3) `release_rules.json` suma `security.py`; (4) `roles.py` crea `data/` (mkdir); (5) `release_zip.py` SIEMPRE embebe `data/`; (6) `build.py --zip` ahora **materializa el dist**: extrae el zip EN `dist\ytremote` → dist queda portable completo (runtime+src+config junto al exe); (7) skeleton servido como `data:` URL base64 con logo embebido (el `file://` se rompía en WebView2); (8) menú contextual Pegar/Copiar propio (WebView2 no tiene menú nativo) + limpieza de caracteres invisibles (zero-width) que invalidaban el token al pegar con Ctrl+V.

Verificado: POST /api/connect con token inválido → 400 `Token inválido: Unauthorized` y NO guarda sesión; bot arrancado directo del bundle → proceso vivo + `data/roles.db` creado; exe real sirve `/launcher` (200), `/static/skeleton.html` (200), `/api/session` (200). Falta la prueba con token real (depende del usuario).

Los fixes del pack anterior (12/sep mañana: skeleton instantáneo, `/buscar` diagnóstico, `settings.enc`) quedaron en `b605228` + `f9a3e0e`, aún sin push.

El crash "Main window failed to start" sigue resuelto (la ventana ya no llama `show()`/`hide()` prematuramente; auto-conexión en `webview.start(func)`).

UI aprobada y aplicada: paleta Telegram (azul #229ed9 primario, fantasma secundario, rojo #e53935 destructivo, verde #31c471 conectado; nada de dorado), botones en línea (Conectar+Salir y Detener+Cerrar sesión en fila), logo `logo-ytremote.png` en el encabezado del formulario e icono del exe desde `ytremote.ico` en ambas builds (`build.py` y `build_exe.py`).

Ventana subida a 420x640 y centrada en el **área de trabajo** (SPI_GETWORKAREA: pantalla menos barra de inicio) horizontal y verticalmente, con corrección DPI. Formulario compactado y CSS anti-scroll (`overflow:hidden` en html/body + `#form-container` con scroll interno): el logo nunca se corta ni aparece barra de scroll.

Commiteado y pusheado a `origin/main` como v1.0.0 (`6b67efc`): `README.md` y `GUIA.txt` actualizados al flujo del exe (ventana del launcher, `session.enc`, tray, WebView2). `.gitignore` ajustado (se versiona `static/`; se ignoran `dist`/`build`/`node_modules`/`session.enc` y volcados de prueba).

Pipeline de release v1.0.0 EN MARCHA: `release_zip.py` extrae la lógica del ZIP a un módulo compartido (`build.py --zip` y `build_exe.py` usan la misma fuente). `build.py --zip` ahora arma un ZIP LEAN: `ytremote.exe` + `_internal` a la raíz, y junto a ellos `runtime/` (site-packages solo con la allowlist), `src/` del bot (10 módulos), `config.json`, `README.md` y `GUIA.txt`. Allowlist validada contra imports reales (se recortaron `colorama`, `iniconfig`, `packaging`; se conservaron `apscheduler`/`tzdata`/`tzlocal` porque `bot.py` usa `job_queue.run_repeating`). `data/roles.db` dejó de versionarse (trae IDs reales de Telegram).

**✅ RELEASE v1.0.0 PUBLICADO** (11/sep/2026): `yt-remote-1.0.0-portable.zip` = 92.0 MB (3.461 entradas; los viejos 0.5.0 pesaban hasta 132 MB con pip/pytest adentro). Build `python build.py --zip` (commit `9182276`, push `6b67efc..9182276`). Release: https://github.com/drfoxsoscomputer/yt-remote/releases/tag/v1.0.0

## Pendientes / ToDo
- [ ] Emparejar con token REAL desde el exe/dist nuevo: Conectar → getMe aprueba → bot spawn desde runtime portable → `data/bot.log` sin errores → responder en Telegram + tray
- [ ] Pull/verificar en máquina limpia: skeleton visible, launcher, `/buscar` diagnóstico, arranque sin `.env`
- [ ] Push de packs (12/sep mañana + tarde) a origin/main — pendiente confirmación explícita
- [ ] Sin commitear (trabajo previo, fuera del plan): `src/test_members.py` (fix de indentación), `src/launcher.py` + `src/ctk_theme.json` (prototipo CustomTkinter) — decidir si van o se descartan

## Decisiones recientes
- 12/sep/2026 (tarde): "Conectado" honesto => `bot_process.validate_token()` hace getMe contra Telegram ANTES de guardar `session.enc`/arrancar el bot, y launcher responde el motivo real de `last_error()`. El "conectado" falso del usuario era doble caída: (a) ZIP sin `security.py` (allowlist `release_rules.json`) → `ModuleNotFoundError` en `config.py`; (b) ZIP sin carpeta `data/` → `sqlite3.OperationalError` al crear `roles.db` en `RoleManager._load`. Fixes: allowlist suma `security.py`; `release_zip.py` embebe SIEMPRE `data/`; `roles.py` y `security.save_bot_data` crean `data/` con mkdir.
- 12/sep/2026 (tarde): `build.py --zip` ahora materializa el dist: `materialize_portable_dist()` extrae el zip EN `dist\ytremote`; así `dist\ytremote` nunca más queda sin `runtime\python\python.exe` (causa del "No se pudo iniciar el bot" original al correr el exe a secas). Además `bot_process.start()` valida existencia de python + main.py con errores concretos y vuelca la salida del subproceso a `data/bot.log` (hilo vigía con timeout 30s) — con `--windowed` eso antes era invisible.
- 12/sep/2026 (tarde): Skeleton vía `data:` URL (base64) en vez de `file://` — WebView2 no renderizaba fiable el `file://` y rompía las rutas relativas; `_data_uri_skeleton()` embebe el HTML + logo (`img/logo-ytremote.png` → base64) con fallback al `http://127.0.0.1:8081/static/skeleton.html`.
- 12/sep/2026 (tarde): Menú contextual propio en el launcher (WebView2 no crea menú nativo: solo Ctrl+V). `LauncherApi.pegar()` lee el portapapeles vía ctypes (OpenClipboard/CF_UNICODETEXT) y el JS limpia caracteres cero-ancho (zero-width) que invalidaban el token al pegar.
- 12/sep/2026: Launcher con skeleton instantáneo (commit `b605228`): la ventana nace de inmediato con `static/skeleton.html` (file://, CSS inline + shimmer, paleta #1a1a2e) en vez de esperar que Flask arranque. `main_launcher.py` reescrito: `_skeleton_url()`, `_cargar_interfaz_real()` (espera Flask hasta 10s en hilo daemon → `load_url('/launcher')` → auto-conexión si hay sesión → minimiza a tray). Flask arranca con `webview.start(_post_start)`.
- 12/sep/2026: `/buscar` con diagnóstico real (raíz: yt-dlp con client web era bloqueado INTERMITENTE por "Sign in to confirm you're not a bot"; el error se tragaba en `search()`). Fix: `search.py` prueba primero `player_client visionos` (el mismo que esquiva el bloqueo en `resolve_stream_url`) con fallback al default, y expone la causa en `last_search_error()`; `bot.py` añade `_SEARCH_TIMEOUT = 30.0` (asyncio.wait_for) y responde el motivo real por Telegram en vez del genérico "No encontre resultados."
- 12/sep/2026: Fin del `.env` para ALLOWED_CHAT_ID (lo pedía el usuario; era texto plano). Ahora `data/settings.enc` con DPAPI (mismo mecanismo que session.enc): `security.py` gana `load/save_bot_data`, `get/set_allowed_chat_id`, `migrate_dotenv_allowed_chat()` (mueve el valor viejo al arranque y borra el `.env` si queda vacío; conserva TELEGRAM_TOKEN/OWNER_ID si el flujo manual legacy los usa). `config.py` resuelve env → settings.enc → `.env` legacy; `bot.py` `/start` escribe encriptado; `setup_cli.py` delega al store. `.env` ya no se crea en el flujo del launcher.
- 12/sep/2026: `launcher_web.py` servía `/launcher` y `/static/` solo empaquetado (Flask resolvía carpetas relativas a `src/` en dev → 404/500). Fix: `RES_DIR` absoluto (módulo `_internal` si frozen, raíz del repo en dev) para `template_folder`/`static_folder`; dev y bundle sirven idéntico.

## Decisiones recientes
- 11/sep/2026: Crash "Main window failed to start" = `show()` prematuro. Fix: mover auto-conexión a `webview.start(func)` y quitar `show()` del flujo sin sesión en `src/main_launcher.py`. Sin sesión la ventana nace visible (`hidden=False`); con sesión el bot arranca post-GUI y la ventana se oculta al tray.
- 11/sep/2026: Build portable: runtimes WebView2 vendedados en `assets/webview2/runtimes/`; `build.py` apunta ahí (antes hardcodeaba `D:\laragon\...\webview\lib\runtimes`, lo que rompía el exe sin WebView2Loader.dll fuera de esa máquina).
- 11/sep/2026: Eliminada la doble auto-conexión: Python arranca el bot (sesión), `launcher.js` solo refleja `/api/status` (antes hacía POST /api/connect con token truncado).
- 11/sep/2026: Paleta Telegram en el launcher. Se eliminó el acento dorado (--oro) que molestaba al usuario; Conectar pasó de rojo YouTube a azul Telegram #229ed9 y Salir/Detener a botón fantasma (para no quedar iguales en fila). Logo `assets/logo-ytremote.png` copiado a `static/img/` (Flask sirve `static/` al lado del exe; la ruta `assets` no se sirve). `build_exe.py` ahora asigna `ytremote.ico` al exe onefile (antes `icon=''`).
- 11/sep/2026: Logo se cortaba por overflow: ventana 420x520 no alcanzaba (~636px de contenido). Fix: ventana → 420x640, centrado contra el área de trabajo con `SystemParametersInfoW(SPI_GETWORKAREA)` (antes `GetSystemMetrics` de pantalla completa, que incluía la barra de inicio), y CSS anti-scroll (`html,body{overflow:hidden}` + `#form-container` scrollea interno). Medido en 100% DPI: T=96, B=736, barra en 834.
- 11/sep/2026: UI aprobada por el usuario y primer release del launcher (v1.0.0). Documentación actualizada al flujo real del exe: `README.md` y `GUIA.txt` descartan el wizard `ytremote.bat`/`.env` y explican la ventana del launcher (token + ID admin + horas kick, botones Conectar/Salir/Detener/Cerrar sesión, `session.enc`, tray, WebView2). Convención: artefactos escritos en español neutro (tono Venezuela).
- 11/sep/2026: Pipeline de ZIP portable unificado en `release_zip.py` (release_rules.json = única fuente para `build.py --zip` y `build_exe.py`). `build.py --zip` arma ZIP lean (exe + `_internal` + runtime allowlist + `src` del bot + `config.json` + docs). Allowlist recortada (fuera `colorama`/`iniconfig`/`packaging`; dentro `apscheduler`/`tzdata`/`tzlocal` por `job_queue`). `data/roles.db` (IDs reales de Telegram) sale del versionado.
- 11/sep/2026: Logo `static/img/logo-ytremote.png` agregado centrado al inicio del README (el pedido del usuario quedó pendiente en la tanda anterior por mi error al archivarlo como "pendiente" en vez de ejecutarlo; se corrigió en esta edición).

## Tests / verificación
- ✅ 12/sep tarde: POST `/api/connect` con token inválido → 400 `Token inválido: Unauthorized`, NO guarda sesión (antes decía "conectado")
- ✅ 12/sep tarde: bot arrancado directo del bundle (`dist\runtime\python\python.exe src\main.py`): con `security.py` y `data/` corregidos → proceso vivo a los 10s + `data/roles.db` creado
- ✅ 12/sep tarde: exe real sirve `/launcher` (200, 6544B), `/static/skeleton.html` (200, 2740B), `/api/session` (200); `dist\ytremote` portable completo (runtime/src/config.json presentes)
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