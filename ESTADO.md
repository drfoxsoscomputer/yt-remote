# ESTADO — YT-Remote

> Una pagina que cualquier sesion futura lee primero. Si esto no esta
> actualizado, es un bug mio, no del usuario.

## Que es
Bot de Telegram que controla la reproduccion de YouTube (video en la PC
donde corre el programa). Busqueda por chat, tarjeta de control con
botones, radio automatica por artista, roles (admin/dj/user).

## Como correr
1. Todo vive DENTRO de la carpeta (portable de verdad): Python embebido en
   `runtime\python\`, mpv en `runtime\mpv\`, dependencias en el Python
   embebido.
2. Descargar el ZIP desde https://github.com/drfoxsoscomputer/yt-remote/releases
   (no requiere git, no requiere Python instalado).
3. Doble clic en `ytremote.bat` → la primera vez le guía para configurar
   el token y su ID de Telegram (se guardan en `.env` que NO se sube a
   GitHub).
4. Dejar la ventana abierta. En Telegram, agregar el bot a un grupo y
   escribir `/start`.

## Estructura
- `src/main.py` — entrada; sys.path al parent y run_polling.
- `src/bot.py` — YTRemoteBot: comandos `/buscar` (dj+), `/now`,
   `/pause`, `/resume`, `/next`, `/prev`, `/stop`, `/volume`, `/adduser`,
   `/removeuser`, `/solicitar`; wrapper `_require(rol)`; tarjeta persistente
   con botones; mensaje de lista (boton 📋) con paginacion y callbacks `lst:`;
   callbacks con thumbnails y `_search_cache`.
- `src/search.py` — `is_youtube_link()` + `search()` via yt-dlp; `SearchResult`.
- `src/player.py` — clase Player, IPC de mpv por named pipe
   `\\.\pipe\mpv-ytremote`. Usa `--idle=yes` para mantener el pipe vivo.
- `src/queue_manager.py` — QueueManager + QueueItem (deque FIFO + historico
   anti-ping-pong maxlen 500).
- `src/config.py` — `load_config()`; lee token PRIMERO de `.env`, fallback
   `config.json`; resuelve `mpv_path` relativo al PROJECT_ROOT; error claro
   si falta token; lee `OWNER_ID` y `ALLOWED_CHAT_ID`.
- `src/setup_cli.py` — utilidades para `ytremote.bat`: `is_configured()`,
   `write_env()`, `set/get_allowed_chat_id()`, y el comando `launch` (wizard
   interactivo + arranque del bot).
- `config.json` — NO sensibles: `mpv_path`, `default_role`, `max_results`;
   token placeholder.
- `.env` — TOKEN REAL + `OWNER_ID` + `ALLOWED_CHAT_ID` (ignorado por git).
- `.env.example` — plantilla segura de configuracion (versionada).
- `ytremote.bat` — lanzador minimo ASCII estilo Albion (4 lineas, sin BOM/
   acentos/chcp): `title` + llamada a `setup_cli.py launch` + pause. Todo el
   texto con acentos/ñ vive en Python.
- `GUIA.txt` — guia de usuario en texto plano (como crear bot, conseguir ID,
   uso, problemas).
- `README.md` — guia rapida con descarga, comandos, roles.
- `.gitignore` — ignora `.env`, `venv`, `data/*.json`, `__pycache__`, `*.log`,
   `bin/`, `static/`, `yt-remote-*.zip`.
- `runtime/` — Python 3.13.9 embebido + mpv portable (SE SUBEN al repo, por
   decision del usuario: todo incluido).
- `yt-remote-v0.2.1-portable.zip` — binario portable adjunto al release v0.2.1
   en GitHub.

## Estado actual
**v0.3.1 publicado en https://github.com/drfoxsoscomputer/yt-remote/releases/tag/v0.3.1**
ZIP `yt-remote-v0.3.1-portable.zip` (~42.4 MB). Incluye el fix de los 4 bugs
post-v0.3.0 (no reanuda tras apagado, a veces video sin audio, mpv "Drop files",
errores que no llegan al admin) con el plan Fase 1-6 aprobado por el usuario,
mas el fix del bot congelado (3ra ronda, timeouts de resolucion) y la card
siempre al final (4ta ronda).

**En desarrollo (post-v0.3.2): Ronda 8 — playlist al toque y persistencia.** Al
pegar un link de playlist/mix: arrancan YA los primeros 15 temas (quick-load) y
el total real se reporta ("Playlist (15/N): …"), mientras el RESTO se expande en
segundo plano hasta 5000 temas (timeout 300s, dedupe por URL, si cambias de
playlist se cancela la expansión anterior; el 📋 se actualiza solo). El bot no
hace wrap prematuro: si la expansión sigue corriendo y llegás al último tema
cargado, espera la anexión en vez de volver al primero. La playlist, la canción,
el cursor y la página del 📋 SE PERSISTEN (al reiniciar se restaura la posición
real). Fix ▶️: antes descartaba la playlist (set_current limpiaba los items).
Fix de videos EN VIVO sin sonido: el HLS de un directo de YouTube NO tiene
formato combinado muxed; se resuelve como video+audio separados y el audio
se detecta aunque yt-dlp no declare la key `acodec` (viene solo con
`vcodec: none`). Eliminada la re-resolución con `best[height<=N]` del Fix
anterior (siempre fallaba en directos y marcaba un error falso). Las
duraciones desconocidas se muestran como "--:--" (no "0:00"). 103 tests
verdes.

**Ronda 9 — bugs reportados por el usuario (2026-09-08/09).** Dos fix junto al
fix live: (1) la lista 📋 en modo radio (item suelto, sin playlist) no
mostraba nada salvo la navegación; ahora muestra el tema sonando como botón
"▶️ titulo" cuya acción es la misma que cerrar (`lst:close`). (2) El botón de
cierre del selector de calidad usaba "✖ Cerrar" (✖ se ve negro); pasó a "❌",
idéntico al del paginado. Test nuevo `test_list_radio_shows_current`.

**Ronda 10 — sync de directos y cancelar /buscar (2026-09-09).** Dos bugs del
usuario: (1) el AUDIO se escuchaba desfasado del video en directos de YouTube
Live. Causa: en vivo el HLS llega como video y audio en sub-playlists
independientes, y `load()` los cargaba con `loadfile` + `audio-add` — cada
demuxer calcula su propio live edge y el audio queda corrido. Fix:
`player.load()` detecta un par de URLs `.m3u8` y construye un master `.m3u8`
temporal (`%TEMP%\ytremote_live.m3u8`, sobrescrito en cada load) que une el
video child con el audio child como grupo `EXT-X-MEDIA` (misma variante):
UN solo demuxer HLS y mpv sincroniza A/V según el spec. Respeta `MAX_HEIGHT`
porque el master referencia exactamente el video que eligió yt-dlp; si el
track-list no muestra audio, cae a `audio-add` viejo como plan B. DASH normal
intacto. (2) El listado de `/buscar` quedaba ARRIBA de la card: el wrapper
`_with_card_reposition` re-renderizaba la card mientras el listado seguía en
pantalla. Fix: flag `_search_list_pending` (se setea al mostrar resultados, se
limpia al elegir, cancelar o ante un `/buscar`/`/play` nuevo) hace que el
wrapper NO mueva la card mientras el listado está visible → la card queda
arriba. Además, el listado termina con un botón "❌ Cancelar" (`pick:cancel`)
que lo borra con su desvanecimiento sin reproducir nada y deja la card
visible; elegir una canción mantiene el flujo actual (borra el listado con
fade + card nueva con miniatura). 6 tests nuevos. **109 tests verdes**.

- **Mensaje de la lista (2026-09-08, ronda 7)**: decisión del usuario NO
  trabajar sobre el comando sino rediseñar el 📋: lista como mensaje separado,
  paginación 10 por página [◀ ❌ ▶], solo admin/dj eligen, ❌ lo usa cualquiera,
  reapertura borra + manda nueva, y el comando `/lista` se elimina. `src/bot.py`:
  `_LIST_PAGE_SIZE`/`_LIST_TITLE_MAX`, `_list_chat_id`/`_list_message_id`/
  `_list_page`/`_list_can_select`, `_list_keyboard` (flag de selección fijo en
  el mensaje), `_open_queue_list` (toast si vacia), `_edit_list_message` (◀ ▶
  editan TEXTO + botones de la pagina nueva), `_on_list_callback` (rama `lst:`
  en `on_callback`; `lst:close` cualquiera, `lst:N` con guard dj +
  `_control_lock`; bug evitado: comparar contra el actual ANTES del `jump_to`,
  porque `jump_to` ya mueve el cursor). Handler `CommandHandler("lista")` y
  `cmd_queue` eliminados; 5 textos
  "Usa /lista" reescritos a "Usa el boton 📋". test_roles ajustado + 10 tests
  nuevos en test_card (abrir dj, user sin botones, paginacion, reapertura,
  elegir reproduce y cierra, user rechazado, ❌, vacia, ya sonando, fuera de
  rango). **89 tests verdes**.

- **Fix paginacion lista (2026-09-08, ronda 7b)**: el usuario probó una
  playlist de 21 canciones y notó dos problemas en el mensaje de la lista:
  la 3ra pagina mostraba el listado completo en texto plano con el tema 21
  como boton suelto, y ademas preguntó por que cada tema se veia dos veces
  (texto plano + boton). Salida del rediseño final: la lista se muestra UNA
  sola vez, como BOTONES paginados (posicion global + titulo COMPLETO sin
  truncar, el actual con "▶️ "; adios a `_LIST_TITLE_MAX`). El texto del
  mensaje es SOLO el encabezado con pagina actual y total (`Lista (pag 2/3):`,
  u `Lista:` con una sola pagina; modo radio idem). `_queue_list_text(page)`
  genera ese encabezado; `_edit_list_buttons` → `_edit_list_message` pagina
  con `edit_message_text` (texto + keyboard sincronizados). Con 21 temas:
  pag 1 = botones 1-10, pag 2 = 11-20, pag 3 = solo el 21. Tests: el de
  paginacion pasa de 25 a 21 temas y verifica los labels numerados y el
  header por pagina. **89 tests verdes**.

- **Calidad en la tarjeta (5ta ronda, plan 7 pasos)**: `src/search.py` gana
  `MAX_HEIGHT` global + `set_max_height()`; `resolve_stream_url` respeta el
  tope (`bestvideo[height<=N]+bestaudio/best[height<=N]`). `src/persistence.py`
  guarda `max_height`. `src/bot.py`: `QUALITY_LEVELS`, `_max_height`, fila 3 en
  `_control_keyboard`, `_quality_keyboard` (grilla 4x), dispatch `cl:` en
  `on_callback`, `_on_control` rama `calidad`, `_on_quality_callback` (admin).
  6 tests nuevos en test_card. Docs al dia.
- **Fix calidad (2026-09-08, ronda 6a)**: el usuario reportó que la calidad no
  cambiaba de una vez, que el bot se congelaba y que el icono 🎚️ parecía
  "una lapida con una cruz". Causas: (1) el callback solo seteaba `MAX_HEIGHT`,
  pero el stream cacheado (`_stream_cache`) y el prefetch quedaban con la
  calidad vieja — el cambio no se veía hasta re-resolver; (2) el callback salía
  del candado `_control_lock` y podía colisionar con un toque de boton: al
  re-crear la card desde dos flujos, el otro quedaba esperando el lock para
  siempre = freeze de TODA la botonera. Cambios: `_on_quality_callback` ahora
  corre DENTRO de `_control_lock`, invalida `_stream_cache`/`_radio_search_cache`
  y cancela el prefetch, relanza el tema actual recargado (`_stream_for` →
  `player.load/play` con la nueva resolucion), icono ⚙️ (engranaje) y tope
  maximo 1080 (QUALITY_LEVELS sin 1440/2160; un state viejo con 2160 cae a
  None=1080). 3 tests nuevos: recarga del tema actual, lock ocupado descarta,
  y >1080 ignorado. **78 tests verdes**.
- **Fix card sin re-crear (2026-09-08, ronda 6b)**: el usuario pidió que al
  elegir calidad la card NO se borre con el efecto de desvanecimiento ni se
  re-renderice toda: solo deben volver los botones de control. Nuevo
  `_swap_card_keyboard(keyboard)` hace `edit_message_reply_markup` sobre el
  MISMO mensaje (ni texto ni miniatura se tocan). Abrir el selector ahora
  también solo cambia el teclado (antes editaba el texto con "⚙️ Calidad
  actual..."); elegir/cerrar restaura `_control_keyboard()` sobre el mismo
  mensaje. Se eliminó el par `_remove_card()`+`_send_card()` del callback
(era lo que desvanecía y recreaba). Tests ajustados (selector y aplicar
   verifican MARKUP, sin sent/deleted). **78 tests verdes**.
- **Misma calidad = Cerrar (2026-09-08, ronda 6c)**: recuperado tras un
  `git checkout -- src/bot.py` accidental que revirtió TODOS los cambios de
  bot.py de la feature calidad (solo bot.py perdió codigo; `search.py`/
  `persistence.py` conservaron sus cambios). Al reconstruir, el usuario pidió
  que elegir el nivel YA activo haga EXACTAMENTE lo mismo que "✖ Cerrar":
  guard `if level == (self._max_height or 1080)` que restaura
  `_control_keyboard()` y sale ANTES de aplicar/persistir/invalidar/recargar.
  Nuevo test `test_quality_same_level_is_ignored` (misma calidad no
  re-resuelve, no invalida `_stream_cache`/`_radio_search_cache`, no recarga
  el player, no reenvía/borra la card, y el botón ⚙️ sigue mostrando el nivel).
  Además un estado `max_height` inválido (>1080) ahora resetea
  `search.MAX_HEIGHT` a 1080 explícitamente (antes podía quedar en el valor
  previo). **79 tests verdes**.

- **Fase 1 (mpv vivo)**: `_toggle_play_pause` y `cmd_resume` reproducen el
  item actual por `_play_item` si mpv no corre (prende el reproductor); los
  2 bloques prefetch de `cmd_next` llaman `player.start()` antes de cargar.
- **Fase 2 (stop que no borra)**: `Player.rewind()` (pausa + seek 0 absolute);
  `cmd_stop` conserva TODO (cola, historial, radio, navegacion, cache) y deja
  `_paused=True` para reanudar con ▶ desde 0:00. El boton stop de la tarjeta
  ya no fuerza `_paused=False`.
- **Fase 3 (audio robusto)**: `Player.load()` reintenta el `audio-add` (DASH)
  y VERIFICA con `track-list` (via request_id) que haya pista de audio
  seleccionada antes de dar el track por cargado.
- **Fase 4 (errores al admin)**: `_notify_admin()` copia todo fallo real
  (resolucion, mpv, reproduccion, red) al chat del OWNER_ID como log;
  cubierto en _play_item, cmd_next/cmd_prev, autoplay, saltos y re-resolucion.
  Los errores desde botones de tarjeta suben como alerta visible
  (`show_alert=True`) en vez de toast efimero.
- **Fase 5 (tests)**: 6 tests nuevos en test_card (toggle/resume/next prenden
  mpv, stop conserva estado y no llama clear, notify_admin llega al owner).
  56 tests verdes. Se reinstalo pytest/pluggy/pytest-asyncio (corruptos) y se
  creo `pytest.ini` con `asyncio_mode=auto`.
- **Fase 6**: zip `yt-remote-v0.3.1-portable.zip` (42.4 MB) GENERADO. Tag + release
  publicados el 2026-09-08.
- **Fix botones ⏭/⏮ lentos en radio** (2026-09-07, 3 medidas): (1) candado
  anti-colision en `_on_control` (`_control_lock`): toques extra se descartan
  al instante ("⏳ Un momento, primero termina...") en vez de encolarse; (2)
  cache de la lista de radio (`_radio_search_cache` por ancla): la busqueda de
  50 temas se hace UNA vez por /buscar y cada salto elige de la cache (era el
  delay real de ~2s por boton); (3) respuesta inmediata: al tocar sin prefetch
  la tarjeta se re-renderiza YA con "⏳ Cargando…\n🎵 {titulo}" (`_render_pending`)
  y el audio entra cuando yt-dlp termina (los 4 caminos radio de cmd_next/
  cmd_prev). Se descubrio ademas que los tests dependian sin querer de un
  `data/state.json` vacio: `make_bot` ahora aísla la persistencia con
  `isolated_state_path()` (nunca toca el state real del proyecto). 3 tests
  nuevos cubren las medidas. **59 tests verdes**.
- **Fix skeleton en la card (2026-09-07, 2da ronda del mismo bug)**: el usuario
  aclaró que el feedback de carga debe vivir en la CARD (el toast de arriba
  "Cargando..." era "una porquería" imperceptible). Cambios: (1) se eliminó el
  toast `query.answer("Cargando...")` — todo el feedback está en la tarjeta;
  (2) `_render_card` acepta `item` opcional y `_render_pending(title, item)`
  muestra miniatura + título del candidato SIN tocar `queue._current` (antes la
  thumb quedaba clavada en el tema viejo porque `_render_card` la sacaba de
  `self.queue.current`); (3) el commit de `queue._current = candidate` en el
  camino "candidato nuevo" se mueve a DESPUÉS del load exitoso (corrige un bug
  latente: antes se setteaba antes de resolver y no se restauraba al fallar).
Presentación ≠ estado de dominio. 1 test ajustado + 1 nuevo (si resolve falla,
   `queue._current` sigue apuntando al anterior). **60 tests verdes**.
- **Fix bot congelado (2026-09-07, 3ra ronda; causa raiz del "no responde nada")**:
   el usuario reportó que el bot quedó "pegado" y ningún botón/comando respondía
   aunque el video seguía sonando. Causa: `_stream_for` resolvía el stream con
   `asyncio.to_thread(resolve_stream_url, url)` SIN timeout; `resolve_stream_url`
   corre yt-dlp síncrono y puede colgarse 30s+ (TLS, rate-limit, client bloqueado)
   INCLUSO con internet; y como python-telegram-bot procesa los updates en
   secuencia (`run_polling` sin concurrencia), un handler colgado congela todo el
   polling (el mpv sigue vivo porque es proceso aparte). Cambios: (1) timeout de
   20s (`_RESOLVE_TIMEOUT`) sobre CADA resolución de stream y sobre `search()` en
   `_pick_next_candidate` — al expirar se trata como fallo de resolución (mensaje +
   `_notify_admin`), el polling nunca queda mudo; (2) se restauró la respuesta al
   toque del botón pero SIN toast: `await query.answer()` mudo al entrar en la
   acción — el spinner del botón se apaga al instante (antes, al quitar el toast
   en la ronda 2, el callback quedaba sin responder hasta que terminaba toda la
   acción y el botón parecía muerto). El candado anti-colisión y el "⏳ Un momento..."
   siguen intactos (es el firewall, no el problema). 2 tests nuevos:
   `_stream_for` con resolve colgado (sleep 5s) expira por timeout en <4s; botón
   vol-10 responde con `answered == ()` SIN toast y la acción corre. **61 verdes
   + 1 fallo ambiental** (`test_singleton_lock`: el puerto 47631 lo tiene el bot
   real corriendo en la PC — matar el proceso 12636 y el test vuelve a pasar).
- **La card siempre al final (2026-09-08, 4ta ronda)**: el usuario pidió que la
   card quede SIEMPRE como el último mensaje del chat: al escribir un comando o
   texto, los mensajes quedan arriba y la card se desvanece (delete_message) y
   se re-envía fresca al final. Cambios: (1) `_remove_card()` borra la card OK
   real (seriada con `_card_lock`) y resetea ids — el pick/:N ya no deja cards
   huérfanas (antes solo resetaba ids sin borrar el mensaje); (2) `_reposition_card(chat_id)`
   = borrar + re-enviar al final; (3) wrapper `_with_card_reposition(handler)` en
   todos los CommandHandlers: si la card existía antes del comando, se reposiciona
   al final al terminar; (4) MessageHandler pasivo `filters.TEXT & ~COMMAND`
   reposiciona la card cuando alguien escribe texto (sin responder); (5) el
   auto-advance NO reposiciona (sigue editando en su lugar — decisión del
   usuario); (6) "Expandiendo playlist/mix" dejó de ser un mensaje clavado: si
   hay card avisa en ella con "⏳ Expandiendo playlist…" y si no hay card usa un
   mensaje efímero que se borra al terminar; además `expand_playlist` ahora corre
   bajo `_RESOLVE_TIMEOUT` (20s) — mismo riesgo de congelamiento que `_stream_for`.
   7 tests nuevos. **69 verdes** (el singleton volvió a pasar porque el bot real
   ya no corre).

> **Nota 2026-09-07**: el primer ZIP subido no incluia `typing_extensions.py`
> (paquete suelto en `site-packages/`, no carpeta) y el bot crasheaba al
> arrancar con `ModuleNotFoundError`. Se corrigio `make_zip.py` (filtrar por
> nombre quitando el `.py`) y se re-subio el ZIP corregido con `--clobber`.
> Si al descargar da error, borrarlo y volver a descargar.

### Features de v0.2.1 (sobre v0.2.0)
- **Modelo de roles simplificado**: user = nada (solo ver lista), dj = buscar +
   controlar musica, admin = todo. Comando `/solicitar` permite a user pedir
   acceso dj al admin.
- **Botones de la tarjeta**: user ve todos pero solo dj/admin pueden usarlos**. El
  user ve todos pero al tocarlos recibe toast "No tenes permiso".
- **Comando `/solicitar`**: user escribe `/solicitar` y el admin recibe un
   mensaje para decidir si darle o no acceso.
- **set_my_commands**: menu "/" muestra solo /start /buscar /solicitar /adduser
   /removeuser.
- **help_for_role**: ayuda simplificada por rol (user no ve comandos, dj ve /buscar,
   admin ve adduser/removeuser).
- **/buscar requiere rol dj**: user no puede escribir /buscar.
- **Limpieza de basura**: `bin/`, `static/`, `scripts/` y `site-packages`
   de pytest/pluggy eliminados del historial con `git filter-repo`; `.gitignore`
   actualizado para nunca incluirlos.

