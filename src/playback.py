"""El motor de reproduccion: que suena, que sigue y como se resuelve el audio.

Estaba todo en `bot.py`, repartido entre el handler de `/buscar` y el de la
cola, y eso lo hacia dificil de razonar porque las piezas estaban separadas
por codigo que no tiene nada que ver: resolver un stream no esta al lado de
dibujar el teclado, pero estan en el mismo archivo y a la misma altura.

Aqui vive el ciclo completo:

    mpv termina -> bandera -> el mesonero decide el siguiente -> prefetch
    resuelve el stream por adelantado -> cuando toca, se reproduce sin silencio

Y el estado que lo sostiene: el cache de streams, las URLs que se estan
resolviendo, la cola y la tarea de anticipacion, y el prefetch (candidato ya
decidido + stream ya resuelto).

QUE NECESITA DEL BOT, y solo esto:
    app.queue, app.player, app._paused, app._radio_artist, app._radio_search_cache,
    app._nav_back, app._nav_forward, app._card_chat_id, app._card_message_id,
    app._push_to_nav_back(), app._persist_dirty(), app._reply(...),
    app._notify_admin(...), app._playlist_asegurar_ventana(), app._show_card(...),
    app._render_card(...), app._track_status_text()

Se pasa el bot entero, igual que en `card.CardManager`, y por la misma razon:
aun no hay segundo cliente ni test sin Telegram, y las 14 delegaciones son el
contrato real. Cuando los haya, se inyectan colaboradores.
"""

import asyncio
import logging

from constants import PLAYLIST_ESPERAS, RESOLVE_TIMEOUT
from queue_manager import QueueItem
from search import last_resolve_error, resolve_stream_url, search

import artist_match

logger = logging.getLogger(__name__)


