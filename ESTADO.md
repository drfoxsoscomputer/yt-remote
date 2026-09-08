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
- `src/bot.py` — YTRemoteBot: comandos `/buscar` (dj+), `/lista`, `/now`,
   `/pause`, `/resume`, `/next`, `/prev`, `/stop`, `/volume`, `/adduser`,
   `/removeuser`, `/solicitar`; wrapper `_require(rol)`; tarjeta persistente
   con botones; callbacks con thumbnails y `_search_cache`.
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

**En desarrollo (post-v0.3.1): Calidad en la tarjeta.** Boton ancho en fila 3
(`⚙️ Calidad: N`, visible para todos, solo admin lo usa). Al tocarlo la propia
tarjeta cambia SOLO el teclado a la grilla de niveles (144-1080, el vigente con
✓, + Cerrar); elegir aplica el tope a `resolve_stream_url` (`MAX_HEIGHT`), lo
persiste en `data/state.json` (`max_height`), invalida cache de mesonero/radio y
RECARGA la canción actual con la resolución nueva. La card NUNCA se borra ni se
recrea: al elegir (o cerrar) vuelve el teclado de control sobre el MISMO mensaje
(`edit_message_reply_markup`), sin desvanecimiento ni reenvio. No-admin
rechazado con alerta; si otra accion de boton corre, el toque se descarta al
instante (candado anti-colision, fix del freeze). Elegir el nivel YA activo
equivale a Cerrar: no recarga ni invalida cache. Tope maximo: 1080 (sin
1440/2160). 79 tests verdes.

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
- **Playlist fija con cursor**: `/lista` muestra 25 temas con `[▶️]` en el
  actual; `/lista N` mueve el cursor; `/next` y `/prev` con wrap.
- **Hardening**: drop_pending_updates en run_polling, vigilante de red con
  `_on_bot_error` + `_net_watch_job` (15s), singleton lock en puerto 47631,
  retry de `send_photo`.

## Pendientes / ToDo
- [ ] **Revalidar la calidad en vivo**: tocar ⚙️ como no-admin (alerta), como
   admin elegir 480 y ver la canción recargarse SIN que la card se borre ni se
   desvanezca (solo vuelven los botones de control sobre el mismo mensaje, ⚙️
   mostrando "Calidad: 480p"); repetir tocando el nivel YA activo (debe
   comportarse como ✖ Cerrar: no recarga nada); repetir el escenario del freeze
   (cambiar calidad y tocar ⏭ varias veces seguidas: no debe congelarse);
   reiniciar el bot y confirmar que mantiene la calidad elegida.
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
   `_radio_over_message(por_error)` sugiere /lista en fallos de red.
- [x] **Bounds check visible de /volume**: valida 0-100 y avisa en cliente.
- [x] **Pruebas en vivo con Telegram**: 15 tests humanos OK.
- [x] **`requirements.txt`**: eliminado (todo vive en runtime/).
- [x] **Menu de comandos**: `/now`, `/pause`, `/resume`, `/lista`, `/next`,
   `/stop`, `/volume` fuera del menu.
- [x] **Modelo de roles simplificado**: user = nada (solo ver lista), dj = buscar +
   controlar musica, admin = todo.
- [x] **Comando `/solicitar`**: user pide acceso dj al admin.
- [x] **Botones de la tarjeta**: user ve todos pero no puede usarlos (excepto
  📋). Solo dj/admin usan los controles.
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
  usar comandos /buscar /next /stop /volume del menu, solo /lista. dj y admin
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
  **79 tests verdes en total**.