### Detalles de diseño vigentes
- **Persistencia de estado (NUEVA en v0.2.2)**: `src/persistence.py` guarda en
  `data/state.json` volumen, pausa, cancion actual, playlist, historial (100),
  ancla de radio y la tarjeta. Escritura atomica (`os.replace`) + debounce 500ms.
  Al reiniciar: NO auto-reanuda (arranca en pausa), re-edita la tarjeta con
  "🔄 Retomada del cierre anterior: usa ▶ para reanudar."
- **Diagnostico remoto por Telegram (NUEVO)**: `resolve_stream_url` hoy prueba
  client 'visionos' y, si falla, reintenta UNA vez con el client por defecto de
  yt-dlp (algunas redes/ISP lo rechazan). Cuando no se puede resolver, el motivo
  real de yt-dlp viaja en el mensaje de error del bot (`last_resolve_error()`),
  sin pedirle consolas al usuario afectado.
- **Tarjeta persistente con botones**: edita el MISMO mensaje con
  `[⏮ ▶/⏸ ⏭ ⏹]` + `[🔊−10 🔊+10 📋]` + fila 3 `[⚙️ Calidad: N]`.
  Callbacks `ctl:prev|pp|next|stop|vol-10|vol+10|lista|calidad` despachados
  por `_on_control`; la rama `calidad` (solo admin) cambia SOLO el teclado:
  la grilla `_quality_keyboard` reemplaza a los controles y al elegir/cerrar
  `_on_quality_callback` restaura `_control_keyboard` sobre el MISMO mensaje
  (`edit_message_reply_markup`): la card nunca se borra ni se recrea.