class PlaybackEngine:
    """Decide y ejecuta que se reproduce. No sabe de comandos ni de teclado."""

    def __init__(self, app) -> None:
        self._app = app
        # Streams ya resueltos: url -> (stream_url, audio_url).
        self.stream_cache: dict[str, tuple[str, str | None]] = {}
        # URLs en este momento siendo resueltas, para no duplicar el trabajo.
        self.resolving: set[str] = set()
        # Cola y tarea de anticipacion (el mesonero resuelve por adelantado).
        self.anticipate_queue: asyncio.Queue[str] | None = None
        self.anticipate_task: asyncio.Task | None = None
        # La puesta de mpv en otro hilo solo puede poner una bandera.
        self.queue_advance_needed: bool = False
        # Prefetch: candidato decidido y stream ya resuelto, para que /next y el
        # auto-advance no esperen nada.
        self.prefetch_task: asyncio.Task | None = None
        self.prefetch_basis: str | None = None
        self.prefetch_candidate: tuple[QueueItem, bool] | None = None
        self.prefetch_resolved: tuple[tuple[QueueItem, bool], tuple] | None = None

    # --- La bandera de fin de cancion -------------------------------------

    def track_ended_callback(self) -> None:
        """Callback invocado por el reader thread cuando mpv detecta end-file.

        Este callback se ejecuta en un hilo separado. Sólo marca una bandera
        que el bot verificará en el siguiente handler para avanzar la cola.
        """
        self.queue_advance_needed = True

    def check_queue_advance(self) -> bool:
        """Verifica y limpia la bandera de avance de cola.

        Retorna True si se avanzó la cola, False en caso contrario.
        """
        if self.queue_advance_needed:
            self.queue_advance_needed = False
            return True
        return False

    # --- Resolucion de streams ---------------------------------------------

    def cancel_prefetch(self) -> None:
        """Cancela el prefetch en curso (nuevo /play del usuario)."""
        if self.prefetch_task is not None:
            self.prefetch_task.cancel()
        self.prefetch_task = None
        self.prefetch_basis = None
        self.prefetch_candidate = None
        self.prefetch_resolved = None

    async def stream_for(self, url: str) -> tuple[str, str | None] | None:
        """Stream directo para una URL: del cache si ya se resolvio; si no,
        resuelve en caliente (~1 s) y lo guarda. Idempotente: si otra tarea
        ya lo esta resolviendo, espera a esa en vez de duplicar el trabajo."""
        cached = self.stream_cache.get(url)
        if cached is not None:
            return cached
        if url in self.resolving:
            while url in self.resolving and url not in self.stream_cache:
                await asyncio.sleep(0.1)
            return self.stream_cache.get(url)
        self.resolving.add(url)
        try:
            try:
                resolved = await asyncio.wait_for(
                    asyncio.to_thread(resolve_stream_url, url),
                    timeout=RESOLVE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Resolucion de stream agotada (%ss): %s", RESOLVE_TIMEOUT, url
                )
                resolved = None
        finally:
            self.resolving.discard(url)
        if resolved:
            self.stream_cache[url] = resolved
        return resolved

    async def anticipate_worker(self) -> None:
        """Consume la cola de anticipacion resolviendo un stream por vez."""
        queue = self.anticipate_queue
        if queue is None:
            return
        while True:
            url = await queue.get()
            if url in self.stream_cache or url in self.resolving:
                continue
            try:
                await self.stream_for(url)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - un fallo no corta la cadena
                logger.warning("No se pudo anticipar el stream de %s", url)

    def anticipate_urls(self, urls: list[str]) -> None:
        """Encarga la resolucion por adelantado de varias URLs (el mesonero)."""
        if not urls:
            return
        if self.anticipate_queue is None:
            self.anticipate_queue = asyncio.Queue()
            self.anticipate_task = asyncio.create_task(self.anticipate_worker())
        for url in urls:
            if url not in self.stream_cache and url not in self.resolving:
                self.anticipate_queue.put_nowait(url)

    def clear_stream_cache(self) -> None:
        """Vuelve a vaciar el cache de streams (nueva busqueda o /stop)."""
        self.stream_cache.clear()
        self.resolving.clear()
        # Nuevo /buscar = nuevo catalogo de radio: la lista cacheada del
        # ancla anterior ya no sirve (puede ser otro artista o el mismo).
        self._app._radio_search_cache.clear()
        if self.anticipate_queue is not None:
            while not self.anticipate_queue.empty():
                try:
                    self.anticipate_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    # --- Decision: cual es el siguiente ------------------------------------

    def artist_seed(self, item: QueueItem) -> str:
        """Semilla de la radio: el artista de lo que esta sonando ahora.

        Cada tema lleva su propio artista (`QueueItem.artist`), asi que la
        semilla sigue a la cancion REAL: si el usuario busco "Y", eligio una de
        "Y" y despues se devuelve con /prev a una de "X", la radio sigue con
        "X". Antes se usaba el ancla de sesion a secas y el programa reproducia
        del artista equivocado.

        Cadena de respaldo: artista del tema, luego ancla de la sesion, luego
        el canal de quien subio el video (en playlist/link suele ser el artista
        o su sello). Nunca se re-deriva desde los titulos. Si no hay nada de
        eso, la radio se detiene con aviso en vez de improvisar.
        """
        if item.artist:
            return item.artist
        if self._app._radio_artist:
            return self._app._radio_artist
        if item.channel:
            return item.channel
        return ""

    async def pick_next_candidate(
        self, current: QueueItem
    ) -> tuple[QueueItem | None, bool, bool]:
        """Fase A: decide cual es el siguiente a reproducir.

        Prioridad:
        1. Si la cola (playlist del usuario) tiene items, ese es el siguiente.
        2. Si no, radio por semilla: busca "una parecida" al track actual.

        Devuelve (candidato, vino_de_la_cola, hubo_error_de_red). El tercer
        flag distingue "la radio se acabo" (catalogo agotado) de "no pude
        buscar" (red caida / yt-dlp fallo) para dar un mensaje util al user.
        """
        # La playlist se carga a ventanas. Si la reproduccion llega al final de
        # lo que esta cargado, el peek devolveria un wrap prematuro (la cola
        # aun no tiene el resto): se pide la tanda siguiente y se espera a que
        # llegue. Eso es lo que garantiza que el avance automatico nunca se
        # quede sin tema siguiente. Tope de seguridad: si en PLAYLIST_ESPERAS
        # segundos no llega nada, se sigue con lo que hay.
        _expand_esperas = 0
        while (
            self._app.queue.has_playlist
            and self._app.queue._items
            and self._app.queue._cursor >= len(self._app.queue._items) - 1
            and _expand_esperas < PLAYLIST_ESPERAS
        ):
            if not self._app._playlist_asegurar_ventana():
                break
            _expand_esperas += 1
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
        peeked = self._app.queue.peek(0)
        if peeked is not None:
            return peeked, True, False

        # Radio por semilla: buscar por el ARTISTA que el usuario escribió en la
        # lupa (el ancla de sesion) o, si arranco de un link/playlist, por el
        # canal del video. Sobre el titulo del candidato NO se parsea nada:
        # es un filtro estricto, no una derivacion de artista.
        seed = self.artist_seed(current)
        if not seed:
            return None, False, False
        # La lista de radio se busca UNA vez por ancla de sesion y queda
        # cacheada: cada saltarse eligirá de aquí sin volver a golpear yt-dlp
        # (esa segunda busqueda era el delay perceptible de los botones).
        results = self._app._radio_search_cache.get(seed)
        if results is None:
            # Pedir mas resultados: mas catalogo del artista para poder avanzar
            # sin repetir (la radio busca 50 y elige entre los no-recientes).
            try:
                try:
                    results = await asyncio.wait_for(
                        asyncio.to_thread(search, seed, 50),
                        timeout=RESOLVE_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logging.getLogger(__name__).warning(
                        "Busqueda de radio '%s' agotada (%ss)", seed, RESOLVE_TIMEOUT
                    )
                    return None, False, True
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Error en busqueda de radio '%s': %s", seed, exc
                )
                return None, False, True
            self._app._radio_search_cache[seed] = results

        def _candidate(r) -> tuple[QueueItem, bool, bool]:
            return (
                QueueItem(
                    url=r.url,
                    title=r.title,
                    duration_seconds=r.duration,
                    thumbnail=r.thumbnail,
                    channel=r.channel,
                    artist=seed,
                ),
                False,
                False,
            )

        # Unica pasada, estricta: el candidato tiene que MENCIONAR al artista
        # en su titulo (normalizado) o ser del MISMO canal. Todo lo demas
        # queda fuera, aunque suene parecido (nada de covers importados de
        # otros artistas: si el usuario escribió "GP Band", nunca entra un
        # "Denicher Pol - Inexplicable" por suerte).
        anchor = artist_match.normalizar(seed)
        if not anchor:
            return None, False, False
        for r in results:
            if r.url == current.url or self._app.queue.is_recent(r.url):
                continue
            # Tambien filtrar items que ya estan en la pila de navegacion
            # (prev/next) o en los stacks de esta sesion, para que un
            # next tras un prev no vuelva a algo que ya estaba sonando.
            nav_urls = {i.url for i in self._app._nav_back}
            nav_urls.update(i.url for i in self._app._nav_forward)
            if r.url in nav_urls:
                continue
            if (
                anchor in artist_match.normalizar(r.title)
                or anchor in artist_match.normalizar(r.channel)
            ):
                return _candidate(r)
        return None, False, False

    def radio_over_message(self, por_error: bool = False) -> str:
        """Aviso cuando la radio estricta no encuentra mas temas del artista.

        Args:
            por_error: si es True, el motivo fue un fallo de red y se sugiere
                usar el boton de la lista en vez de asumir que se acabo el catalogo.
        """
        ancla = self._app._radio_artist
        if ancla:
            if por_error:
                return (
                    f"No pude buscar la siguiente cancion de {ancla} "
                    f"(error de red). Usa el boton 📋 de la tarjeta para elegir otro tema."
                )
            return f"Se acabo la radio de {ancla}: no hay mas canciones de este artista en YouTube. Usa el boton 📋 de la tarjeta para elegir otro tema."
        if por_error:
            return "No pude buscar la siguiente cancion (error de red). Usa el boton 📋 de la tarjeta para elegir otro tema."
        return "No encontre otra cancion para la radio. Usa el boton 📋 de la tarjeta."

    # --- Prefetch y avance --------------------------------------------------

    def schedule_prefetch(self, current: QueueItem) -> None:
        """Anticipa el siguiente track (el mesonero no espera).

        Fase A (inmediata): decide el candidato (playlist o radio por semilla).
        Fase B (inmediata tambien): resuelve el stream del candidato enseguida
        y lo deja cacheado, para que /next y el auto-advance no esperen nada.

        Si la URL cacheada vence antes de usarse, se re-resuelve al vuelo.
        """
        self.cancel_prefetch()
        self.prefetch_basis = current.url

        async def _flow() -> None:
            try:
                candidate, from_queue, _ = await self.pick_next_candidate(current)
            except asyncio.CancelledError:
                return
            if candidate is None:
                return
            # el usuario puede interceptar mientras tanto: solo seguimos
            # si seguimos reproduciendo el mismo track.
            if self.prefetch_basis != current.url or self.prefetch_basis is None:
                return
            self.prefetch_candidate = (candidate, from_queue)
            try:
                resolved = await self.stream_for(candidate.url)
            except asyncio.CancelledError:
                return
            if self.prefetch_basis != current.url or self._app.queue.current is not current:
                return
            if resolved:
                self.prefetch_resolved = ((candidate, from_queue), resolved)

        self.prefetch_task = asyncio.create_task(_flow())

    async def advance_after_end(self, context) -> None:
        """Reproduce el siguiente tras end-file.

        Si el prefetch ya resuelto el stream -> reproduce cacheado (cero
        silencio). Si no (duracion desconocida), reproduce por Fase A/B
        directo.
        """
        if self.prefetch_resolved is not None:
            (candidate, from_queue), (stream_url, audio_url) = self.prefetch_resolved
            self.prefetch_resolved = None
            try:
                await self._app.player.start()
                await self._app.player.load(stream_url, audio_url)
                await self._app.player.play()
            except RuntimeError as exc:
                await self._app._reply(None, f"No se pudo continuar: {exc}")
                await self._app._notify_admin(f"Fallo en autoplay: {exc}")
                return
            self._app._push_to_nav_back()
            # Si venia de la playlist, avanzar el cursor (bucle) para no repetir
            # el mismo track en el siguiente prefetch.
            if from_queue:
                self._app.queue.next()
            else:
                self._app.queue.set_current(candidate)
            self._app._persist_dirty()
            self.schedule_prefetch(candidate)
            if self._app._card_chat_id is not None and self._app._card_message_id is not None:
                await self._app._render_card(
                    self._app._track_status_text(), self._app._card_chat_id
                )
            return

        # Sin prefetch listo: reusar el candidato ya decidido (Fase A) si sigue
        # vigente; solo si falla, re-derivar ahora mismo.
        current_item = self._app.queue.current
        if current_item is None:
            return
        candidate, from_queue, error = None, False, False
        if (
            self.prefetch_candidate is not None
            and self.prefetch_basis == current_item.url
        ):
            candidate, from_queue = self.prefetch_candidate
        else:
            candidate, from_queue, error = await self.pick_next_candidate(current_item)
        if candidate is None:
            await self._app._reply(None, self.radio_over_message(por_error=error))
            return
        if from_queue:
            self._app.queue.next()  # avanza el cursor de la playlist (bucle)
        started = await self.play_item(None, candidate, preserve_current=from_queue)
        if started:
            await self._app._reply(None, f"▶️ Siguiente: {candidate.title}")

    async def check_queue_advance_job(self, context) -> None:
        """Job periódico: si la bandera está puesta, avanzar con el prefetch."""
        if self.check_queue_advance():
            await self.advance_after_end(context)

    # --- Reproducir --------------------------------------------------------

    async def play_item(
        self,
        update,
        item: QueueItem,
        *,
        preserve_current: bool = False,
    ) -> bool:
        """Reproduce un video YA, imitando el flujo de YouTube.

        Recibe el QueueItem ya construido (con url, titulo, duracion, thumbnail
        y channel). Resuelve el stream URL, arranca el player si hace falta,
        carga y reproduce al instante. 'loadfile replace' corta cualquier cosa
        que esté sonando (no se encola: el usuario elige y suena en el momento).

        - preserve_current=False (radio / cancion suelta): marca el item como
          actual (set_current) y programa el prefetch del siguiente.
        - preserve_current=True (playlist/jump): la cola ya posiciono el item
          como current; solo reproduce y programa el prefetch.
        """
        self.cancel_prefetch()

        # Resolver el stream: del cache del mesonero si ya se anticipo; si no,
        # resolver en caliente. Si la URL cacheada vencio, mpv la rechaza y se
        # re-resuelve UNA vez abajo (sin TTL preventivos ni esperas).
        resolved = await self.stream_for(item.url)
        if not resolved:
            motivo = last_resolve_error()
            if motivo and "resuelto con client" in motivo:
                # No es un error real: es el testeo interno del client fallback.
                motivo = "todas las variantes de yt-dlp fallaron"
            detalle = f"\nMotivo: {motivo}" if motivo else ""
            await self._app._reply(update, "No se pudo reproducir. Esto suele ser un bloqueo del proveedor de internet o de YouTube a este equipo." + detalle)
            await self._app._notify_admin(
                f"Fallo de resolucion: {item.title}\n{motivo or 'sin motivo detallado'}"
            )
            return False
        stream_url, audio_url = resolved

        # empezar reproduccion desde cero
        try:
            await self._app.player.start()
        except RuntimeError as exc:
            await self._app._reply(update, f"Problema al iniciar el reproductor: {exc}")
            await self._app._notify_admin(f"mpv no arranco: {exc}")
            return False

        for attempt in range(2):
            try:
                await self._app.player.load(stream_url, audio_url)
                await self._app.player.play()
                self._app._paused = False
                if not preserve_current:
                    # Guardar el item actual en la pila de navegación antes
                    # de cambiar el cursor (solo en modo radio).
                    self._app._push_to_nav_back()
                    self._app.queue.set_current(item)
                    self._app._persist_dirty()
                break
            except RuntimeError as exc:
                if attempt == 1 or item.url not in self.stream_cache:
                    await self._app._reply(update, f"No se pudo reproducir: {exc}")
                    await self._app._notify_admin(
                        f"Fallo al reproducir {item.title}: {exc}"
                    )
                    return False
                # URL cacheada vencida: descartar esa entrada, re-resolver y
                # reintentar una vez.
                logger.info("Stream cacheado expirado, re-resolviendo: %s", item.url)
                self.stream_cache.pop(item.url, None)
                resolved = await self.stream_for(item.url)
                if not resolved:
                    await self._app._reply(update, f"No se pudo reproducir: {exc}")
                    return False
                stream_url, audio_url = resolved
        self.schedule_prefetch(item)
        # La tarjeta del mini reproductor refleja el tema nuevo. En acciones
        # del usuario se crea/edita en su chat; en el auto-advance (update
        # None) se re-edita la ultima tarjeta con el titulo nuevo.
        if update is not None:
            await self._app._show_card(update.effective_chat.id)
        elif self._app._card_chat_id is not None and self._app._card_message_id is not None:
            await self._app._render_card(
                self._app._track_status_text(), self._app._card_chat_id
            )
        return True