# ESTADO — YT-Remote

> Pagina que cualquier sesion futura lee primero. Si esto no esta actualizado, es un bug mio, no del usuario.

**Version: v0.2.0** (2026-09-06)

## Que es
Bot de Telegram que controla la reproduccion de YouTube (video en la PC donde corre el programa). Busqueda por chat, tarjeta de control con botones, radio automatica por artista, roles (admin/dj/user).

## Como correr
1. Todo vive DENTRO de la carpeta (portable de verdad): Python embebido en `runtime\python\`, mpv en `runtime\mpv\`, dependencias en el Python embebido.
2. Descargar el ZIP desde https://github.com/drfoxsoscomputer/yt-remote/releases (no requiere git, no requiere Python instalado).
3. Doble clic en `ytremote.bat` → la primera vez te guia para configurar el token y tu ID de Telegram (se guardan en `.env` que NO se sube a GitHub).
4. Dejar la ventana abierta. En Telegram, agregar el bot a un grupo y escribir `/start`.

## Estructura
- `src/main.py` — entrada; sys.path al parent y run_polling.
- `src/bot.py` — YTRemoteBot: comandos `/buscar`, `/lista`, `/now`, `/pause`, `/resume`, `/next`, `/prev`, `/stop`, `/volume`, `/adduser`, `/removeuser`; wrapper `_require(rol)`; tarjeta persistente con botones; callbacks con thumbnails y `_search_cache`.
- `src/search.py` — `is_youtube_link()` + `search()` via yt-dlp; `SearchResult`.
- `src/player.py` — clase Player, IPC de mpv por named pipe `\\.\pipe\mpv-ytremote`. Usa `--idle=yes` para mantener el pipe vivo.
- `src/queue_manager.py` — QueueManager + QueueItem (deque FIFO + historico anti-ping-pong maxlen 500).
- `src/config.py` — `load_config()`; lee token PRIMERO de `.env`, fallback `config.json`; resuelve `mpv_path` relativo al PROJECT_ROOT; error claro si falta token; lee `OWNER_ID` y `ALLOWED_CHAT_ID`.
- `src/setup_cli.py` — utilidades para `ytremote.bat`: `is_configured()`, `write_env()`, `set/get_allowed_chat_id()`, y el comando `launch` (wizard interactivo + arranque del bot).
- `config.json` — NO sensibles: `mpv_path`, `default_role`, `max_results`; token placeholder.
- `.env` — TOKEN REAL + `OWNER_ID` + `ALLOWED_CHAT_ID` (ignorado por git).
- `.env.example` — plantilla segura de configuracion (versionada).
- `ytremote.bat` — lanzador minimo ASCII estilo Albion (4 lineas, sin BOM/acentos/chcp): `title` + llamada a `setup_cli.py launch` + pause. Todo el texto con acentos/ñ vive en Python.
- `GUIA.txt` — guia de usuario en texto plano (como crear bot, conseguir ID, uso, problemas).
- `README.md` — guia rapida con descarga, comandos, roles.
- `.gitignore` — ignora `.env`, `venv`, `data/*.json`, `__pycache__`, `*.log`, `bin/`, `static/`, `yt-remote-*.zip`.
- `runtime/` — Python 3.13.9 embebido + mpv portable (SE SUBEN al repo, por decision del usuario: todo incluido).
- `yt-remote-v0.2.0-portable.zip` — binario portable adjunto al release v0.2.0 en GitHub.

## Estado actual
**v0.2.0 publicado en https://github.com/drfoxsoscomputer/yt-remote/releases/tag/v0.2.0**
ZIP portable de 48 MB adjunto al release. Quien clone o descargue el ZIP no necesita instalar Python ni mpv.

### Features de v0.2.0 (sobre v0.1.0)
- **Pila de navegacion prev/next en modo radio** (`b44c1d7`): dos stacks (`_nav_back` y `_nav_forward`) registran el orden; permite ir y volver entre temas escuchados.
- **Historial de radio 20→500 + filtro anti-duplicado** (`7697faa`): `_MAX_HISTORY=500` y `_pick_next_candidate` filtra URLs ya reproducidas para evitar loops.
- **Feedback "Cargando..."** al tocar boton de la tarjeta (`8f41528`): `query.answer("Cargando...")` al inicio de `_on_control` para que el usuario vea respuesta inmediata.
- **Retry `send_photo` con fallback** (`d3aab54`): si la foto falla cae a `send_message` sin romper.
- **/buscar unificado** (`cb492b4`): con guion (artista - cancion), sin guion (solo artista), o link directo. Sin texto: muestra uso.
- **Documentacion al dia** (`407d94f`, `b5cc29a`, `138ec26`): GUIA.txt y README.md reescritos con artistas cristianos hispanos como ejemplo (Marcos Witt, Jesus Adrian Romero), mencion explicita de descarga como ZIP portable.
- **Limpieza de basura** (`a181eb8`, `e5af788`): `bin/cloudflared.exe` y `static/` eliminados del historial con `git filter-repo`; `bin/`, `static/`, `yt-remote-*.zip` agregados a `.gitignore`.