- **Tope de resolucion (NUEVO)**: `search.MAX_HEIGHT` (1080 default, tope
  maximo de la app: sin 1440/2160) aplica a `bestvideo` y a `best` en
  `resolve_stream_url`; `set_max_height()` lo cambia.
  Es un tope maximo: si el video no llega, usa la mayor que no lo supere. Se
  persiste en `state.json` (`max_height`) y sobrevive reinicios (un valor
  persistido >1080 cae a None=1080).
- **Radio por artista ancla**: `_radio_artist` se captura en el primer `/buscar`;
  `/next` y el auto-advance usan ese ancla para que la radio no derive.
- **Cache "mesonero"**: cache en RAM de URLs de YouTube a streams directos
   googlevideo, resolucion serial (no paralelizar para evitar rate limit).
- **Prefetch 2 fases**: decide candidato al reproducir (Fase A) + resuelve el
   stream de inmediato (Fase B) = cero silencio.
- **Playlist fija con cursor**: el boton 📋 abre la lista (mensaje aparte,
  paginado de 10 en 10, actual con [▶️]); tocar un tema mueve el cursor con
  `jump_to`; `/next` y `/prev` con wrap.
- **Hardening**: drop_pending_updates en run_polling, vigilante de red con
  `_on_bot_error` + `_net_watch_job` (15s), singleton lock en puerto 47631,
  retry de `send_photo`.

