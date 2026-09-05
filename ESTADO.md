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
- MODELO NUEVO (imita el flujo de YouTube): `/play` reproducе YA, sin cola manual. Un nuevo /play corta lo que suene (loadfile replace). Al terminar una canción el bot sigue solo con radio por semilla (busca "una parecida" al track actual).
- Prefetch 2 fases (cero silencio): Fase A decide el candidato al reproducir (playlist > radio semilla); Fase B resuelve el stream ~45s antes del final (las URLs de googlevideo expiran) y lo cachea. El end-file salta al stream cacheado al instante. Un /play del usuario cancela el prefetch.
- Playlists/mixes: /play con link de list/mix expande todos los tracks (expand_playlist con extract_flat) y arma una PLAYLIST FIJA (set_playlist): el primero suena YA, el cursor avanza en orden y da la vuelta (BUCLE) al llegar al final; nunca se consumen (la lista queda siempre completa en /queue). Un /play nuevo (playlist o cancion suelta) reemplaza la reproduccion entera.
- La radio por semilla ahora se siembra con el ARTISTA/CANAL del track actual (QueueItem.channel, traido de entry.channel/uploader en search() y expand_playlist()), no con el titulo completo: asi /next da otra cancion del musico. Si el canal no existe (links sueltos) cae al titulo (_radio_seed).
- setMyCommands al arrancar (post_init): al escribir "/" en Telegram se ve la lista de comandos.
- /prev quedó como placeholder (sin historial no aplica); fuera del help.
- Fases 1-5 anteriores completadas y pusheadas. Fase 6 (testing) en curso.
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
- FIX VENTANA (2026-09-04): la ventana de mpv se cerraba al terminar cada cancion y se reabria con la siguiente. Con `--keep-open=yes --force-window=yes` la ventana queda SIEMPRE abierta (pausa en el ultimo frame). Como keep-open suprime el evento end-file, el fin de cancion se detecta suscribiendo a la propiedad `eof-reached` de mpv via `observe_property`: el reader thread abre SU propio handle del named pipe y hace el subscribe por ese mismo handle (mpv emite los property-change al cliente que se suscribio), y los comandos load/play/etc. van por un handle EFIMERO aparte (en Windows escribir y leer por el mismo handle de un named pipe cuelga la lectura). start() espera a que el reader abra el pipe (threading.Event) antes de retornar. Verificado empíricamente: fin de cancion detectado + encadenamiento fin1→carga2→fin2.
- FIX HUERFANOS (2026-09-04): los tests dejaban procesos mpv huerfanos (cada uno con su ventana vacia). main.py ahora registra `atexit.register(bot.player._quit)` para matar mpv al salir el bot. Se limpiaron 11 mpv huerfanos con Stop-Process -Name mpv -Force (0 procesos restantes).
- FULLSCREEN + 1080p (2026-09-04, pedido del usuario): mpv arranca SIEMPRE en pantalla completa (`--fullscreen=yes` en _launch_mpv; si el usuario en la PC la cambia, no se fuerza de vuelta). La resolucion paso de 360p cap (client 'android' de yt-dlp solo expone formato 18) a maximo 1080p: resolve_stream_url ahora usa player client 'visionos' (no bloqueado, expone hasta 2160p) con format `bestvideo[height<=1080]+bestaudio/best`; si el video no tiene 1080p toma la mayor que no la supere. Verificado: dQw4w9WgXcQ resuelve a 1080p (399+251), jNQXAC9IVRw (video viejo, solo 240p) resuelve bien, los tres videos de prueba devuelven streams directos video+audio.
- FIX del bloqueo de YouTube "Sign in to confirm you're not a bot": yt-dlp con el player client web por defecto falla al extraer el stream ("No se pudo resolver el video"). Solucion: resolve_stream_url() fuerza el player client 'android' de yt-dlp, que devuelve un solo stream mp4 (video+audio juntos) y evita el bloqueo. Verificado: ricky martin / rick astley ahora reproducen en mpv.
- FIX SIN AUDIO (2026-09-04): regresion del cambio 1080p — con el client 'visionos' de yt-dlp los streams van SEPARADOS (video 399@1080 sin audio + audio 251 opus), y Player.load mandaba `loadfile` y `audio-add` back-to-back. Diagnostico empirico: si el `audio-add` llega mientras el video se esta cargando, mpv descarta la pista de audio al completar file-loaded (queda video sin sonido, aid=false, fallo silencioso SIN error). Fix: Player.load espera el evento `file-loaded` (nuevo threading.Event _file_loaded_event, seteado por el reader thread al recibir el broadcast) ANTES de mandar `audio-add`; con timeout, e igual intenta el add si no llega. Verificado: aid pasa a 1 y audio-reconfig aparece al reproducir ambos temas de prueba (audio presente). Tambien se arreglo _debug_events (un getattr con lista vacia [] es falsy y jamas logueaba).
- FIX /next (2026-09-04): con el flujo YouTube (un video suelto) la cola manual queda vacia, asi que `self.queue.next()` devolvia None y decia "No hay mas canciones en la cola". cmd_next ahora usa la MISMA decision que el auto-advance: (1) si hay prefetch resuelto salta con cero silencio (stream cacheado), (2) si no toma el proximo de la cola (queue.next()), (3) si la cola esta vacia hace radio por semilla del track actual (_pick_next_candidate). Siempre corta YA (loadfile replace). Verificado: consumo de cola + salto + audio OK.
- FIX BUCLE 2 CANCIONES / radio ping-pong (2026-09-04): la radio por semilla solo excluia el track ACTUAL (`r.url != current.url`), sin memoria de lo reproducido, asi que al sonar A buscaba "parecida" -> B, y al sonar B -> A: loop infinito entre 2 temas (igual en /next que en auto-advance, ambos usan _pick_next_candidate). Fix: QueueManager ahora mantiene un historico por URL (deque maxlen 20, _MAX_HISTORY) que se actualiza en set_current() y next() (el que deja de sonar entra al historico, con dedup) y se limpia en clear(); "_pick_next_candidate" pide mas resultados (search(seed, 10)) y elige el primero que NO sea el actual NI este en el historico (queue.is_recent()); si todo lo encontrado ya sonó devuelve None (la radio se detiene en vez de repetir). La cola manual (playlist) no cambia: es orden explícito. Verificado con simulacion: secuencia A→B→C→D→E sin repetir recientes; historico acota en 20; clear() reset total; cola manual intacta.
- UX miniaturas segun pedido del user: el listado de resultados (/play) se muestra SIN miniatura (solo texto + botones titulo/duracion); la miniatura aparece al ELEGIR el video (on_callback envia la foto del elegido + "Reproduciendo: <titulo>").
- /start ahora muestra los comandos disponibles segun el rol del que pregunta (admin ve todo, dj ve controles, user ve lo basico) via help_for_role(). Verificado.
- GUIA.txt actualizada: seccion USO con los comandos agrupados por rol (user / dj+admin / solo admin) y roles/permisos.
- Falta probar el bot en vivo con Telegram (requiere token real en .env).

