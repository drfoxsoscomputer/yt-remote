# ESTADO — YT-Remote

> Pagina que cualquier sesion futura lee primero. Si esto no esta actualizado, es un bug mio, no del usuario.

## Que es
Bot de Telegram que controla la reproduccion de YouTube en la TV (PC conectado por HDMI). Busqueda privada en el chat, roles (admin/dj/user) y thumbnails en los resultados.

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
- `src/config.py` — load_config(); lee token PRIMERO de .env, fallback config.json; resuelve mpv_path relativo al PROJECT_ROOT; error claro si falta token.
- `src/roles.py` — RoleManager (admin/dj/user); has_role por rango; persiste data/roles.json.
- `config.json` — NO sensibles: mpv_path, default_role, max_results; token placeholder.
- `.env` — TOKEN REAL (ignorado por git).
- `setup.bat` / `start.bat` — verifican Python y mpv embebidos; arrancan con runtime\python\python.exe.
- `.gitignore` — ignora .env, venv, data/roles.json, __pycache__, *.log.
- `runtime/` — Python 3.13.9 embebido + mpv portable (SE SUBEN al repo, por decision del usuario: todo incluido).

## Estado actual
- Fases 1-5 completadas y pusheadas. Fase 6 (testing) en curso.
- Python portable embebido + mpv portable dentro de runtime/ (todo en el repo por decision del usuario).
- start.bat y setup.bat reescritos para usar el Python embebido (sin depender del sistema ni de venv).
- Token ahora se lee de .env (seguridad: no subir token real a GitHub).
- Player IPC verificado contra mpv portable real (fix de --idle=yes).
- Busqueda YouTube verificada con yt-dlp real.
- Falta probar el bot en vivo con Telegram (requiere token real en .env).

## Pendientes / ToDo
- [ ] Fase 6: testing en vivo con Telegram (token real en .env).
- [ ] Restriccion de chat/whitelist (solo funcione en grupo privado del admin) — pendiente de decidir e implementar.
- [ ] Fase 7: push final.
- [ ] Manual de usuario (decision: portable de verdad, todo incluido en el repo).
- [ ] Limpiar venv\ viejo local (ya no se usa, pero esta en carpeta; no se sube).

## Decisiones recientes
- Portable de verdad: todo dentro de la carpeta (Python embebido 3.13.9 + mpv). (2026-09-04)
- Todo incluido en el repo (no setup que descargue): el usuario descomprime y ya esta todo. (2026-09-04)
- El token real va SOLO en .env (gitignored), nunca en config.json ni en el repo publico. (2026-09-04)
- El arranque es con start.bat (no .exe) por ahora. (2026-09-04)

## Tests / verificacion
- Player IPC: mpv arranca en idle, acepta set_volume/get_property, se cierra. Verificado.
- Busqueda: yt-dlp devuelve resultados reales; is_youtube_link() correcto.
- Config: token desde .env; error claro sin token; .env ignorado por git (git check-ignore).
- Dependencias importan en el Python embebido.