## Pendientes / ToDo
- [ ] **Revalidar la ronda 10 en vivo**: probar un directo de YouTube Live en
   Telegram por unos minutos y confirmar que el audio ya NO va desfasado del
   video (fix del master .m3u8 local); probar `/buscar artista - cancion` con
   la card a la vista (que el listado quede ABAJO de la card, cancelar lo
   borra y deja la card, y elegir muestre la card nueva con la miniatura).
   Aprobación del usuario antes de commit/push.
- [ ] **Revalidar la ronda 8 en vivo**: probar en Telegram una playlist larga
   (link → arranca YA con "Playlist (15/N)", resto en segundo plano con la card
   avisando "✅ Playlist cargada: N temas", 📋 con todo), un video EN VIVO (que
   suene con audio), y reiniciar el bot a mitad de una playlist (que retome la
   canción y el 📋 en la página correcta). Aprobación del usuario antes de
   commit/push.
- [ ] **Revalidar la lista en vivo (ronda 7)**: probar el 📋 en Telegram
   (admin/dj ve botones de tema, paginar ◀▶, elegir reproduce y cierra, ❌
   cierra para cualquiera, reapertura borra y manda nueva, user abre sin
   botones). Aprobacion del usuario antes de commit/push.
- [x] **Revalidar la calidad en vivo** (2026-09-08): el usuario la probó en
   vivo y la dio por buena; con eso aprobó commit + push (v0.3.2).