## Pendientes / ToDo
- [ ] PROBAR EN VIVO con Telegram: playlist fija (25 temas siempre visibles, bucle, /queue N salta sin perder la lista), radio por artista (elegir otro musico en /play y verificar que /next da canciones de ESE artista), y que /play nuevo corte el bucle.
- [ ] Verificar el formulario real de ytremote.bat en una consola cmd de Windows (pedido de token/ID).
- [ ] Fase 6: testing en vivo con Telegram (token real en .env). PROBAR el flujo nuevo: busca→suena ya, /next, fin de canción→sigue solo, playlist/mix, que /play corte lo que suena.
- [ ] Completar/ajustar manual de usuario (GUIA.txt) al nuevo flujo (reordenar pasos segun aprobacion del usuario).
- [ ] Fase 7: push + crear release (zip) del proyecto.
- [ ] Limpiar venv\ viejo local (ya no se usa, pero esta en carpeta; no se sube).

## Decisiones recientes
- NUEVO MODELO (2026-09-04, aprobado por el user): el bot imita el flujo de YouTube — /play suena YA (no se encola); un nuevo /play corta lo que suene; al terminar sigue solo con radio por semilla (busca "una parecida"); prefetch en 2 fases para cero silencio; soporta playlists/mixes (primero suena ya, resto en cola). Motivo: el usuario quiere música continua sin colas manuales (evitar el "¿Sigues ahí?" de YouTube en navegador).
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
- NUEVO MODELO: sintaxis OK (py_compile bot/search/queue_manager/player). FALTA verificación en vivo: reproduce-ya, fin de canción con auto-continuación, playlist/mix, setMyCommands.
- VENTANA: verificado empiricamente que eof-reached se detecta con keep-open (fin cancion→on_track_ended; encadenamiento fin1→carga2→fin2 OK). FALTA la prueba visual en vivo del usuario: la ventana NO debe cerrarse entre canciones y el bot debe saltar solo a la siguiente.
- AUDIO: verificado empiricamente — con streams separados (DASH 399+251) load() espera file-loaded y el audio llega (audio-reconfig + aid=1). DOS temas seguidos con auto-advance: ambos con audio. FALTA prueba en vivo con Telegram del usuario.
- /NEXT: verificado con cola — consume el item, salta al instante con audio OK. La rama radio-por-semilla reusa _pick_next_candidate (ya probada por el auto-advance). FALTA prueba en vivo con Telegram del usuario.
- RADIO: verificado que el historico (maxlen 20) rompe el ping-pong — la radio avanza A→B→C→D→E sin repetir recientes y se detiene si todo ya sonó; clear() resetea el historico; cola manual intacta. FALTA prueba en vivo con Telegram del usuario (dejarla reproducir sola varias canciones seguidas).
- MINIATURAS /NEXT + /NOW (2026-09-05): /next (ambas ramas) y /now ahora envian la miniatura del tema via `_send_track_card` (send_photo → fallback send_message, patron de on_callback). El current y los candidatos (radio/playlist) propagan thumbnail (new QueueItem.thumbnail); los links directos la derivan de la URL via `search.thumbnail_from_url` (regex watch/shorts/youtu.be → hqdefault). La miniatura al elegir en /play (on_callback) NO se toco. El auto-avance sigue silencioso. Verificado: tests unitarios thumbnail_from_url (watch/shorts/youtu.be/invalido) + compile.
- /QUEUE CON SALTO POR NUMERO (2026-09-05): /queue mantiene la lista enumerada de la vista visible (1 = [▶️] lo que suena, luego la cola) y ahora acepta `/queue N` para reproducir YA el tema de esa posicion (QueueManager.jump_to descarta los anteriores de la cola al historico y deja el elegido como current; resolve + load + play + prefetch). Fuera de rango → lista + aviso; N=1 con algo sonando → "Ya esta sonando". Se corrigio de paso el enumerate invertido del /queue viejo (reventaba). Descripcion del comando actualizada en setMyCommands y help_for_role. Verificado: tests unitarios jump_to (con/sin current, salto al medio, fuera de rango, cola resultante) + compile.
- COLA FIJA + BUCLE + RADIO POR ARTISTA (2026-09-05, pedido del user): la cola deja de ser FIFO que se consume y pasa a PLAYLIST FIJA con cursor — `/play <playlist>` encola completa (set_playlist) y suena en orden dando la vuelta; `/queue` SIEMPRE lista los 25 temas con el actual marcado [▶️] (antes, tras un /queue 20, quedaban visibles solo 5); `/queue N` salta a la posicion SIN descartar nada (jump_to solo mueve el cursor); se interrumpe solo con un /play nuevo. `_play_item` unificado recibe el QueueItem ya construido y un flag `preserve_current` (True en playlist/jump para no pisar el cursor ya posicionado; False en radio/cancion suelta para set_current). Compatible con el historico anti-ping-pong (maxlen 20) y el prefetch 2 fases. Verificado: py_compile OK + tests unitarios (set_playlist, jump_to sin descartar, next con wrap, peek con wrap, all siempre completa, radio set_current/next None, is_recent, clear). FALTA prueba en vivo con Telegram.
- FIX ANCLA DE ARTISTA EN EL /PLAY (2026-09-05, pedido del user): la radio ya no "redescubre" a cada /next. El ARTISTA se captura UNA sola vez en la PRIMERA REPRODUCCION — cuando el usuario elige la cancion del listado (on_callback): `_radio_artist = _artist_from_title(titulo elegido)` con fallback al texto del /play si el titulo no trae "Artista - Cancion". Playlist lo fija con el primer track; /stop lo borra; un /play nuevo lo redefine. `_artist_seed` usa: 1) ancla de sesion (_radio_artist), 2) artista propagado del candidato, 3) parseo del titulo, 4) titulo limpio. Asi el titulo "al reves" ("En la Manana - Kent Leroy") YA NO desvia la cadena (el ancla de sesion manda). Ademas la radio busca 50 resultados (search(seed, 50), antes 10) para tener mas catalogo antes de agotarse. Verificado con simulacion REAL de 100 saltos (play kent leroy -> elegir cancion 1 -> 100 /next): ancla == "KENT LEROY" en TODOS los saltos, 25 canciones unicas, sin deriva, ~3.1 s por salto. py_compile OK. FALTA prueba en vivo con Telegram.
