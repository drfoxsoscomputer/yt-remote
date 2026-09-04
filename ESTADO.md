# ESTADO — YT-Remote

> Pagina que cualquier sesion futura lee primero. Si esto no esta actualizado, es un bug mio, no del usuario.

## Que es
Bot de Telegram que controla la reproduccion de YouTube (video en la PC donde corre el programa). Busqueda privada en el chat, roles (admin/dj/user) y thumbnails en los resultados.

## Como correr
1. Todo vive DENTRO de la carpeta (portable de verdad): Python embebido en `runtime\python\`, mpv en `runtime\mpv\`, dependencias en el Python embebido.
2. Token de Telegram en `.env` (NO se sube a GitHub): `TELEGRAM_TOKEN=...`
3. Doble clic en `start.bat` → arranca el bot con el Python embebido.

## Estructura
- `src/main.py` — entrada; sys.path al parent y run_polling.
- `src/bot.py` — YTRemoteBot: comandos play/pause/resume/next/prev/stop/volume/queue/now/adduser/removeuser; wrapper `_require(rol)`; callbacks con thumbnails y `_search_cache`.
- `src/search.py` — is_youtube_link() + search() via yt-dlp; SearchResult.
- `src/player.py` — clase Player, IPC de mpv por named pipe `\\.\pipe\mpv-ytremote`. Usa `--idle=yes` para mantener el pipe vivo.
- `src/queue_manager.py` — QueueManager + QueueItem (deque FIFO).
- `src/config.py` — load_config(); lee token PRIMERO de .env, fallback config.json; resuelve mpv_path relativo al PROJECT_ROOT; error claro si falta token; lee OWNER_ID y ALLOWED_CHAT_ID.
- `src/setup_cli.py` — utilidades para ytremote.bat: is_configured(), write_env(), set/get_allowed_chat_id(). Escribe el .env sin tocar archivos a mano.
- `src/roles.py` — RoleManager (admin/dj/user); has_role por rango; persiste data/roles.json.
- `config.json` — NO sensibles: mpv_path, default_role, max_results; token placeholder.
- `.env` — TOKEN REAL + OWNER_ID + ALLOWED_CHAT_ID (ignorado por git).
- `.env.example` — plantilla segura de configuracion (versionada).
- `ytremote.bat` — UNICO archivo de arranque: si .env vacio muestra formulario (pide token e ID, guiado); si listo, arranca el bot. Mensajes amigables, sin jerga tecnica.
- `GUIA.txt` — guia de usuario en texto plano (como crear bot, conseguir ID, uso, problemas).
- `.gitignore` — ignora .env, venv, data/roles.json, __pycache__, *.log.
- `runtime/` — Python 3.13.9 embebido + mpv portable (SE SUBEN al repo, por decision del usuario: todo incluido).

## Estado actual
- Fases 1-5 completadas y pusheadas. Fase 6 (testing) en curso.
- Python portable embebido + mpv portable dentro de runtime/ (todo en el repo por decision del usuario).
- UNICO ytremote.bat reemplaza a start.bat y setup.bat (eliminados); con formulario de primera configuracion y mensajes amigables.
- Token ahora se lee de .env (seguridad: no subir token real a GitHub).
- OWNER_ID + ALLOWED_CHAT_ID leidos de .env; el dueno queda como admin automaticamente.
- Restriccion de chat: si ALLOWED_CHAT_ID definido solo responde ahi; si no, solo el dueno hasta que configure el grupo.
- Autoconfiguracion: el dueno manda /start la primera vez y el bot guarda ese chat como permitido (implementado en bot.py cmd_start + setup_cli.set_allowed_chat_id; verify: _chat_allowed corretto).
- setup_cli.py verificado (is-configured/save/set-chat + CLI; write_env; set/get allowed_chat_id). HAY que verificar el formulario real en doble clic (no simulable por pipeline).
- BUG .bat resuelto: el patron `for /f` para capturar salida de un comando Python con rutas y comillas NUNCA funciona (parsing de cmd). Solucion robusta: redirigir salida a archivo temp (`> %TEMP%\..`) + `set /p` para leerlo. Ademas el .bat debe ser 100% ASCII (sin acentos/<<>>) o cmd rompe el parsing: escribir sin tildes/n.
- Player IPC verificado contra mpv portable real (fix de --idle=yes).
- Busqueda YouTube verificada con yt-dlp real.
- BUG "pegado en buscando / no reproduce" CORREGIDO: el bot se congelaba porque (1) search() sincrono bloqueaba el event loop de Telegram mientras yt-dlp consultaba, y (2) Player._send_raw abria el named pipe de Windows con open() sincrono y bloqueante, que se colgaba si mpv aun no creaba el pipe. Fix: search() en asyncio.to_thread; Player usa to_thread para abrir/escribir el pipe, espera a que mpv cree el pipe al arrancar (con timeout) y nunca bloquea el loop. Verificado de verdad: mpv arranca, carga una URL de YouTube y reproduce sin cuelgues.
- CAUSA RAIZ adicional del "se queda buscando": con extract_flat=True yt-dlp devuelve thumbnails VACIOS, y cmd_play hacia send_photo(photo="") que se colgaba. Fix: search.py construye la miniatura desde el video ID (https://i.ytimg.com/vi/<id>/hqdefault.jpg) y cmd_play cae a un mensaje de texto con botones si el thumbnail falta o la foto falla (try/except). Verificado: busqueda de 'denicher pool inexplicable' devuelve thumbs validos (HTTP 200).
- BUG REAL del "no veo ni escucho nada": mpv daba "loading failed" al cargar YouTube porque NO encontraba yt-dlp (el unico yt-dlp.exe vive en runtime\python\Scripts, pero el sistema no tiene ninguno y mpv no lo usaba). Fix: player.py _mpv_env() agrega runtime\python\Scripts al PATH del proceso mpv. Verificado de verdad: mpv ahora emite audio-reconfig + video-reconfig + playback-restart (antes: loading failed).
- FIX DEFINITIVO del "no veo ni escucho nada": ahora el bot RESUELVE las URLs de YouTube a streams directos de googlevideo en Python (usando yt-dlp como libreria) ANTES de pasarselas a mpv. mpv ya no necesita resolver YouTube internamente. Funciona con videos DASH (streams separados: video VP9 + audio Opus) via audio-add. Verificado: busqueda + seleccion + resolucion + carga en mpv = reproduce correctamente.
- FIX del bloqueo de YouTube "Sign in to confirm you're not a bot": yt-dlp con el player client web por defecto falla al extraer el stream ("No se pudo resolver el video"). Solucion: resolve_stream_url() fuerza el player client 'android' de yt-dlp, que devuelve un solo stream mp4 (video+audio juntos) y evita el bloqueo. Verificado: ricky martin / rick astley ahora reproducen en mpv.
- UX miniaturas segun pedido del user: el listado de resultados (/play) se muestra SIN miniatura (solo texto + botones titulo/duracion); la miniatura aparece al ELEGIR el video (on_callback envia la foto del elegido + "Reproduciendo: <titulo>").
- /start ahora muestra los comandos disponibles segun el rol del que pregunta (admin ve todo, dj ve controles, user ve lo basico) via help_for_role(). Verificado.
- GUIA.txt actualizada: seccion USO con los comandos agrupados por rol (user / dj+admin / solo admin) y roles/permisos.
- Falta probar el bot en vivo con Telegram (requiere token real en .env).

## Pendientes / ToDo
- [ ] Verificar el formulario real de ytremote.bat en una consola cmd de Windows (pedido de token/ID).
- [ ] Fase 6: testing en vivo con Telegram (token real en .env).
- [ ] Completar/ajustar manual de usuario (GUIA.txt) al nuevo flujo (reordenar pasos segun aprobacion del usuario).
- [ ] Fase 7: push + crear release (zip) del proyecto.
- [ ] Limpiar venv\ viejo local (ya no se usa, pero esta en carpeta; no se sube).

## Decisiones recientes
- Portable de verdad: todo dentro de la carpeta (Python embebido 3.13.9 + mpv). (2026-09-04)
- Todo incluido en el repo (no setup que descargue): el usuario descomprime y ya esta todo. (2026-09-04)
- El token real va SOLO en .env (gitignored), nunca en config.json ni en el repo publico. (2026-09-04)
- El arranque es con start.bat (no .exe) por ahora. (2026-09-04)
- Dueno definido por OWNER_ID en .env (se registra admin solo); whitelist de chat via ALLOWED_CHAT_ID; /start imprime el chat_id. (2026-09-04)

## Tests / verificacion
- Player IPC: mpv arranca en idle, acepta set_volume/get_property, se cierra. Verificado.
- Busqueda: yt-dlp devuelve resultados reales; is_youtube_link() correcto.
- Config: token/OWNER_ID/ALLOWED_CHAT_ID desde .env; .env ignorado, .env.example versionado (git check-ignore).
- YTRemoteBot instancia sin red y registra al dueno (OWNER_ID) como admin (verificado).
- Sintaxis OK en todos los .py; dependencias importan en el Python embebido.
- resolve_stream_url(): busca + resuelve + carga en mpv con streams separados (video VP9 + audio Opus via audio-add). Verificado: mpv reproduce con exito.