- [ ] **Revalidar el bot en vivo tras el fix del congelamiento (Test 8)**: el
   bot actual quedó colgado en el arreglo viejo; reiniciarlo
   con el fix (timeouts + `query.answer` mudo) y repetir el Test 7 (⏭ lento,
   doble toque).
- [ ] **Revalidar la card siempre al final en vivo**: escribir texto un comando
   y confirmar que la card se desvanece y reaparece como último mensaje; probar
   expandir una playlist y ver el "⏳ Expandiendo playlist…" en la card.
- [ ] **Testeo humano del fix de botones (Test 7)**: revalidar con el amigo
   que ⏭/⏮ responden al instante (tarjeta cambia YA con "⏳ Cargando…") y que
   no se encolan toques múltiples.
- [x] **Release v0.3.1 en GitHub** (2026-09-08): zip regenerado con el codigo
   nuevo (bot/player/test/docs/pytest.ini), tag `v0.3.1`, release con el zip
   como unico asset, commit + push. 69 tests verdes.
- [x] **Fix botones ⏭/⏮ lentos en radio** (2026-09-07): candado anti-colision
   + cache de lista de radio por ancla + render pendiente "⏳ Cargando…".
   59 tests verdes.
- [x] **Skeleton en la card sin riesgo de estado** (2026-09-07): toast eliminado;
   `_render_pending(title, item)` muestra miniatura+título del candidato sin
   tocar `queue._current`; commit de `_current` solo tras load exitoso. La
   presentación nunca compromete el estado de dominio. 60 tests verdes.