### Detalles de diseno vigentes
- **Tarjeta persistente con botones**: edita el MISMO mensaje con `[⏮ ▶/⏸ ⏭ ⏹]` + `[🔊−10 🔊+10 📋]`. Callbacks `ctl:prev|pp|next|stop|vol-10|vol+10|lista` despachados por `_on_control`.
- **Radio por artista ancla**: `_radio_artist` se captura en el primer `/buscar`; `/next` y el auto-advance usan ese ancla para que la radio no derive.
- **Cache "mesonero"**: cache en RAM de URLs de YouTube a streams directos googlevideo, resolucion serial (no paralelizar para evitar rate limit).
- **Prefetch 2 fases**: decide candidato al reproducir (Fase A) + resuelve el stream de inmediato (Fase B) = cero silencio.
- **Playlist fija con cursor**: `/lista` muestra 25 temas con `[▶️]` en el actual; `/lista N` mueve el cursor; `/next` y `/prev` con wrap.
- **Hardening**: drop_pending_updates en run_polling, vigilante de red con `_on_bot_error` + `_net_watch_job` (15s), singleton lock en puerto 47631, retry de `send_photo`.

## Pendientes / ToDo
- [x] **Tests `test_card.py`**: 22 errores LSP corregidos. py_compile OK + tests OK.
- [x] **Manejo de errores de red en `_pick_next_candidate`**: flag de error + `_radio_over_message(por_error)` sugiere /lista en fallos de red.
- [x] **Bounds check visible de /volume**: valida 0-100 y avisa en cliente.
- [x] **Pruebas en vivo con Telegram**: 15 tests humanos OK.
- [x] **`requirements.txt`**: eliminado (todo vive en runtime/).
- [x] **Menu de comandos**: `/now`, `/pause`, `/resume` fuera del menu, solo admin.
- [ ] **Comando `/skipartist`**: descartado (no tiene solucion buena — cambiar de artista requiere /buscar nuevo).
- [ ] **Botones de la tarjeta**: verificar rol dj/admin en los botones de control (▶/⏸ ⏮ ⏭ ⏹) para users normales. Hoy un user puede tocarlos aunque no tenga rol.

## Decisiones recientes
- **v0.2.0 release con ZIP portable** (2026-09-06): el usuario descarga el ZIP desde GitHub Releases, descomprime y doble clic. Sin git, sin Python, sin nada instalado. Numero de version siguiendo semver: bump minor (0.1.0 → 0.2.0) por features nuevos (no fixes).
- **Semver aplicado**: 0.1.0 = primera version publica; 0.2.0 = features (pila de navegacion, retry, radio mejorada); futuros 0.2.x = bugfixes; 0.3.0 = breaking change o feature mayor.
- **Limpieza del historial con git filter-repo** (2026-09-06): `bin/cloudflared.exe` (52MB) y `static/` eliminados para que clones frescos no descarguen basura. Force-push autorizado.
- **Cambio a artista cristiano hispano en ejemplos** (2026-09-06): el README usaba artistas menos conocidos; ahora usa Marcos Witt y Jesus Adrian Romero.
- **Numeracion de version x convencion** (2026-09-06): usuario pidio seguir semver, no el "build N" anterior.

## Tests / verificacion
- Player IPC: mpv arranca en idle, acepta set_volume/get_property, se cierra. Verificado.
- Busqueda: yt-dlp devuelve resultados reales; `is_youtube_link()` correcto.
- Config: token/OWNER_ID/ALLOWED_CHAT_ID desde `.env`; `.env` ignorado, `.env.example` versionado (git check-ignore).
- YTRemoteBot instancia sin red y registra al dueno (OWNER_ID) como admin (verificado).
- Sintaxis OK en todos los .py (`python -m py_compile`); dependencias importan en el Python embebido.
- resolve_stream_url(): busca + resuelve + carga en mpv con streams separados (video VP9 + audio Opus via audio-add). Verificado: mpv reproduce con exito.
- **Tests unitarios `test_card.py`**: VERDES (0 errores LSP). player.py expone `_loaded` y property `loaded` para que las assertions funcionen.