- [x] **Fix bot congelado / timeouts de resolución (2026-09-07)**: `_RESOLVE_TIMEOUT`
   de 20s en `_stream_for` y en `search()` de `_pick_next_candidate` (un yt-dlp
   colgado ya no congela el polling); `query.answer()` mudo al entrar en la acción
   (spinner del botón se apaga sin toast). 2 tests nuevos; 61 verdes + singleton
   verde cuando no corre el bot real.
- [x] **Calidad en la tarjeta** (2026-09-08): boton ⚙️ en fila 3 (solo admin),
   grilla de niveles en la card, tope aplicado en `resolve_stream_url`, persistido
   en `state.json`. 9 tests (los 6 originales + recarga del actual, lock ocupado,
   >1080 ignorado); **78 verdes**.
- [x] **La card siempre al final (2026-09-08)**: `_remove_card`/`_reposition_card`
   + wrapper `_with_card_reposition` en comandos + MessageHandler pasivo de texto +
   feedback de playlist en la card con timeout. 7 tests nuevos. **69 verdes**.
- [x] **Tests `test_card.py`**: 22 errores LSP corregidos. py_compile OK + tests OK.
- [x] **Manejo de errores de red en `_pick_next_candidate`**: flag de error +
   `_radio_over_message(por_error)` sugiere el boton 📋 en fallos de red.
- [x] **Bounds check visible de /volume**: valida 0-100 y avisa en cliente.
- [x] **Pruebas en vivo con Telegram**: 15 tests humanos OK.
- [x] **`requirements.txt`**: eliminado (todo vive en runtime/).
- [x] **Menu de comandos**: `/now`, `/pause`, `/resume`, `/next`,
   `/stop`, `/volume` fuera del menu (la lista vive en el boton 📋).
- [x] **Modelo de roles simplificado**: user = nada (solo ver la lista con 📋),
   dj = buscar + controlar musica, admin = todo.
- [x] **Comando `/solicitar`**: user pide acceso dj al admin.
- [x] **Botones de la tarjeta**: user ve todos pero no puede usarlos (excepto
   📋, que abre la lista sin botones de tema). Solo dj/admin controlan.
- [x] **Documentacion actualizada**: README.md y GUIA.txt reflejan el nuevo
   modelo de roles.
- [x] **Persistencia de estado completa** (Fase 1 y 2, 2026-09-07): StateStore
   atómico + versionado, QueueItem serializable, instrumentación de
   cambios, tarjeta re-editada al arranque con pausa forzada. Tests: 3 suites
   verdes (tarjeta/roles/persistencia) sin tocar el state.json real (make_bot
   aislado con temporal).
- [x] **Diagnostico remoto del ZIP v0.2.1** (2026-09-07): se descartó "falta un
   archivo" (el zip funciona en la PC del usuario; yt-dlp igual en ambos).
   Sospecha: firewall/antivirus o ISP bloquea mpv.exe / googlevideo.com.
   Solución sin trabajo para el usuario: fallback de client en resolución +
   motivo real por Telegram. ZIP reconstruido con los fuentes nuevos.
- [x] **Pruebas en vivo con Telegram**: verificar que user no pueda usar
   botones de control, /buscar requiere dj, /solicitar llega al admin.

## Decisiones recientes
- **v0.3.1 bugfix plan (2026-09-07)**: fixes de los 4 bugs post-v0.3.0 (reanudar
  tras apagado, audio sin sonido, stop que borraba, errores invisibles).
  Errores al OWNER_ID directo (chat privado), no al allowed_chat_id. Botones
  que prenden mpv: solo atras/play/pausa/siguiente. Stop = pausa + rewind a
  0:00 sin limpiar nada.
- **v0.3.0 release**: persistencia de estado + diagnostico remoto (feature,
  bump minor). Commit `425eff5`.
- **v0.2.1 release**: features principales (modelo de roles simplificado, /solicitar,
  botones de tarjeta restringidos). Numero de version siguiendo semver:
  bump minor (0.2.0 → 0.2.1) por features nuevos (no fixes).
- **Semver aplicado**: 0.1.0 = primera version publica; 0.2.0 = features
  (pila de navegacion, retry, radio mejorada); futuros 0.2.x = bugfixes;
  0.3.0 = breaking change o feature mayor.
- **Limpieza del historial con git filter-repo** (2026-09-06): `bin/`,
  `static/` y `scripts/` eliminados del historial; `.gitignore` actualizado
  para que clones frescos no descarguen basura. Force-push autorizado.
- **Cambio a modelo de roles simplificado** (2026-09-06): user ya no puede
   usar comandos /buscar /next /stop /volume del menu; solo ver la lista con
   el boton 📋. dj y admin
  mantienen acceso total. Se agregó comando `/solicitar` para que user pida
  acceso dj al admin.
- **Numeracion de version x convencion** (2026-09-06): usuario pidio seguir
  semver, no el "build N" anterior.

## Tests / verificacion
- Player IPC: mpv arranca en idle, acepta set_volume/get_property, se cierra. Verificado.
- Busqueda: yt-dlp devuelve resultados reales; `is_youtube_link()` correcto.
- Config: token/OWNER_ID/ALLOWED_CHAT_ID desde `.env`; `.env` ignorado, `.env.example`
  versionado (git check-ignore).
- YTRemoteBot instancia sin red y registra al dueno (OWNER_ID) como admin (verificado).
- Sintaxis OK en todos los .py (`python -m py_compile`); dependencias importan en el
  Python embebido.
- resolve_stream_url(): busca + resuelve + carga en mpv con streams separados
  (video VP9 + audio Opus via audio-add). Verificado: mpv reproduce con exito.
- **Tests unitarios `test_card.py`**: VERDES (0 errores LSP). player.py expone
  `_loaded` y property `loaded` para que las assertions funcionen.
- **Tests de calidad** (test_card, 10): boton en fila 3, selector solo admin
  con ✓ en el vigente, eleccion aplica+persiste SIN re-crear la card (solo
  MARKUP), recarga del tema actual tras cambiar nivel, persistencia entre
  reinicios (state.json compartido), cerrar sin cambios, no-admin rechazado,
  lock ocupado descarta, >1080 ignorado, y misma-calidad-equivalente-a-cerrar.
- **Tests de la lista** (test_card/test_roles, 11): boton 📋 abre MENSAJE aparte
  sin pisar la card, user sin botones de tema, paginacion 10 por pagina con
  ◀▶ (edita solo botones), reapertura borra+manda nueva, elegir reproduce y
  cierra la lista con fade, user rechazado con alerta, ❌ cierra para cualquiera,
  cola vacia = toast sin enviar, elegir el actual = "Ya esta sonando", posicion
  fuera de rango avisa y no cierra. **89 tests verdes en total**.

### Tests de la ronda 8 (13 nuevos)
- **Ronda 8**: `test_toggle_play_pause_keeps_playlist` (fix ▶️), persistencia
  de cursor+página (`test_restore_cursor_and_list_page`, `test_restore_cursor_clamped_to_range`,
  `test_restore_cursor_corrupt_falls_to_zero`, `test_close_list_keeps_page`,
  `test_defaults_for_cursor_and_list_page`, `test_round_trip_cursor_and_list_page`),
  quick-load (`test_playlist_feedback_renders_in_card`, `test_playlist_expand_times_out`,
  `test_playlist_quick_load_starts_immediately`, `test_playlist_background_expansion_does_not_duplicate`,
  `test_playlist_background_expansion_ignores_swapped_queue`, `test_pick_next_waits_for_expansion_no_early_wrap`),
  y live (`test_live_stream_resolves_separated`, `test_fmt_duration_live_shows_dashes`).
  **103 tests verdes en total** (74 en test_card, 17 en persistence, 12 en roles).

### Tests de la ronda 10 (6 nuevos)
- **Ronda 10**: `test_build_master_playlist_joins_video_and_audio` (el master
  local une video child + audio child con el mismo grupo EXT-X-MEDIA),
  `test_search_keyboard_has_cancel_button` (el listado termina con ❌ Cancelar
  y el flag pendiente queda True), `test_pick_cancel_removes_list_and_keeps_card`
  (cancelar borra el listado con fade, no toca la card, no reproduce),
  `test_pick_result_clears_pending_search` (elegir limpia el flag),
  `test_with_card_reposition_skips_while_search_pending` (con listado visible
  la card NO se mueve), `test_cmd_play_link_resets_pending_search` (un link
  nuevo descarta el listado pendiente). **109 tests verdes en total**
  (80 en test_card, 17 en persistence, 12 en roles).

### Detalles técnicos nuevos (ronda 8)
- **Quick-load + expansión de fondo**: `search.quick_playlist(url, N)` usa
  `playlistend` + `playlist_count` para devolver los primeros N y el total real.
  `_play_link_or_playlist` arranca YA con `_QUICK_TRACKS=15`, `_radio_artist` se
  fija al canal del primer tema, y lanza `_start_playlist_expansion(url, total)`
  que corre `expand_playlist(url, _MAX_PLAYLIST=5000)` en `to_thread` con
  `_EXPAND_TIMEOUT=300s`, dedupe por URL contra lo ya cargado, verificación de
  vigencia (si cambiaste de playlist se ignora), `queue.add` + persist por lotes,
  y aviso en la card ("✅ Playlist cargada: N temas") al terminar. Cambiar de
  playlist cancela la expansión anterior (`_cancel_playlist_expansion`).
- **Sin wrap prematuro**: `_pick_next_candidate`, con expansión activa y cursor
  en el último tema, espera hasta `_expand_esperas < 30` sleeps de 1s por si la
  expansión anexa más, antes de hacer wrap al primero.
- **Persistencia de posición**: `state.json` guarda `cursor` y `list_page`; al
  restaurar el cursor se clampa al rango real y la página se valida (int, 0+).
  Cerrar el 📋 NO resetea la página (la reapertura usa la última; el render
  clampa rangos inválidos).
- **Fix live sin sonido**: el HLS de un directo de YouTube NO tiene formato
  combinado muxed (la re-resolución con `best[height<=MAX_HEIGHT]` del Fix del
  Paso 6 siempre fallaba y dejaba un error falso en `last_resolve_error`). El
  directo se resuelve como video+audio separados y `_stream_from_info` detecta
  el audio HLS aunque yt-dlp no declare la key `acodec` (el formato llega solo
  con `vcodec: "none"`). `_fmt_duration(0|None)` → "--:--".
- **Fix ▶️ descartaba la playlist**: `_toggle_play_pause` llamaba
  `_play_item(..., preserve_current=False)` → `set_current` → `_items=[]`.
  Ahora es `preserve_current=self.queue.has_playlist`.