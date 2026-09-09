"""Bot de Telegram de YT-Remote.

Controla un reproductor mpv local reproduciendo videos de YouTube.
Los comandos se enrutan por roles (admin > dj > user).
"""

from collections import deque

import asyncio
import logging
import os

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Config
from player import Player
from persistence import StateStore
from queue_manager import QueueItem, QueueManager
from roles import VALID_ROLES, RoleManager
from search import SearchResult, is_playlist_url, expand_playlist, quick_playlist, is_youtube_link, search, resolve_stream_url, thumbnail_from_url, last_resolve_error, set_max_height
import setup_cli as setup

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Timeout para cada resolucion de stream (yt-dlp en thread): si tarda mas,
# se corta y se trata como fallo de resolucion. Sin esto, un yt-dlp colgado
# congelaria el polling entero (los handlers de python-telegram-bot corren
# en secuencia) y el bot dejaria de responder botones y comandos.
_RESOLVE_TIMEOUT = 20.0

# Arranque al toque de playlists: los primeros tracks se cargan al instante
# (una sola llamada rapida de yt-dlp con playlistend) y el RESTO se expande
# en segundo plano. El techo tecnico de seguridad: YouTube no permite
# playlists de mas de 5000 items, asi que no hay tope de usuario.
_QUICK_TRACKS = 15
_QUICK_TIMEOUT = 15.0
_MAX_PLAYLIST = 5000
_EXPAND_TIMEOUT = 300.0

# Niveles de calidad disponibles para el admin. 1080 es el tope maximo.
QUALITY_LEVELS = (144, 240, 360, 480, 720, 1080)

# Mensaje de lista (boton 📋): temas visibles por pagina.
_LIST_PAGE_SIZE = 10


class YTRemoteBot:
    """Ensambla bot, player, cola y roles."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.roles = RoleManager()
        self.player = Player(config.mpv_path)
        self.player._on_track_ended = self._mpv_track_ended_callback
        self._queue_advance_needed = False
        self.queue = QueueManager()
        # Ancla de la radio: el ARTISTA fijado UNA sola vez, en el /buscar
        # (todo lo que va antes del guion). /next y el auto-advance lo reusan
        # tal cual, sin re-derivar en cada salto.
        self._radio_artist: str = ""
        # Vigilante de conexion: True mientras el polling esta caido por red
        # (sin internet / Telegram inalcanzable). Al volver, se descarta el
        # backlog acumulado y se avisa al usuario que esos comandos se perdieron.
        self._net_offline: bool = False
        # cache: callback_data -> SearchResult para los botones de busqueda
        self._search_cache: dict[str, SearchResult] = {}
        # True mientras el listado de resultados de /buscar sigue en pantalla.
        # En ese lapso la card NO se reposiciona: queda arriba del listado.
        # Se limpia al elegir, al cancelar o ante un /buscar o /play nuevo.
        self._search_list_pending: bool = False
        # Candado anti-colision de botones: una sola accion de control a la
        # vez. Si ya hay una en curso (p.ej. resolviendo el stream de un
        # salto), los toques extra se descartan al instante en vez de
        # acumularse en la cola del bot (eran los saltos encolados lentos).
        self._control_lock = asyncio.Lock()
        # Cache de la lista de radio: la busqueda de 50 temas se hace UNA vez
        # por ancla de sesion (lo que escribió el usuario en /buscar) y cada
        # /next elige de la lista cacheada en vez de volver a golpear yt-dlp
        # (era el delay real de los botones ⏭/⏮: ~2s de busqueda por salto).
        self._radio_search_cache: dict[str, list] = {}
        # Prefetch (auto-continuacion): candidato a siguiente + stream resuelto
        self._prefetch_task: asyncio.Task | None = None
        self._prefetch_basis: str | None = None
        self._prefetch_candidate: tuple[QueueItem, bool] | None = None
        self._prefetch_resolved: tuple[tuple[QueueItem, bool], tuple[str, str | None]] | None = None
        # Cache de streams (modelo "mesonero"): URL de YouTube -> stream directo
        # resuelto con yt-dlp. Se llena por adelantado (listado de /buscar,
        # candidato de /next, historial de /prev) para que cada boton casi no
        # espere. La resolucion es idempotente y serial (una a la vez, para no
        # pegarle a YouTube en rafagas y evitar el bloqueo "not a bot").
        self._stream_cache: dict[str, tuple[str, str | None]] = {}
        self._resolving: set[str] = set()
        self._anticipate_queue: asyncio.Queue[str] | None = None
        self._anticipate_task: asyncio.Task | None = None
        # Mini reproductor persistente: un solo mensaje editable con botones.
        # El bot rastrea pausa y volumen porque el IPC de mpv no devuelve
        # respuestas a comandos (la respuesta se pierde en el handle efimero).
        self._card_chat_id: int | None = None
        self._card_message_id: int | None = None
        self._card_is_photo: bool = False
        # Tope de resolucion elegido por el admin (None = 1080 por defecto).
        # Se aplica a search.MAX_HEIGHT y se persiste en state.json.
        self._max_height: int | None = None
        # Mensaje de la lista (boton 📋): referencias efimeras (no se
        # persisten, la lista muere al reiniciar) + pagina visible de 10.
        self._list_chat_id: int | None = None
        self._list_message_id: int | None = None
        self._list_page: int = 0
        # Determina si el mensaje de la lista abierto muestra botones de tema
        # (True si lo abrio un admin/dj). Fijo durante la vida del mensaje:
        # al paginar no cambia aunque lo pages un user.
        self._list_can_select: bool = False
        # True mientras la expansion de fondo de una playlist esta activa y
        # False apenas termina. Sirve para no cerrar el listado con wrap
        # prematuro mientras la cola sigue creciendo. Tambien sujeta el total
        # real de la lista (playlist_count) para el feedback de la card.
        self._expanding_playlist: bool = False
        self._expanding_playlist_url: str | None = None
        self._expanding_total: int = 0
        self._expand_task: asyncio.Task | None = None
        # Seria la re-creacion de la tarjeta (borrar + enviar de nuevo): dos
        # updates seguidos no deben borrar dos veces para duplicar la card.
        self._card_lock: asyncio.Lock = asyncio.Lock()
        self._paused: bool = False
        self._volume: int = 100
        # True mientras se ejecuta una accion disparada por un boton de la
        # tarjeta: los reply/errores van como toast (answer) y no se envian
        # fotos de confirmacion, porque la tarjeta se re-renderiza al final.
        self._from_card: bool = False
        # Pila de navegación (estándar en reproductores): _nav_back guarda
        # los temas que dejaron de sonar (para /prev), _nav_forward guarda los
        # temas que se saltaron con /next (para /next tras un /prev). Se
        # actualizan en _play_item y se limpian al cambiar de fuente.
        self._nav_back: deque[QueueItem] = deque(maxlen=100)
        self._nav_forward: deque[QueueItem] = deque(maxlen=100)
        # Persistencia: volumen, pausa, cola, historial y radio artist
        # sobreviven a reinicios. Por defecto arranca en pausa si habia
        # algo sonando (el usuario decide reanudar con /play).
        self._state = StateStore()
        # True si se restauro estado previo (hay una cancion que retomar),
        # para el texto "retoma" en la tarjeta.
        self._restored: bool = False
        self._load_persisted_state()
        self._register_owner()

    def _persist_dirty(self) -> None:
        """Construye el estado actual y lo marca como dirty para guardado."""
        history = [i.to_dict() for i in list(self.queue._history_items)[-100:]]
        self._state._current = {
            "version": 1,
            "volume": self._volume,
            "paused": self._paused,
            "current": self.queue.current.to_dict() if self.queue.current else None,
            "playlist": [i.to_dict() for i in self.queue._items],
            "cursor": self.queue._cursor if self.queue.has_playlist else 0,
            "list_page": self._list_page,
            "history": history,
            "radio_artist": self._radio_artist,
            "max_height": self._max_height,
            "card": {
                "chat_id": self._card_chat_id,
                "message_id": self._card_message_id,
                "is_photo": self._card_is_photo,
            },
        }
        self._state.mark_dirty()

    def _persist_flush(self) -> None:
        """Guardado inmediato: llama save() sin esperar el debounce."""
        self._persist_dirty()
        self._state.flush()

    def _load_persisted_state(self) -> None:
        """Carga el estado persistente y aplica valores al bot.

        Si no hay archivo o esta corrupto, usa defaults:
        - volumen 100, sin pausa, sin cola.
        Al arrancar, la cancion queda en pausa para que el usuario confirme.
        """
        loaded = self._state.load()
        self._volume = loaded.get("volume", 100)
        self._radio_artist = loaded.get("radio_artist", "")
        has_current = bool(loaded.get("current"))
        self._restored = has_current
        # Si se restauro una cancion, arranca en PAUSA aunque el estado
        # guardado dijera "sonando": el autoadvance no corre y el usuario
        # decide reanudar con /play (o el boton ▶ de la tarjeta).
        self._paused = True if has_current else loaded.get("paused", False)
        if loaded.get("current"):
            try:
                self.queue._current = QueueItem.from_dict(loaded["current"])
            except Exception:
                pass
        if loaded.get("playlist"):
            try:
                items = [QueueItem.from_dict(i) for i in loaded["playlist"]]
                self.queue._items = items
                # Restaurar la posicion real dentro de la playlist (con
                # validacion de rango: un cursor corrupto cae a 0).
                try:
                    cursor = int(loaded.get("cursor", 0) or 0)
                except (TypeError, ValueError):
                    cursor = 0
                self.queue._cursor = max(0, min(cursor, len(items) - 1))
            except Exception:
                pass
        if loaded.get("history"):
            try:
                self.queue._history_items = deque(
                    [QueueItem.from_dict(i) for i in loaded["history"][-100:]],
                    maxlen=100,
                )
            except Exception:
                pass
        card = loaded.get("card") or {}
        self._card_chat_id = card.get("chat_id")
        self._card_message_id = card.get("message_id")
        self._card_is_photo = bool(card.get("is_photo"))

        # Pagina del listado 📋 en la que iba el usuario: se restaura para
        # no volver siempre al inicio tras un reinicio (validada >= 0).
        try:
            pagina = int(loaded.get("list_page", 0) or 0)
        except (TypeError, ValueError):
            pagina = 0
        self._list_page = max(0, pagina)

        # Tope de resolucion: un valor persistido invalido (>1080, caida del
        # tope antiguo) se ignora y se vuelve a 1080 por defecto.
        persistido = loaded.get("max_height")
        if persistido in QUALITY_LEVELS:
            self._max_height = persistido
            set_max_height(persistido)
        else:
            self._max_height = None
            set_max_height(1080)

    def _push_to_nav_back(self) -> None:
        """Guarda el item actual en la pila de navegación hacia atrás.

        Se llama antes de cambiar a otro tema (via /next, /prev, /buscar, etc.).
        Siempre limpia la pila forward porque una nueva elección rompe la
        posibilidad de re‑avanzar.
        """
        if self.queue.current is not None:
            self._nav_back.append(self.queue.current)
        self._nav_forward.clear()

    def _pop_from_nav_back(self) -> QueueItem | None:
        """Poppea el último item guardado en la pila hacia atrás."""
        if self._nav_back:
            return self._nav_back.pop()
        return None

    def _pop_from_nav_forward(self) -> QueueItem | None:
        """Poppea el último item guardado en la pila hacia adelante."""
        if self._nav_forward:
            return self._nav_forward.pop()
        return None

    def _clear_nav_stacks(self) -> None:
        """Limpia ambas pilas. Se llama al cambiar de fuente (nueva búsqueda,
        nuevo /play, detener, etc.) para que el historial no se mezcle."""
        self._nav_back.clear()
        self._nav_forward.clear()

    def _register_owner(self) -> None:
        """El dueno (OWNER_ID) queda como admin automaticamente."""
        if self.config.owner_id is None:
            logger.warning("OWNER_ID no configurado en .env: no hay admin inicial.")
            return
        if not self.roles.has_role(self.config.owner_id, "admin"):
            self.roles.set_role(self.config.owner_id, "admin")
            logger.info("Dueno %s registrado como admin.", self.config.owner_id)

    @property
    def app(self) -> Application:
        return self._app

    def build(self) -> Application:
        app = Application.builder().token(self.config.token).build()

        # Comandos principales (el menu "/" se registra en post_init).
        # _with_card_reposition: al terminar cualquier comando, si la tarjeta
        # ya existia, se re-envia al final del chat (la card queda como ultima
        # visualizacion; los mensajes del comando quedan arriba).
        app.add_handler(CommandHandler("start", self._with_card_reposition(self._require_chat(self.cmd_start))))
        app.add_handler(CommandHandler("buscar", self._with_card_reposition(self._require_chat(self._require("dj", self.cmd_play)))))
        app.add_handler(CommandHandler("play", self._with_card_reposition(self._require_chat(self._require("dj", self.cmd_play)))))
        app.add_handler(CommandHandler("solicitar", self._with_card_reposition(self._require_chat(self.cmd_solicitar))))
        app.add_handler(
            CommandHandler(
                "adduser", self._with_card_reposition(self._require_chat(self._require("admin", self.cmd_adduser)))
            )
        )
        app.add_handler(
            CommandHandler(
                "removeuser",
                self._with_card_reposition(self._require_chat(self._require("admin", self.cmd_removeuser))),
            )
        )
        app.add_handler(CallbackQueryHandler(self._require_chat(self.on_callback)))
        # Cualquier mensaje de texto en el chat (alguien escribio algo) re-posiciona
        # la tarjeta como ultimo mensaje. Va despues de los comandos para no
        # interceptarlos; el handler no responde nada.
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._require_chat(self._passive_card_reposition),
            )
        )

        self._app = app

        async def post_init(_app: Application) -> None:
            # Marca de arranque: si ves DOS de estas sin haber reiniciado, hay
            # mas de una instancia del bot con el mismo token (una segunda
            # instancia pierde mensajes en silencio).
            logger.info(
                "BOT ARRANCADO (pid=%s)",
                os.getpid(),
            )
            # Si se restauro estado previo con una cancion y hay tarjeta
            # persistida, se re-edita la tarjeta con el texto "retoma"
            # (la tarjeta queda "viva" al volver al chat).
            if self._restored and self._card_message_id is not None:
                await self._render_card(self._track_status_text())
            # Aviso de comandos perdidos: se lee el backlog ANTES de que el
            # polling lo consuma (post_init corre antes del start).
            await self._notify_pending_dropped(_app.bot)
            # Menú de comandos: al escribir "/" Telegram muestra esta lista.
            bot = _app.bot
            try:
                await bot.set_my_commands(
                    [
                        BotCommand("start", "Info del bot"),
                        BotCommand("buscar", "Buscar artista o link para reproducir (dj+)"),
                        BotCommand("solicitar", "Pedir acceso de dj al admin"),
                        BotCommand("adduser", "Dar acceso (admin)"),
                        BotCommand("removeuser", "Quitar acceso (admin)"),
                    ]
                )
            except Exception as exc:  # noqa: BLE001 - no debe tumbar el arranque
                logger.warning("No se pudo registrar los comandos: %s", exc)

        app.post_init = post_init

        # Job periódico para verificar si hay que avanzar la cola
        # (se ejecuta cada 5 segundos empezando a los 10s).
        if app.job_queue:
            app.job_queue.run_repeating(
                self._check_queue_advance_job,
                interval=5,
                first=10,
            )
            # Vigilante de conexion: detecta la caida de red del polling y,
            # al volver, descarta el backlog acumulado con aviso al usuario.
            app.job_queue.run_repeating(self._net_watch_job, interval=15, first=15)
        # Errores de red del polling marcan el bot como offline.
        app.add_error_handler(self._on_bot_error)
        app.run_polling  # noqa: B018  (validar metodo disponible)
        return app

    async def _notify_pending_dropped(self, bot) -> None:
        """Informa al chat permitido que los comandos enviados mientras el
        bot estuvo apagado se DESCARTARON (no quedaron en cola y no se ejecutan).

        Se llama en post_init, antes de que el polling consuma el backlog.
        Lee sin confirmar (timeout=0), arma el reporte de los comandos viejos
        y luego hace un offset=-1 para descartar TODO el backlog pendiente.
        Si falla la lectura, el bot arranca igual (nadie queda bloqueado).
        """
        if self.config.allowed_chat_id is None:
            return
        try:
            pending = await bot.get_updates(timeout=0)
        except Exception as exc:  # noqa: BLE001 - no debe tumbar el arranque
            logger.warning("No se pudo leer el backlog acumulado: %s", exc)
            return
        try:
            # Sin confirmar el offset, Telegram REENVIARIA todo en el proximo
            # poll; un offset=-1 descarta el backlog completo (lo pendiente NO
            # se ejecuta; se avisa abajo que se perdio).
            await bot.get_updates(offset=-1, timeout=0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo descartar el backlog: %s", exc)

        commands = []
        for upd in pending:
            msg = upd.message
            if msg is None or msg.chat_id != self.config.allowed_chat_id:
                continue
            text = (msg.text or "").strip()
            if text.startswith("/"):
                commands.append(text)
        if not commands:
            return

        shown = commands[:5]
        extra = len(commands) - len(shown)
        report = (
            "🔌 El bot estuvo apagado y lo que enviaste NO quedo en cola ni "
            "se ejecuto. Se descartaron estos comandos:\n"
            + "\n".join(f"• {c}" for c in shown)
            + (f"\n…y {extra} más." if extra else "")
            + "\n\nEnvialos de nuevo si todavia los necesitas."
        )
        try:
            await bot.send_message(self.config.allowed_chat_id, report)
        except Exception as exc:  # noqa: BLE001 - no debe tumbar el arranque
            logger.warning("No se pudo notificar el backlog descartado: %s", exc)

    async def _on_bot_error(self, update: object, context) -> None:
        """Marca el bot como OFFLINE cuando el polling falla por red.

        Errores de red REALES (sin internet, Telegram inalcanzable) ponen la
        bandera que el vigilante (_net_watch_job) usa para avisar al volver.
        Los errores de API (BadRequest y similares) NO cuentan como caida de
        red: en este PTB BadRequest hereda de NetworkError, asi que hay que
        filtrar por la clase exacta para no apagar el bot ante un rechazo
        puntual de Telegram.
        """
        error = context.error
        is_real_network = (
            isinstance(error, (NetworkError, TimedOut))
            and type(error) in (NetworkError, TimedOut)
        )
        if is_real_network:
            self._net_offline = True
            logger.warning("Red del bot caida (%s). Se avisara al volver.", type(error).__name__)
        else:
            logger.error("Error del bot: %r", error)

    async def _net_watch_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Vigilante de conexion: al detectar que la red volvio, descarta el
        backlog acumulado (los comandos enviados sin conexion NO se ejecutan)
        y avisa al usuario que se perdieron, igual que el aviso de arranque.

        Mientras esta offline no hace nada (solo sondea); los comandos que
        llegaron quedan en Telegram hasta que se decide: o se ejecutan o se
        descartan con aviso. Aqui se descartan SIEMPRE: un /stop viejo no debe
        cortar la musica minutos despues de la caida.
        """
        if not self._net_offline:
            return
        try:
            await context.bot.get_me()
        except Exception:  # noqa: BLE001 - todavia sin conexion
            return
        # La conexion volvio: la bandera se apaga en la pausa del updater para
        # que un error a mitad del flush no quede como "offline" para siempre.
        updater = getattr(context.application, "updater", None)
        applied = False
        if updater is not None:
            try:
                await updater.stop()
                applied = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo pausar el updater: %s", exc)
        try:
            self._net_offline = False
            pending = await context.bot.get_updates(timeout=0)
            try:
                # offset=-1 descarta TODO el backlog sin ejecutarlo.
                await context.bot.get_updates(offset=-1, timeout=0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo descartar el backlog: %s", exc)
            await self._notify_connection_lost(context.bot, pending)
        finally:
            if applied and updater is not None:
                try:
                    await updater.start_polling(
                        drop_pending_updates=False,
                        allowed_updates=["message", "callback_query"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("No se pudo reanudar el updater: %s", exc)

    async def _notify_connection_lost(self, bot, pending) -> None:
        """Avisa por escrito que los comandos mandados sin conexion se perdieron."""
        if self.config.allowed_chat_id is None:
            return
        commands = []
        for upd in pending:
            msg = upd.message
            if msg is None or msg.chat_id != self.config.allowed_chat_id:
                continue
            text = (msg.text or "").strip()
            if text.startswith("/"):
                commands.append(text)
        if not commands:
            return
        shown = commands[:5]
        extra = len(commands) - len(shown)
        report = (
            "🔌 La conexion del bot se perdio un rato y lo que enviaste en "
            "ese momento NO se ejecuto. Se descartaron estos comandos:\n"
            + "\n".join(f"• {c}" for c in shown)
            + (f"\n…y {extra} más." if extra else "")
            + "\n\nEnvialos de nuevo si todavia los necesitas."
        )
        try:
            await bot.send_message(self.config.allowed_chat_id, report)
        except Exception as exc:  # noqa: BLE001 - no debe tumbar nada
            logger.warning("No se pudo notificar la conexion perdida: %s", exc)

    def _require(self, role: str, handler):
        """Envuelve un handler exigiendo un rol minimo."""

        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            if user is None:
                return
            if not self.roles.has_role(user.id, role):
                await self._reply(update, f"Acceso denegado: necesitas rol '{role}' para este comando.")
                return
            await handler(update, context)

        wrapper.__name__ = handler.__name__
        return wrapper

    def _chat_allowed(self, update: Update) -> bool:
        """Restringe el bot al chat permitido.

        - Si ALLOWED_CHAT_ID esta definido: solo ese chat.
        - Si no esta definido (primera vez): solo el dueno, hasta que
          el propio /start configure el grupo permitido.
        """
        if self.config.allowed_chat_id is None:
            user = update.effective_user
            return user is not None and user.id == self.config.owner_id
        chat_id = update.effective_chat.id if update.effective_chat else None
        return chat_id == self.config.allowed_chat_id

    def _require_chat(self, handler):
        """Envuelve un handler exigiendo que el chat este permitido."""

        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._chat_allowed(update):
                chat_id = update.effective_chat.id if update.effective_chat else None
                logger.info("Rechazado mensaje de chat no permitido (chat_id=%s).", chat_id)
                return
            await handler(update, context)

        wrapper.__name__ = getattr(handler, "__name__", "wrapper")
        return wrapper

    async def _card_exists_in(self, chat_id: int | None) -> bool:
        return (
            chat_id is not None
            and self._card_chat_id == chat_id
            and self._card_message_id is not None
        )

    def _with_card_reposition(self, handler):
        """Envuelve un handler para que, al terminar, la tarjeta vuelva a ser
        el ULTIMO mensaje del chat.

        Si la tarjeta ya existia antes del handler (el usuario escribio algo
        arriba: comando, texto, respuesta del bot), se borra con su
        desvanecimiento y se re-envia al final: los mensajes quedan arriba y
        la tarjeta pasa a ser la ultima visualizacion. Si el handler creo la
        tarjeta desde cero (no existia), no se reposiciona (ya quedo de ultima).

        Excepcion: mientras el listado de resultados de /buscar sigue en
        pantalla (_search_list_pending), NO se reposiciona: la card queda
        donde esta, arriba del listado, y elegir/cancelar la gestiona el pick.
        """

        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat_id = update.effective_chat.id if update.effective_chat else None
            existia = await self._card_exists_in(chat_id)
            await handler(update, context)
            if existia and await self._card_exists_in(chat_id):
                if self._search_list_pending:
                    # Listado de /buscar visible: la card no se mueve.
                    return
                await self._reposition_card(chat_id)  # type: ignore[arg-type]

        wrapper.__name__ = getattr(handler, "__name__", "wrapper")
        return wrapper

    async def _passive_card_reposition(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Cualquier mensaje de texto de alguien en el chat re-posiciona la
        tarjeta como ultimo mensaje (se desvanece la vieja y aparece abajo).
        No responde nada: el bot solo mantiene la tarjeta a la vista."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        if await self._card_exists_in(chat_id):
            await self._reposition_card(chat_id)  # type: ignore[arg-type]

    async def _reply(self, update, text: str, **kwargs):
        """Envia un mensaje de respuesta, manejando tanto CallbackQuery como Message.

        - Si update es None (auto-advance sin usuario): no responde.
        - Si update.callback_query: responde (answer) y edita el mensaje original.
        - Si update.message: responde normalmente (reply_text), pasando los kwargs.
        """
        if update is None:
            return
        if update.callback_query:
            query = update.callback_query
            if self._from_card:
                # Desde un boton de la tarjeta el mensaje ya va a ser
                # re-renderizado por el handler del control: el texto de
                # estado/error se muestra como toast (response) y el bot
                # no edita el mensaje. Los mensajes de fallo suben como
                # alerta visible (popup) para que el error no pase de largo.
                es_fallo = (
                    text.startswith("No se pudo")
                    or text.startswith("Problema")
                    or text.startswith("mpv")
                    or text.startswith("Error")
                )
                try:
                    await query.answer(text, show_alert=es_fallo)
                except Exception:
                    pass
                return
            try:
                await query.answer()
            except Exception:
                pass
            parse_mode = kwargs.get("parse_mode")
            if parse_mode:
                await query.edit_message_text(text, parse_mode=parse_mode)
            else:
                await query.edit_message_text(text)
        else:
            await update.message.reply_text(text, **kwargs)

    async def _notify_admin(self, text: str, store_log: bool = True) -> None:
        """Escribe un error/aviso en el log y lo manda al dueno por Telegram.
        Sirve como "log de errores" del admin: el propio bot le copia todo
        fallo real (resolucion, arranque de mpv, reproduccion, red) a su chat.
        No requiere update: se puede llamar desde auto-advance sin usuario.
        """
        logger.warning("ADMIN_LOG: %s", text)
        if self.config.owner_id is None or self._app is None:
            return
        try:
            await self._app.bot.send_message(self.config.owner_id, f"⚠️ {text}")
        except Exception as exc:  # noqa: BLE001 - reportar el fallo es best-effort
            logger.warning("ADMIN_LOG no pudo notificar: %s", exc)

    def _thumbnail_for(self, item: QueueItem) -> str:
        """URL de la miniatura del item; la deriva del link si no la trae."""
        if item.thumbnail:
            return item.thumbnail
        return thumbnail_from_url(item.url)

    async def _send_track_card(
        self, update, caption: str, item: QueueItem | None
    ) -> None:
        """Refresca la tarjeta persistente del mini reproductor.

        Es la unica confirmacion visual: miniatura + estado + botones se
        re-renderizan en el mismo mensaje (nada de fotos sueltas duplicadas).
        El caption se usa solo como fallback si no hay item que reflejar.
        """
        if self._from_card:
            # Desde un boton de la tarjeta, el cierre de _on_control ya
            # re-renderiza la tarjeta en su lugar.
            return
        if item is None:
            await self._reply(update, caption)
            return
        chat = update.effective_chat
        if chat is None:
            return
        await self._show_card(chat.id)

    def _truncate(self, text: str, limit: int = 80) -> str:
        """Trunca un texto con elipsis para que no rompa la tarjeta."""
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def _track_status_text(self) -> str:
        """Estado actual para la tarjeta persistente del mini reproductor.

        Linea 1: estado (Sonando/Pausado). Linea 2: titulo (truncado). El
        volumen no va en el texto: vive en la fila de botones de la tarjeta.
        """
        cur = self.queue.current
        if cur is None:
            return "No hay ninguna cancion sonando."
        state = "⏸️ Pausado" if self._paused else "▶️ Sonando"
        text = f"{state}\n🎵 {self._truncate(cur.title)}"
        if self._restored:
            self._restored = False
            text += "\n🔄 Retomada del cierre anterior: usa ▶ para reanudar."
        return text

    async def _render_pending(
        self, title: str, item: QueueItem | None = None
    ) -> None:
        """La tarjeta cambia YA con el titulo nuevo + espera del stream.

        Al tocar ⏭/⏮ sin prefetch, la tarjeta se actualiza al instante con
        "⏳ Cargando…" en vez de quedarse con el titulo anterior hasta que
        yt-dlp termine; el audio entra cuando el stream esté resuelto.

        Recibe el item del candidato para mostrar su miniatura y su titulo
        SIN tocar queue._current: si el stream falla, el estado queda igual
        y la tarjeta refleja la realidad cuando el error se re-renderiza.
        Tambien sirve para avisos de espera sin item (p.ej. expandir una
        playlist): item=None usa la miniatura del tema actual.
        """
        await self._render_card(
            f"⏳ Cargando…\n🎵 {self._truncate(title)}", item=item
        )

    def _control_keyboard(self) -> InlineKeyboardMarkup:
        """Teclado del mini reproductor persistente.

        Fila 1: ⏮ anterior | ▶/⏸ alternar play-pause | ⏭ siguiente | ⏹ detener
        Fila 2: 🔊−10 | <volumen actual> | 🔊+10 | 📋 lista
        Fila 3: ⚙️ Calidad: Np (solo admin puede usarla)
        """
        play_pause = "▶️" if self._paused else "⏸️"
        keyboard = [
            [
                InlineKeyboardButton("⏮", callback_data="ctl:prev"),
                InlineKeyboardButton(play_pause, callback_data="ctl:pp"),
                InlineKeyboardButton("⏭", callback_data="ctl:next"),
                InlineKeyboardButton("⏹", callback_data="ctl:stop"),
            ],
            [
                InlineKeyboardButton("🔊−10", callback_data="ctl:vol-10"),
                InlineKeyboardButton(f"🔊 {self._volume}", callback_data="ctl:vol-info"),
                InlineKeyboardButton("🔊+10", callback_data="ctl:vol+10"),
                InlineKeyboardButton("📋", callback_data="ctl:lista"),
            ],
            [
                InlineKeyboardButton(
                    f"⚙️ Calidad: {(self._max_height or 1080)}p",
                    callback_data="ctl:calidad",
                )
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

    def _quality_keyboard(self) -> InlineKeyboardMarkup:
        """Grilla de calidades para el admin (el vigente se marca con ✓)."""
        current = self._max_height or 1080
        rows: list[list[InlineKeyboardButton]] = []
        current_row: list[InlineKeyboardButton] = []
        for level in QUALITY_LEVELS:
            label = f"{level} ✓" if level == current else str(level)
            current_row.append(
                InlineKeyboardButton(label, callback_data=f"cl:calidad:{level}")
            )
            if len(current_row) == 4:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)
        rows.append(
            [InlineKeyboardButton("❌", callback_data="cl:calidad:cerrar")]
        )
        return InlineKeyboardMarkup(rows)

    async def _render_card(
        self, text: str, chat_id: int | None = None, item: QueueItem | None = None
    ) -> None:
        """Edita el mensaje persistente del mini reproductor (si existe).

        Si la tarjeta es una FOTO, edita miniatura + texto + teclado juntos
        (edit_message_media), asi al pasar de cancion cambia TODO el mensaje
        y no queda la miniatura de la cancion anterior clavada. Si la tarjeta
        es un mensaje de texto (sin miniatura disponible), edita solo el texto.

        No envia una tarjeta nueva: para crear la primera usar _show_card.

        Si se pasa `item`, la miniatura se toma de ese item (feedback de carga
        del candidato); si no, del tema actual. La presentacion nunca altera
        queue._current: ese es estado de dominio y se commitea solo al exito.
        """
        if self._card_chat_id is None or self._card_message_id is None:
            return
        chat_id = chat_id or self._card_chat_id
        bot = self._app.bot
        source = item if item is not None else self.queue.current
        thumb = self._thumbnail_for(source) if source else ""
        try:
            if self._card_is_photo:
                if thumb:
                    await bot.edit_message_media(
                        media=InputMediaPhoto(media=thumb, caption=text),
                        chat_id=chat_id,
                        message_id=self._card_message_id,
                        reply_markup=self._control_keyboard(),
                    )
                else:
                    # Sin miniatura para el tema nuevo: se actualiza solo el
                    # texto (no se puede poner un mensaje de texto sobre una
                    # foto editando el media a vacio).
                    await bot.edit_message_caption(
                        caption=text,
                        chat_id=chat_id,
                        message_id=self._card_message_id,
                        reply_markup=self._control_keyboard(),
                    )
            else:
                await bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=self._card_message_id,
                    reply_markup=self._control_keyboard(),
                )
        except Exception as exc:  # noqa: BLE001 - no debe romper el control
            if "message is not modified" in str(exc):
                # Re-render con el mismo texto/teclado: es un no-op valido.
                return
            logger.warning("No se pudo editar la tarjeta: %s", exc)

    async def _swap_card_keyboard(self, keyboard: InlineKeyboardMarkup) -> None:
        """Cambia SOLO el teclado de la tarjeta (edit_message_reply_markup).

        Ni borra la card ni re-renderiza texto/miniatura: la grilla de calidad
        reemplaza a los controles y al elegir/cerrar vuelven los controles,
        todo sobre el MISMO mensaje (sin desvanecimiento ni recreacion).
        """
        if self._card_chat_id is None or self._card_message_id is None:
            return
        try:
            await self._app.bot.edit_message_reply_markup(
                chat_id=self._card_chat_id,
                message_id=self._card_message_id,
                reply_markup=keyboard,
            )
        except Exception as exc:  # noqa: BLE001 - no debe romper la card
            if "message is not modified" in str(exc):
                return
            logger.warning("No se pudo cambiar el teclado de la tarjeta: %s", exc)

    async def _send_card(self, chat_id: int) -> None:
        """Crea la tarjeta persistente del mini reproductor en un chat.

        Primera creacion: se usa una FOTO (miniatura) con caption + teclado en
        un solo mensaje. Si el tema no tiene miniatura o falla el envio de foto,
        se crea como texto con botones.
        """
        text = self._track_status_text()
        thumb = self._thumbnail_for(self.queue.current) if self.queue.current else ""
        try:
            if thumb:
                try:
                    msg = await self._app.bot.send_photo(
                        chat_id,
                        photo=thumb,
                        caption=text,
                        reply_markup=self._control_keyboard(),
                    )
                    self._card_is_photo = True
                except Exception as exc_photo:  # noqa: BLE001
                    logger.warning("Fallo send_photo, cayendo a send_message: %s", exc_photo)
                    msg = await self._app.bot.send_message(
                        chat_id,
                        text,
                        reply_markup=self._control_keyboard(),
                    )
                    self._card_is_photo = False
            else:
                msg = await self._app.bot.send_message(
                    chat_id,
                    text,
                    reply_markup=self._control_keyboard(),
                )
                self._card_is_photo = False
            self._card_chat_id = chat_id
            self._card_message_id = msg.message_id
        except Exception as exc:  # noqa: BLE001 - no debe romper el control
            logger.warning("No se pudo crear la tarjeta: %s", exc)

    async def _show_card(self, chat_id: int) -> None:
        """Muestra o refresca la tarjeta del mini reproductor en un chat."""
        if self._card_chat_id is None or self._card_message_id is None:
            await self._send_card(chat_id)
            return
        if self._card_chat_id != chat_id:
            await self._send_card(chat_id)
            return
        await self._render_card(self._track_status_text(), chat_id)

    async def _remove_card(self) -> None:
        """Borra la tarjeta persistente si existe (con su desvanecimiento).

        No envia ninguna nueva: deja la card en None para que el siguiente
        _show_card/_send_card cree una fresca al final de la conversacion.
        Seriada con _card_lock y tolerante a mensajes que Telegram no deja
        borrar (viejos).
        """
        async with self._card_lock:
            old_chat = self._card_chat_id
            old_msg = self._card_message_id
            self._card_chat_id = None
            self._card_message_id = None
            if old_msg is not None and old_chat is not None:
                try:
                    await self._app.bot.delete_message(old_chat, old_msg)
                except Exception as exc:  # noqa: BLE001 - no debe romper la card
                    logger.warning("No se pudo borrar la tarjeta vieja: %s", exc)

    async def _reposition_card(self, chat_id: int) -> None:
        """Re-crea la tarjeta como el ULTIMO mensaje del chat.

        Borra la tarjeta vieja (con su animacion de desvanecimiento) y la
        re-envia al final de la conversacion: los mensajes y comandos quedan
        arriba y la tarjeta pasa a ser siempre la ultima visualizacion.

        Si Telegram no deja borrar la vieja (mensaje muy antiguo), se tolera
        el fallo y solo se envía la nueva.
        """
        await self._remove_card()
        await self._send_card(chat_id)

    def _mpv_track_ended_callback(self) -> None:
        """Callback invocado por el reader thread cuando mpv detecta end-file.

        Este callback se ejecuta en un hilo separado. Sólo marca una bandera
        que el bot verificará en el siguiente handler para avanzar la cola.
        """
        self._queue_advance_needed = True

    def _check_queue_advance(self) -> bool:
        """Verifica y limpia la bandera de avance de cola.

        Retorna True si se avanzó la cola, False en caso contrario.
        """
        if self._queue_advance_needed:
            self._queue_advance_needed = False
            return True
        return False

    def _cancel_prefetch(self) -> None:
        """Cancela el prefetch en curso (nuevo /play del usuario)."""
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
        self._prefetch_task = None
        self._prefetch_basis = None
        self._prefetch_candidate = None
        self._prefetch_resolved = None

    async def _stream_for(self, url: str) -> tuple[str, str | None] | None:
        """Stream directo para una URL: del cache si ya se resolvio; si no,
        resuelve en caliente (~1 s) y lo guarda. Idempotente: si otra tarea
        ya lo esta resolviendo, espera a esa en vez de duplicar el trabajo."""
        cached = self._stream_cache.get(url)
        if cached is not None:
            return cached
        if url in self._resolving:
            while url in self._resolving and url not in self._stream_cache:
                await asyncio.sleep(0.1)
            return self._stream_cache.get(url)
        self._resolving.add(url)
        try:
            try:
                resolved = await asyncio.wait_for(
                    asyncio.to_thread(resolve_stream_url, url),
                    timeout=_RESOLVE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Resolucion de stream agotada (%ss): %s", _RESOLVE_TIMEOUT, url
                )
                resolved = None
        finally:
            self._resolving.discard(url)
        if resolved:
            self._stream_cache[url] = resolved
        return resolved

    async def _anticipate_worker(self) -> None:
        """Consume la cola de anticipacion resolviendo un stream por vez."""
        queue = self._anticipate_queue
        if queue is None:
            return
        while True:
            url = await queue.get()
            if url in self._stream_cache or url in self._resolving:
                continue
            try:
                await self._stream_for(url)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - un fallo no corta la cadena
                logger.warning("No se pudo anticipar el stream de %s", url)

    def _anticipate_urls(self, urls: list[str]) -> None:
        """Encarga la resolucion por adelantado de varias URLs (el mesonero)."""
        if not urls:
            return
        if self._anticipate_queue is None:
            self._anticipate_queue = asyncio.Queue()
            self._anticipate_task = asyncio.create_task(self._anticipate_worker())
        for url in urls:
            if url not in self._stream_cache and url not in self._resolving:
                self._anticipate_queue.put_nowait(url)

    def _clear_stream_cache(self) -> None:
        """Vuelve a vaciar el cache de streams (nueva busqueda o /stop)."""
        self._stream_cache.clear()
        self._resolving.clear()
        # Nuevo /buscar = nuevo catalogo de radio: la lista cacheada del
        # ancla anterior ya no sirve (puede ser otro artista o el mismo).
        self._radio_search_cache.clear()
        if self._anticipate_queue is not None:
            while not self._anticipate_queue.empty():
                try:
                    self._anticipate_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    @staticmethod
    def _normalizar(s: str) -> str:
        """Normaliza un texto para comparaciones: minusculas, sin acentos,
        sin puntuacion ni espacios ("Kent Leroy" -> "kentleroy")."""
        import re as _re
        import unicodedata

        s = "".join(
            c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
        )
        return _re.sub(r"[^a-z0-9]", "", s)

    def _artist_seed(self, item: QueueItem) -> str:
        """Semilla de la radio: el artista EXACTO que escribió el usuario.

        Se fija una sola vez, en el /buscar (o, para playlist/link directo,
        con el canal del video: quien sube suele ser el artista o su sello).
        Nunca se re-deriva desde títulos. Si no hay ancla ni canal, la radio
        se detiene con aviso en vez de improvisar con cualquier cancion.
        """
        if self._radio_artist:
            return self._radio_artist
        if item.channel:
            return item.channel
        return ""

    async def _pick_next_candidate(
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
        # Si el arranque al toque sigue expandiendo la playlist en segundo
        # plano y el cursor esta en el ULTIMO tema cargado, el peek devolveria
        # un wrap prematuro (la cola aun no tiene el resto). Espero un rato a
        # que la expansion anexe mas; con un tope de seguridad: si no llega
        # nada en ~30s (expansion lenta o fallo silencioso), la reproduccion
        # sigue con lo que hay (la playlist quedo en eso).
        _expand_esperas = 0
        while (
            self._expanding_playlist
            and self.queue._items
            and self.queue._cursor == len(self.queue._items) - 1
            and _expand_esperas < 30
        ):
            _expand_esperas += 1
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
        peeked = self.queue.peek(0)
        if peeked is not None:
            return peeked, True, False

        # Radio por semilla: buscar por el ARTISTA que el usuario escribió en la
        # lupa (el ancla de sesion) o, si arranco de un link/playlist, por el
        # canal del video. Sobre el titulo del candidato NO se parsea nada:
        # es un filtro estricto, no una derivacion de artista.
        seed = self._artist_seed(current)
        if not seed:
            return None, False, False
        # La lista de radio se busca UNA vez por ancla de sesion y queda
        # cacheada: cada saltarse eligirá de aquí sin volver a golpear yt-dlp
        # (esa segunda busqueda era el delay perceptible de los botones).
        results = self._radio_search_cache.get(seed)
        if results is None:
            # Pedir mas resultados: mas catalogo del artista para poder avanzar
            # sin repetir (la radio busca 50 y elige entre los no-recientes).
            try:
                try:
                    results = await asyncio.wait_for(
                        asyncio.to_thread(search, seed, 50),
                        timeout=_RESOLVE_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logging.getLogger(__name__).warning(
                        "Busqueda de radio '%s' agotada (%ss)", seed, _RESOLVE_TIMEOUT
                    )
                    return None, False, True
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Error en busqueda de radio '%s': %s", seed, exc
                )
                return None, False, True
            self._radio_search_cache[seed] = results

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
        anchor = self._normalizar(seed)
        if not anchor:
            return None, False, False
        for r in results:
            if r.url == current.url or self.queue.is_recent(r.url):
                continue
            # Tambien filtrar items que ya estan en la pila de navegacion
            # (prev/next) o en los stacks de esta sesion, para que un
            # next tras un prev no vuelva a algo que ya estaba sonando.
            nav_urls = {i.url for i in self._nav_back}
            nav_urls.update(i.url for i in self._nav_forward)
            if r.url in nav_urls:
                continue
            if anchor in self._normalizar(r.title) or anchor in self._normalizar(r.channel):
                return _candidate(r)
        return None, False, False

    def _radio_over_message(self, por_error: bool = False) -> str:
        """Aviso cuando la radio estricta no encuentra mas temas del artista.

        Args:
            por_error: si es True, el motivo fue un fallo de red y se sugiere
                usar el boton 📋 en vez de asumir que se acabo el catalogo.
        """
        ancla = self._radio_artist
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

    def _schedule_prefetch(self, current: QueueItem) -> None:
        """Anticipa el siguiente track (el mesonero no espera).

        Fase A (inmediata): decide el candidato (playlist o radio por semilla).
        Fase B (inmediata tambien): resuelve el stream del candidato enseguida
        y lo deja cacheado, para que /next y el auto-advance no esperen nada.
        Si la URL cacheada vence antes de usarse, se re-resuelve al vuelo.
        """
        self._cancel_prefetch()
        self._prefetch_basis = current.url

        async def _flow() -> None:
            try:
                candidate, from_queue, _ = await self._pick_next_candidate(current)
            except asyncio.CancelledError:
                return
            if candidate is None:
                return
            # el usuario puede interceptar mientras tanto: solo seguimos
            # si seguimos reproduciendo el mismo track.
            if self._prefetch_basis != current.url or self._prefetch_basis is None:
                return
            self._prefetch_candidate = (candidate, from_queue)
            try:
                resolved = await self._stream_for(candidate.url)
            except asyncio.CancelledError:
                return
            if (
                self._prefetch_basis != current.url
                or self.queue.current is not current
            ):
                return
            if resolved:
                self._prefetch_resolved = ((candidate, from_queue), resolved)

        self._prefetch_task = asyncio.create_task(_flow())

    async def _advance_after_end(self, context) -> None:
        """Reproduce el siguiente tras end-file.

        Si el prefetch ya resuelto el stream -> reproduce cacheado (cero
        silencio). Si no (duracion desconocida), reproduce por Fase A/B
        directo.
        """
        if self._prefetch_resolved is not None:
            (candidate, from_queue), (stream_url, audio_url) = self._prefetch_resolved
            self._prefetch_resolved = None
            try:
                await self.player.start()
                await self.player.load(stream_url, audio_url)
                await self.player.play()
            except RuntimeError as exc:
                await self._reply(None, f"No se pudo continuar: {exc}")
                await self._notify_admin(f"Fallo en autoplay: {exc}")
                return
            # Si venia de la playlist, avanzar el cursor (bucle) para no repetir
            # el mismo track en el siguiente prefetch.
            if from_queue:
                self.queue.next()
            else:
                self.queue.set_current(candidate)
            self._schedule_prefetch(candidate)
            await self._reply(None, f"▶️ Siguiente: {candidate.title}")
            return

        # Sin prefetch listo: reusar el candidato ya decidido (Fase A) si sigue
        # vigente; solo si falla, re-derivar ahora mismo.
        current_item = self.queue.current
        if current_item is None:
            return
        candidate, from_queue, error = None, False, False
        if (
            self._prefetch_candidate is not None
            and self._prefetch_basis == current_item.url
        ):
            candidate, from_queue = self._prefetch_candidate
        else:
            candidate, from_queue, error = await self._pick_next_candidate(current_item)
        if candidate is None:
            await self._reply(None, self._radio_over_message(por_error=error))
            return
        if from_queue:
            self.queue.next()  # avanza el cursor de la playlist (bucle)
        started = await self._play_item(None, candidate, preserve_current=from_queue)
        if started:
            await self._reply(None, f"▶️ Siguiente: {candidate.title}")

    async def _check_queue_advance_job(self, context) -> None:
        """Job periódico: si la bandera está puesta, avanzar con el prefetch."""
        if self._check_queue_advance():
            await self._advance_after_end(context)

    def help_for_role(self, role: str) -> str:
        """Devuelve el listado de comandos permitidos para un rol."""
        rank = {"user": 0, "dj": 1, "admin": 2}
        level = rank.get(role, 0)

        lines: list[str] = []

        if level >= 1:  # dj o superior
            lines.append("• /buscar — buscar y reproducir un artista o link")

        if level >= 2:  # admin
            lines.append("• /adduser <id> <rol> — dar acceso (dj, admin)")
            lines.append("• /removeuser <id> — quitar acceso")

        return "\n".join(lines)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        chat_id = chat.id if chat else "?"
        chat_title = chat.title if chat and chat.title else "(chat privado)"
        user = update.effective_user
        user_id = user.id if user else "?"
        user_id_int = user.id if user else None
        user_role = self.roles.get_role(user_id_int) if user_id_int is not None else "user"

        # Imprime el ID del chat en la consola para que el usuario pueda copiarlo.
        logger.info(
            "Mensaje /start | chat_id=%s | chat_title=%s | usuario_id=%s | rol=%s",
            chat_id,
            chat_title,
            user_id,
            user_role,
        )
        print(f"[YT-Remote] /start recibido | chat_id={chat_id} | chat={chat_title} | usuario={user_id} | rol={user_role}")

        # Primera configuracion: el dueno configura el grupo permitido.
        user = update.effective_user
        is_owner = user is not None and user.id == self.config.owner_id
        if is_owner and self.config.allowed_chat_id is None:
            allowed = chat.id if chat else None
            if allowed is not None:
                setup.set_allowed_chat_id(allowed)
                self.config.allowed_chat_id = allowed
                logger.info("Grupo permitido configurado: %s", allowed)
                await self._reply(update, "Configurado: este chat quedo habilitado para el bot.\n\n" + "Comandos disponibles para su rol (admin):\n" + self.help_for_role("admin"))
                return

        if not self._chat_allowed(update):
            await self._reply(update, "Este bot no esta habilitado en este chat.")
            return

        await self._reply(update, "YT-Remote activo.\n\n" + f"Comandos disponibles para su rol ({user_role}):\n" + self.help_for_role(user_role))

    async def cmd_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Busca y reproduce en un solo paso desde /buscar.

        /buscar <artista> - <cancion>: lo que va ANTES del primer guion es el
        ARTISTA (cantante, banda, grupo, canal). Queda fijado como ancla de la
        radio (_radio_artist) ANTES de buscar, asi /next y el auto-advance
        siguen de ese artista (el mesonero no espera nadа).
        - Sin texto: muestra el uso.
        - Sin guion: todo el texto ES el artista (se busca tal cual y la
          radio se ancla a eso).
        - Link de YouTube: reproduce directo (video o playlist/mix).
        """
        query = " ".join(context.args or []).strip()
        if not query:
            await self._reply(
                update,
                "Uso: /buscar <artista> - <cancion>.\n"
                "Ej: /buscar GP Band - Inexplicable\n"
                "Tambien puedes pegar un link de YouTube.",
            )
            return
        # Todo /buscar o /play nuevo reemplaza al intento anterior: la card
        # vuelve a poder reposicionarse (un listado nuevo re-setea el flag).
        self._search_list_pending = False
        if is_youtube_link(query):
            await self._play_link_or_playlist(update, context, query)
            return

        artist, _, song = query.partition("-")
        artist = artist.strip()
        song = song.strip()
        if not artist:
            artist = query
            song = ""
        # El artista es EXACTAMENTE lo que escribió el usuario, nunca se
        # deriva de títulos. Queda fijado antes de la busqueda.
        self._radio_artist = artist
        self._persist_dirty()
        search_query = f"{artist} {song}".strip()
        await self._run_search(update, context, search_query)

    async def _play_link_or_playlist(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
    ) -> None:
        """Reproduce un link de YouTube (video directo o playlist/mix).

        Nueva intencion: se descarta el ancla anterior de la radio. En una
        playlist, la radio se ancla conservadoramente al canal del primer
        track (quien sube suele ser el artista o su sello); nunca se inventa
        un artista a partir del titulo.
        """
        self._radio_artist = ""
        self._clear_nav_stacks()
        if is_playlist_url(query):
            chat_id = update.effective_chat.id if update.effective_chat else None
            # Feedback en la tarjeta si existe: no se envia un mensaje que
            # quede clavado en el chat. Si no hay tarjeta, un mensaje efimero
            # que se borra al terminar (exito o fallo) para no ensuciar.
            pending_id = None
            if await self._card_exists_in(chat_id):
                await self._render_card("⏳ Arrancando playlist…")
            else:
                try:
                    pending = await context.bot.send_message(
                        chat_id, "⏳ Arrancando playlist, un momento..."
                    )
                    pending_id = pending.message_id
                except Exception:
                    pending_id = None
            try:
                try:
                    first_tracks, total = await asyncio.wait_for(
                        asyncio.to_thread(quick_playlist, query, _QUICK_TRACKS),
                        timeout=_QUICK_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Arranque de playlist agotado (%ss): %s", _QUICK_TIMEOUT, query
                    )
                    first_tracks, total = [], 0
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error arrancando playlist: %s", exc)
                first_tracks, total = [], 0
            if not first_tracks:
                # Sin arranque rapido no hay playlist: no molestar al usuario
                # con datos parciales, se avisa el fallo como antes.
                if pending_id is not None:
                    try:
                        await context.bot.delete_message(chat_id, pending_id)
                    except Exception:
                        pass
                await self._reply(update, "No se pudo expandir esa lista.")
                if await self._card_exists_in(chat_id):
                    await self._render_card(self._track_status_text())
                return
            if pending_id is not None:
                try:
                    await context.bot.delete_message(chat_id, pending_id)
                except Exception:
                    pass
            items = [
                QueueItem(
                    url=t.url,
                    title=t.title,
                    duration_seconds=t.duration_seconds,
                    thumbnail=t.thumbnail,
                    channel=t.channel,
                )
                for t in first_tracks
            ]
            self.queue.set_playlist(items)
            self._persist_dirty()
            first = items[0]
            self._radio_artist = first.channel or ""
            # El resto de la lista (hasta el techo tecnico) se trae en segundo
            # plano: el usuario ya escucha el primer track mientras carga.
            self._start_playlist_expansion(query, total)
            started = await self._play_item(update, first, preserve_current=True)
            if started:
                await self._reply(
                    update,
                    f"▶️ Playlist ({len(items)}/{total or '…'}): {first.title}\n"
                    f"Tocando YA; el resto se carga en segundo plano (boton 📋 para verla).",
                )
            return

        await context.bot.send_message(
            update.effective_chat.id, "Reproduciendo link directo..."
        )
        item = QueueItem(url=query, title=query, thumbnail=thumbnail_from_url(query))
        started = await self._play_item(update, item)
        if not started:
            return

    def _start_playlist_expansion(self, url: str, total: int) -> None:
        """Lanza la expansion de fondo de una playlist.

        Si ya hay una expansion activa de OTRA playlist, se cancela: el
        anexo de la lista vieja al final de la nueva ensuciaria la cola.
        """
        if self._expanding_playlist and self._expanding_playlist_url != url:
            self._cancel_playlist_expansion()
        self._expanding_playlist_url = url
        self._expanding_total = total
        if self._expanding_playlist:
            return
        self._expanding_playlist = True
        self._expand_task = asyncio.create_task(self._expand_playlist_background(url))

    def _cancel_playlist_expansion(self) -> None:
        """Cancela la expansion en curso (si existe): se usa al arrancar
        otra playlist para que la lista vieja no se anexe a la nueva."""
        if self._expand_task is not None:
            try:
                self._expand_task.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._expand_task = None
        self._expanding_playlist = False
        self._expanding_playlist_url = None
        self._expanding_total = 0

    async def _expand_playlist_background(self, url: str) -> None:
        """Trae el resto de la playlist por detras y lo anexa a la cola.

        Corre como tarea aparte (asyncio) tras el arranque al toque: no
        congela los botones ni la resolucion de streams.
        """
        try:
            extra = await asyncio.wait_for(
                asyncio.to_thread(expand_playlist, url, _MAX_PLAYLIST),
                timeout=_EXPAND_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Expansion de playlist agotada (%ss): %s", _EXPAND_TIMEOUT, url
            )
            extra = []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error expandiendo playlist en segundo plano: %s", exc)
            extra = []
        finally:
            self._expand_task = None

        # Al terminar, la expansion SOLO se deja ver si sigue siendo la misma
        # (misma URL): si el usuario arranco otra playlist (o la cancelo / no
        # trajo nada), la tarea vieja es de datos caducos y no debe tocar la
        # cola nueva. Sea el camino que sea, siempre se cierran los flags.
        vigente = self._expanding_playlist and self._expanding_playlist_url == url
        self._expanding_playlist = False
        self._expanding_playlist_url = None
        self._expanding_total = 0
        if not vigente:
            return
        if not extra:
            return

        # Anexar SOLO lo que no tenemos ya (el quick-load cargo las primeras
        # N y la expansion completa vuelve a incluir las mismas, no duplicar).
        existentes: set[str] = {i.url for i in self.queue._items}
        nuevos = []
        for t in extra:
            if t.url in existentes:
                continue
            nuevos.append(
                QueueItem(
                    url=t.url,
                    title=t.title,
                    duration_seconds=t.duration_seconds,
                    thumbnail=t.thumbnail,
                    channel=t.channel,
                )
            )
        if not self.queue.has_playlist:
            # El usuario ya cambio de cola mientras expandiamos: descartar
            # sin tocar la nueva (nunca pisar una lista que el usuario eligio
            # despues).
            self._expanding_playlist = False
            self._expanding_playlist_url = None
            self._expanding_total = 0
            return
        for item in nuevos:
            self.queue.add(item)
        if nuevos:
            self._persist_dirty()
            logger.info(
                "Playlist expandida en segundo plano: +%d temas (total %d)",
                len(nuevos), len(self.queue._items),
            )
            # Aviso efimero en la card (si existe): la playlist quedo completa
            # y el 📋 ya muestra todos los temas.
            if self._card_chat_id is not None:
                total_actual = len(self.queue._items)
                try:
                    await self._render_card(
                        f"✅ Playlist cargada: {total_actual} temas.",
                        self._card_chat_id,
                    )
                except Exception:  # noqa: BLE001 - puramente informativo
                    pass
        self._expanding_playlist = False
        self._expanding_playlist_url = None
        self._expanding_total = 0

    async def _run_search(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
    ) -> None:
        """Busca en YouTube y muestra el listado de resultados con botones.

        Envia un mensaje "Buscando..." inmediato (asi el usuario no piensa
        que su comando se perdio) y lo EDITA con los resultados en lugar de
        mandar un segundo mensaje: queda un solo mensaje visible en el chat.
        """
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None:
            await self._reply(update, "Buscando...")
            return
        # Mensaje inmediato: el usuario ve que el bot recibio el comando.
        try:
            pending = await context.bot.send_message(
                chat_id, "🔎 Buscando...", parse_mode=ParseMode.HTML
            )
            pending_id = pending.message_id
        except Exception:
            pending_id = None

        # yt-dlp es lento y bloqueante: ejecutarlo en un thread para no
        # congelar el bot mientras busca.
        try:
            results = await asyncio.to_thread(search, query, self.config.max_results)
        except Exception as exc:
            if pending_id is not None:
                try:
                    await context.bot.edit_message_text(
                        f"❌ Error al buscar: {exc}",
                        chat_id=chat_id,
                        message_id=pending_id,
                    )
                except Exception:
                    pass
            else:
                await self._reply(update, f"❌ Error al buscar: {exc}")
            return

        if not results:
            if pending_id is not None:
                try:
                    await context.bot.edit_message_text(
                        "No encontre resultados.",
                        chat_id=chat_id,
                        message_id=pending_id,
                    )
                except Exception:
                    pass
            else:
                await self._reply(update, "No encontre resultados.")
            return

        # Nueva busqueda reemplaza el cache de streams: queda solo con las
        # URLs de los resultados que van a anticiparse abajo.
        self._clear_stream_cache()
        self._search_cache.clear()
        # Mientras el listado esta en pantalla la card NO se reposiciona: la
        # proxima edicion del wrapper se salta para dejar la card arriba, al
        # alcance del ojo, con los resultados debajo. Se limpia en el pick.
        self._search_list_pending = True
        keyboard = []
        for i, r in enumerate(results):
            cb = f"pick:{i}"
            self._search_cache[cb] = r
            label = f"{i+1}. {r.title} ({r.duration})"
            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])
        # Boton de salida: si el listado no se elige, cancelar lo borra (con
        # su desvanecimiento) y la card que estaba arriba queda visible.
        keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="pick:cancel")])

        # El mesonero: se anticipan los streams de los resultados en la cola
        # de resolucion serial, para que elegir uno suene casi al instante.
        self._anticipate_urls([r.url for r in results])

        reply_markup = InlineKeyboardMarkup(keyboard)
        # Editamos el mismo mensaje "Buscando..." con los resultados: queda
        # un solo mensaje en el chat, no dos.
        try:
            if pending_id is not None:
                await context.bot.edit_message_text(
                    f"Resultados para '{query}':",
                    chat_id=chat_id,
                    message_id=pending_id,
                    reply_markup=reply_markup,
                )
            else:
                await context.bot.send_message(
                    chat_id,
                    f"Resultados para '{query}':",
                    reply_markup=reply_markup,
                )
        except Exception:
            # Si la edicion falla (mensaje viejo, race condition), mandamos
            # uno nuevo para no dejar al usuario sin respuesta.
            await context.bot.send_message(
                chat_id,
                f"Resultados para '{query}':",
                reply_markup=reply_markup,
            )

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query.data and query.data.startswith("ctl:"):
            # Un boton de la tarjeta persistente del mini reproductor.
            await self._on_control(update, context, query.data.split(":", 1)[1])
            return
        if query.data and query.data.startswith("cl:calidad:"):
            # Selector de calidad de la tarjeta (cl:calidad:N / cl:calidad:cerrar).
            await self._on_quality_callback(update, context)
            return
        if query.data and query.data.startswith("lst:"):
            # Botones del mensaje de la lista (temas, paginacion, cerrar).
            await self._on_list_callback(update, context)
            return
        if query.data == "pick:cancel":
            # Cancelar el listado de /buscar: se borra (con su desvanecimiento)
            # sin reproducir nada y sin mover la card. El listado queda limpio
            # y el cache se descarta; la card que estaba arriba queda visible.
            self._search_list_pending = False
            self._search_cache.clear()
            await query.answer("Busqueda cancelada.")
            try:
                await query.message.delete()
            except Exception as exc:  # noqa: BLE001 - no romper el flujo
                logger.warning("No se pudo borrar el listado al cancelar: %s", exc)
            return
        await query.answer()
        if not query.data or not query.data.startswith("pick:"):
            return
        result = self._search_cache.get(query.data)
        if result is None:
            await query.edit_message_text("Esa busqueda ya expiro, busca de nuevo.")
            return

        # El usuario eligio un resultado: el listado deja de estar en pantalla.
        self._search_list_pending = False

        # Confirmar la eleccion: la tarjeta persistente (mini reproductor con
        # miniatura + estado + botones) ES la unica confirmacion; _play_item
        # la crea/actualiza al reproducir. Se quita el listado de resultados
        # para no dejar el titulo duplicado en el chat.
        try:
            await query.message.delete()
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo borrar el listado de resultados: %s", exc)
            try:
                await query.edit_message_text("▶️ Listo, reproduciendo...")
            except Exception as exc2:  # noqa: BLE001
                logger.warning("No se pudo editar mensaje tras fallo de borrado: %s", exc2)

        # Forzar creación de tarjeta fresca: borrar la card previa (con su
        # desvanecimiento) para que _play_item cree la nueva al final de la
        # conversación. Antes quedaba huérfana porque solo se resetaban los
        # ids, sin borrar el mensaje.
        await self._remove_card()
        # Nueva intención: limpiar stacks de navegación.
        self._clear_nav_stacks()

        # ANCLA DE LA RADIO: ya quedó fijada en el /buscar, con el texto que va
        # antes del guion. Aquí solo se propaga al item; el artista NUNCA se
        # re-deriva desde títulos ni desde el canal.
        artist = self._radio_artist

        item = QueueItem(
            url=result.url,
            title=result.title,
            duration_seconds=result.duration_seconds,
            thumbnail=result.thumbnail,
            channel=result.channel,
            artist=artist,
        )
        started = await self._play_item(update, item)
        if not started:
            return

    async def _play_item(
        self,
        update: Update,
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
        self._cancel_prefetch()

        # Resolver el stream: del cache del mesonero si ya se anticipo; si no,
        # resolver en caliente. Si la URL cacheada vencio, mpv la rechaza y se
        # re-resuelve UNA vez abajo (sin TTL preventivos ni esperas).
        resolved = await self._stream_for(item.url)
        if not resolved:
            motivo = last_resolve_error()
            if motivo and "resuelto con client" in motivo:
                # No es un error real: es el testeo interno del client fallback.
                motivo = "todas las variantes de yt-dlp fallaron"
            detalle = f"\nMotivo: {motivo}" if motivo else ""
            await self._reply(update, "No se pudo reproducir. Esto suele ser un bloqueo del proveedor de internet o de YouTube a este equipo." + detalle)
            await self._notify_admin(
                f"Fallo de resolucion: {item.title}\n{motivo or 'sin motivo detallado'}"
            )
            return False
        stream_url, audio_url = resolved

        # empezar reproduccion desde cero
        try:
            await self.player.start()
        except RuntimeError as exc:
            await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
            await self._notify_admin(f"mpv no arranco: {exc}")
            return False

        for attempt in range(2):
            try:
                await self.player.load(stream_url, audio_url)
                await self.player.play()
                self._paused = False
                if not preserve_current:
                    # Guardar el item actual en la pila de navegación antes
                    # de cambiar el cursor (solo en modo radio).
                    self._push_to_nav_back()
                    self.queue.set_current(item)
                    self._persist_dirty()
                break
            except RuntimeError as exc:
                if attempt == 1 or item.url not in self._stream_cache:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    await self._notify_admin(f"Fallo al reproducir {item.title}: {exc}")
                    return False
                # URL cacheada vencida: descartar esa entrada, re-resolver y
                # reintentar una vez.
                logger.info("Stream cacheado expirado, re-resolviendo: %s", item.url)
                self._stream_cache.pop(item.url, None)
                resolved = await self._stream_for(item.url)
                if not resolved:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    return False
                stream_url, audio_url = resolved
        self._schedule_prefetch(item)
        # La tarjeta del mini reproductor refleja el tema nuevo. En acciones
        # del usuario se crea/edita en su chat; en el auto-advance (update
        # None) se re-edita la ultima tarjeta con el titulo nuevo.
        if update is not None:
            await self._show_card(update.effective_chat.id)
        elif self._card_chat_id is not None and self._card_message_id is not None:
            await self._render_card(self._track_status_text(), self._card_chat_id)
        return True

    async def _load_url(
        self, update: Update, url: str, caption: str | None = None
    ) -> None:
        # Delegamos la lógica core en _play_item y nos quedamos solo
        # en la respuesta final (el "▶️ Reproduciendo:").
        item = QueueItem(
            url=url, title=caption or url, thumbnail=thumbnail_from_url(url)
        )
        started = await self._play_item(update, item)
        if started:
            await self._reply(update, f"▶️ Reproduciendo: {caption or url}")

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.queue.current is None or not self.player.is_running:
            await self._reply(update, "No hay ninguna reproduccion activa.")
            return
        await self.player.pause()
        self._paused = True
        self._persist_dirty()
        await self._reply(update, "⏸️ Pausado")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.queue.current is None:
            await self._reply(update, "No hay ninguna reproduccion activa.")
            return
        # Si mpv no esta corriendo (tras apagado de la PC o tras un stop),
        # reproducir el item actual por _play_item: prende mpv y carga el stream.
        if not self.player.is_running:
            started = await self._play_item(update, self.queue.current)
            if started:
                await self._send_track_card(
                    update, f"▶️ Reanudando: {self.queue.current.title}", self.queue.current
                )
            return
        await self.player.play()
        self._paused = False
        self._persist_dirty()
        await self._reply(update, "▶️ Reanudando")

    async def _toggle_play_pause(self, update: Update | None = None) -> None:
        """Alterna reproducir/pausar (boton ▶/⏸ de la tarjeta).

        Si hay un item actual pero mpv no esta corriendo (tras apagado de la
        PC o tras un stop), reproduce ese item por _play_item: eso prende mpv
        y carga el stream. Si mpv ya corre, solo alterna pausa/reproduccion.
        """
        if self.queue.current is None:
            return
        if not self.player.is_running:
            # Con playlist activa, reanudar NO debe descartar la cola: el item
            # actual ya esta posicionado (items[cursor]). Sin playlist (radio /
            # cancion suelta) sigue el camino de set_current de siempre.
            await self._play_item(
                update,
                self.queue.current,
                preserve_current=self.queue.has_playlist,
            )
            return
        if self._paused:
            await self.player.play()
            self._paused = False
        else:
            await self.player.pause()
            self._paused = True
        self._persist_dirty()

    async def _on_control(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        action: str,
    ) -> None:
        """Maneja los botones de la tarjeta persistente (callback ctl:*).

        Cada accion reusa la logica de los comandos equivalentes, pero con
        _from_card activo: no se mandan fotos de confirmacion y los mensajes
        de estado/error van como toast. Al final la tarjeta se re-renderiza
        en su lugar para reflejar siempre el estado actual.
        """
        query = update.callback_query
        user = query.from_user
        user_id = user.id if user else None
        role = self.roles.get_role(user_id) if user_id else "user"

        CONTROL_ACTIONS = {"pp", "prev", "next", "stop", "vol-10", "vol+10", "vol-info"}
        if action in CONTROL_ACTIONS and user_id is not None and not self.roles.has_role(user_id, "dj"):
            await query.answer("No tiene permiso para eso.", show_alert=True)
            return

        self._card_chat_id = update.effective_chat.id
        self._card_message_id = query.message.message_id
        chat_id = self._card_chat_id

        # Candado anti-colision: si una accion de boton ya esta en curso
        # (p.ej. resolviendo el stream de un salto), los toques extra que
        # llegan mientras tanto se DESCARTAN al instante y no se encolan.
        # Esa cola acumulada era la otra mitad del delay percibido: el bot
        # procesaba un salto lento tras otro. Un boton a la vez.
        if self._control_lock.locked():
            try:
                await query.answer("⏳ Un momento, primero termina...")
            except Exception:
                pass
            return

        async with self._control_lock:
            self._from_card = True
            reflect_status = True
            # Respuesta muda al toque: apaga el spinner del boton al instante
            # sin mostrar toast. Todo el feedback de carga vive en la tarjeta
            # (ver _render_pending); el texto aparece solo en los errores.
            try:
                await query.answer()
            except Exception:
                pass
            try:
                if action == "pp":
                    await self._toggle_play_pause(update)
                elif action == "prev":
                    await self.cmd_prev(update, context)
                elif action == "next":
                    await self.cmd_next(update, context)
                elif action == "stop":
                    await self.cmd_stop(update, context)
                elif action in ("vol-10", "vol+10"):
                    delta = -10 if action == "vol-10" else 10
                    self._volume = max(0, min(100, self._volume + delta))
                    if self.player.is_running:
                        await self.player.set_volume(self._volume)
                    self._persist_dirty()
                elif action == "vol-info":
                    # Boton de estado: el volumen ya se lee en su etiqueta.
                    await query.answer(f"Volumen: {self._volume}%")
                    reflect_status = False
                elif action == "lista":
                    # El boton 📋 abre la lista como MENSAJE aparte en el
                    # chat: la card NO se pisa (queda con su estado intacto).
                    if chat_id is None:
                        return
                    await self._open_queue_list(query, user_id, chat_id)
                    reflect_status = False  # la card no se re-renderiza
                elif action == "calidad":
                    # Solo admin puede abrir el selector de calidad.
                    if user_id is None or not self.roles.has_role(user_id, "admin"):
                        await query.answer("Solo el admin puede cambiar la calidad.", show_alert=True)
                        return
                    # Solo cambia el teclado (la grilla reemplaza a los
                    # botones de control); el texto/miniatura no se tocan.
                    await self._swap_card_keyboard(self._quality_keyboard())
                    reflect_status = False  # no pisa el selector abierto
                else:
                    return
            finally:
                self._from_card = False

            # Estado final: el texto de la tarjeta refleja el cambio de la
            # accion (nuevo tema, pausa, volumen, detenido, etc.).
            if reflect_status:
                await self._render_card(self._track_status_text(), chat_id)

    async def _on_quality_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Maneja el selector de calidad de la tarjeta (callback `cl:calidad:*`).

        Solo admin. Se serializa con el MISMO `_control_lock` que los botones de
        control: un toque de calidad jamas se pisa con un next/prev. Si otra
        accion corre, se descarta al instante.

        `cl:calidad:N` aplica el nivel (persistido en `state.json`, aplicado a
        `search.MAX_HEIGHT`), invalida el cache de streams/prefetch y, si hay una
        canción sonando, la recarga con la nueva resolución. `cl:calidad:cerrar`
        descarta sin guardar nada.

        Elegir el nivel ya activo equivale a cerrar: solo vuelve los botones de
        control, sin recargar ni invalidar el cache.

        Al aplicar (o cerrar, o repetir), la card NO se borra ni se recrea: se
        restaura el teclado de control sobre el MISMO mensaje (`_swap_card_keyboard`).
        """
        query = update.callback_query
        user_id = query.from_user.id if query.from_user else None
        if user_id is not None and not self.roles.has_role(user_id, "admin"):
            await query.answer("Solo el admin puede cambiar la calidad.", show_alert=True)
            return

        if self._control_lock.locked():
            try:
                await query.answer("⏳ Un momento, primero termina...")
            except Exception:
                pass
            return

        async with self._control_lock:
            self._from_card = True
            try:
                data = query.data or ""
                level_str = (
                    data.split(":", 2)[2] if data.startswith("cl:calidad:") else ""
                )
                self._card_chat_id = update.effective_chat.id
                self._card_message_id = query.message.message_id
                try:
                    await query.answer()
                except Exception:
                    pass

                if level_str == "cerrar":
                    await self._swap_card_keyboard(self._control_keyboard())
                    return

                try:
                    level = int(level_str)
                except ValueError:
                    return
                if level not in QUALITY_LEVELS:
                    return

                # Elegir el nivel VIGENTE equivale a cerrar: no recargar, no
                # invalidar cache, no tocar nada. Solo vuelve los botones.
                if level == (self._max_height or 1080):
                    await self._swap_card_keyboard(self._control_keyboard())
                    return

                self._max_height = level
                set_max_height(level)
                self._persist_dirty()
                try:
                    await query.answer(f"Calidad: {level}p ✓")
                except Exception:
                    pass

                # Cambio YA: invalidar el mesonero y el prefetch para que nada
                # siga resolviendo con la calidad vieja, y recargar el tema
                # actual con la nueva resolucion (si hay algo sonando).
                self._clear_stream_cache()
                self._cancel_prefetch()
                current = self.queue.current
                if current is not None:
                    try:
                        resolved = await self._stream_for(current.url)
                        if not resolved:
                            await self._notify_admin(
                                f"No se pudo recargar '{current.title}' con calidad {level}p."
                            )
                        else:
                            stream_url, audio_url = resolved
                            await self.player.start()
                            await self.player.load(stream_url, audio_url)
                            await self.player.play()
                            self._paused = False
                    except Exception as exc:  # noqa: BLE001 - no romper la card
                        logger.warning("Fallo la recarga tras cambiar calidad: %s", exc)
                        await self._notify_admin(
                            f"No se pudo recargar '{current.title}' a {level}p: {exc}"
                        )

                # La card NO se borra ni se recrea: se restaura el MISMO mensaje
                # con el teclado de control estandar (ya mostrando el nuevo nivel
                # en el boton ⚙️), sin desvanecimiento ni efecto de recreacion.
                await self._swap_card_keyboard(self._control_keyboard())
            finally:
                self._from_card = False

    async def cmd_next(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Salta YA al siguiente tema: corta el actual y reproduce el siguiente.

        Modo playlist: avanza el cursor con wrap (bucle).
        Modo radio: si hay algo en la pila forward (porque se hizo /prev),
        reproduce ese item directamente. Si no, decide un candidato nuevo
        (prefetch ya resuelto -> cero silencio; o prefetch candidato; o
        busqueda nueva por semilla).
        """
        if self.queue.current is None:
            await self._reply(update, "No hay ninguna cancion reproduciendose.")
            return

        # Modo playlist: cursor con wrap (logica previa).
        if self.queue.has_playlist:
            self._push_to_nav_back()  # guardar el actual en la pila back
            if self._prefetch_resolved is not None:
                (candidate, from_queue), (stream_url, audio_url) = self._prefetch_resolved
                self._prefetch_resolved = None
                self._cancel_prefetch()
                try:
                    await self.player.start()
                    await self.player.load(stream_url, audio_url)
                    await self.player.play()
                    self._paused = False
                except RuntimeError as exc:
                    await self._reply(update, f"No se pudo saltar: {exc}")
                    return
                if from_queue:
                    self.queue.next()
                else:
                    self.queue.set_current(candidate)
                self._schedule_prefetch(candidate)
                await self._send_track_card(
                    update, f"⏭️ Siguiente: {candidate.title}", candidate
                )
                return

            current_item = self.queue.current
            candidate, from_queue, error = None, False, False
            if (
                self._prefetch_candidate is not None
                and self._prefetch_basis == current_item.url
            ):
                candidate, from_queue = self._prefetch_candidate
            else:
                candidate, from_queue, error = await self._pick_next_candidate(current_item)
            if candidate is None:
                await self._reply(update, self._radio_over_message(por_error=error))
                return
            if from_queue:
                self.queue.next()
            started = await self._play_item(update, candidate, preserve_current=from_queue)
            if not started:
                await self._reply(update, "No se pudo reproducir el siguiente tema.")
                return
            await self._send_track_card(
                update, f"⏭️ Siguiente: {candidate.title}", candidate
            )
            return

        # Modo radio (sin playlist).
        # 1) Si hay algo en el stack forward (por un /prev previo), reproducirlo.
        if self._nav_forward:
            item = self._nav_forward.pop()
            # Navegación pura: no alteramos los stacks de navegación más de
            # lo necesario. Seteamos el queue._current directo para que el
            # anti-ping-pong (history) no se accione aquí (solo al elegir
            # un tema nuevo desde búsqueda). El _note_played se omite aquí
            # intencionalmente: el flujo de navegación no entra al histórico
            # de "ya reproducidos" de la radio.
            self._cancel_prefetch()
            await self._render_pending(item.title, item)
            resolved = await self._stream_for(item.url)
            if not resolved:
                await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
                await self._notify_admin(f"Fallo de resolucion en /next (forward): {item.title}")
                return
            stream_url, audio_url = resolved
            try:
                await self.player.start()
            except RuntimeError as exc:
                await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
                await self._notify_admin(f"mpv no arranco en /next: {exc}")
                return
            for attempt in range(2):
                try:
                    await self.player.load(stream_url, audio_url)
                    await self.player.play()
                    self._paused = False
                    self.queue._current = item
                    self.queue._items = []
                    self.queue._cursor = 0
                    break
                except RuntimeError as exc:
                    if attempt == 1 or item.url not in self._stream_cache:
                        await self._reply(update, f"No se pudo reproducir: {exc}")
                        await self._notify_admin(f"Fallo al reproducir {item.title}: {exc}")
                        return
                    logger.info("Stream cacheado expirado, re-resolviendo: %s", item.url)
                    self._stream_cache.pop(item.url, None)
                    resolved = await self._stream_for(item.url)
                    if not resolved:
                        await self._reply(update, f"No se pudo reproducir: {exc}")
                        await self._notify_admin(f"Fallo al re-resolver {item.title}: {exc}")
                        return
                    stream_url, audio_url = resolved
            self._schedule_prefetch(item)
            if update is not None:
                await self._show_card(update.effective_chat.id)
            await self._send_track_card(
                update, f"⏭️ Siguiente: {item.title}", item
            )
            return

        # 2) Sin pila forward: comportamiento estandar (prefetch o candidato nuevo).
        self._push_to_nav_back()
        # Prefetch ya resuelto -> salto instantaneo (cero silencio).
        if self._prefetch_resolved is not None:
            (candidate, from_queue), (stream_url, audio_url) = self._prefetch_resolved
            self._prefetch_resolved = None
            self._cancel_prefetch()
            try:
                await self.player.start()
                await self.player.load(stream_url, audio_url)
                await self.player.play()
                self._paused = False
            except RuntimeError as exc:
                await self._reply(update, f"No se pudo saltar: {exc}")
                await self._notify_admin(f"Fallo al saltar en /next: {exc}")
                return
            # En radio, from_queue siempre es False. Navegacion pura: seteamos
            # _current directo para no duplicar el push al stack (ya lo hicimos).
            self.queue._current = candidate
            self.queue._items = []
            self.queue._cursor = 0
            self._schedule_prefetch(candidate)
            await self._send_track_card(
                update, f"⏭️ Siguiente: {candidate.title}", candidate
            )
            return

        # Sin stream cacheado aun: usar el candidato que la anticipacion decidio,
        # o buscar uno nuevo por semilla.
        current_item = self.queue.current
        candidate, from_queue, error = None, False, False
        if (
            self._prefetch_candidate is not None
            and self._prefetch_basis == current_item.url
        ):
            candidate, from_queue = self._prefetch_candidate
        else:
            candidate, from_queue, error = await self._pick_next_candidate(current_item)
        if candidate is None:
            await self._reply(update, self._radio_over_message(por_error=error))
            return
        # Navegacion pura: seteamos _current directo para evitar el doble push
        # al stack de navegacion (ya se guardo el item en _push_to_nav_back).
        # El commit del estado se hace SOLO tras el load exitoso (abajo):
        # si resolver o reproducir falla, queue._current sigue apuntando al
        # tema anterior. La miniatura del candidato va por _render_pending
        # (item explicito) sin tocar el estado del dominio.
        await self._render_pending(candidate.title, candidate)
        resolved = await self._stream_for(candidate.url)
        if not resolved:
            await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
            await self._notify_admin(f"Fallo de resolucion en /next: {candidate.title}")
            return
        stream_url, audio_url = resolved
        try:
            await self.player.start()
        except RuntimeError as exc:
            await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
            await self._notify_admin(f"mpv no arranco en /next: {exc}")
            return
        for attempt in range(2):
            try:
                await self.player.load(stream_url, audio_url)
                await self.player.play()
                self._paused = False
                break
            except RuntimeError as exc:
                if attempt == 1 or candidate.url not in self._stream_cache:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    await self._notify_admin(f"Fallo al reproducir {candidate.title}: {exc}")
                    return
                logger.info("Stream cacheado expirado, re-resolviendo: %s", candidate.url)
                self._stream_cache.pop(candidate.url, None)
                resolved = await self._stream_for(candidate.url)
                if not resolved:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    await self._notify_admin(f"Fallo al re-resolver {candidate.title}: {exc}")
                    return
                stream_url, audio_url = resolved
        # Audio arrancando: recien aqui se commitea el nuevo tema en la cola.
        self.queue._current = candidate
        self.queue._items = []
        self.queue._cursor = 0
        self._schedule_prefetch(candidate)
        if update is not None:
            await self._show_card(update.effective_chat.id)
        await self._send_track_card(
            update, f"⏭️ Siguiente: {candidate.title}", candidate
        )

    async def cmd_prev(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Retrocede al tema anterior.

        Modo playlist: cursor con wrap (simetrico a /next).
        Modo radio: si hay algo en la pila back, poppea y lo reproduce (el
        actual pasa al forward stack para que /next pueda volver a el).
        Fallback: si la pila back esta vacia, usa el historico de items
        que dejaron de sonar (queue.previous()).
        """
        if self.queue.current is None:
            await self._reply(update, "No hay ninguna cancion reproduciendose.")
            return

        # Modo playlist: cursor con wrap (logica previa).
        if self.queue.has_playlist:
            item = self.queue.previous()
            if item is None:
                await self._reply(update, "No hay cancion anterior en la cola.")
                return
            from_playlist = True
            started = await self._play_item(update, item, preserve_current=from_playlist)
            if started:
                await self._send_track_card(
                    update, f"⏮️ Anterior: {item.title}", item
                )
            return

        # Modo radio.
        # Intentar sacar el item anterior de la pila de navegacion (nav_back).
        # Si esta vacia, fallback al historial de items de la cola.
        item: QueueItem | None = self._pop_from_nav_back()
        used_nav_back = item is not None
        if not used_nav_back:
            # Fallback: usar el historico de items que dejaron de sonar.
            # No es navegacion pura: el nav_back queda igual y el nav_forward
            # tampoco se mezcla. _play_item (con set_current) hara un push
            # normal a nav_back por nosotros.
            item = self.queue.previous()
        if item is None:
            await self._reply(update, "No hay cancion anterior en la cola.")
            return
        if used_nav_back:
            # Guardar el actual en forward: si el usuario hace /next despues,
            # debe volver al item que se esta abandonando.
            if self.queue.current is not None:
                self._nav_forward.append(self.queue.current)
            # Reproducir el item sin pasar por _play_item (que haria un push
            # extra a back, duplicando el item). Manejo manual.
            self._cancel_prefetch()
            await self._render_pending(item.title, item)
            resolved = await self._stream_for(item.url)
            if not resolved:
                await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
                await self._notify_admin(f"Fallo de resolucion en /prev (back): {item.title}")
                return
            stream_url, audio_url = resolved
            try:
                await self.player.start()
            except RuntimeError as exc:
                await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
                await self._notify_admin(f"mpv no arranco en /prev: {exc}")
                return
            for attempt in range(2):
                try:
                    await self.player.load(stream_url, audio_url)
                    await self.player.play()
                    self._paused = False
                    self.queue._current = item
                    self.queue._items = []
                    self.queue._cursor = 0
                    break
                except RuntimeError as exc:
                    if attempt == 1 or item.url not in self._stream_cache:
                        await self._reply(update, f"No se pudo reproducir: {exc}")
                        await self._notify_admin(f"Fallo al reproducir {item.title}: {exc}")
                        return
                    logger.info("Stream cacheado expirado, re-resolviendo: %s", item.url)
                    self._stream_cache.pop(item.url, None)
                    resolved = await self._stream_for(item.url)
                    if not resolved:
                        await self._reply(update, f"No se pudo reproducir: {exc}")
                        await self._notify_admin(f"Fallo al re-resolver {item.title}: {exc}")
                        return
                    stream_url, audio_url = resolved
            self._schedule_prefetch(item)
            if update is not None:
                await self._show_card(update.effective_chat.id)
            await self._send_track_card(
                update, f"⏮️ Anterior: {item.title}", item
            )
            return
# Fallback: item del historico de cola. No llamamos a _play_item
        # (que hace _push_to_nav_back y limpiaria nav_forward). En su lugar,
        # hacemos el manejo manual directo para preservar los stacks de navegacion.
        self._cancel_prefetch()
        await self._render_pending(item.title, item)
        resolved = await self._stream_for(item.url)
        if not resolved:
            await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
            await self._notify_admin(f"Fallo de resolucion en /prev: {item.title}")
            return
        stream_url, audio_url = resolved
        try:
            await self.player.start()
        except RuntimeError as exc:
            await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
            await self._notify_admin(f"mpv no arranco en /prev: {exc}")
            return
        for attempt in range(2):
            try:
                await self.player.load(stream_url, audio_url)
                await self.player.play()
                self._paused = False
                self.queue._current = item
                self.queue._items = []
                self.queue._cursor = 0
                break
            except RuntimeError as exc:
                if attempt == 1 or item.url not in self._stream_cache:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    await self._notify_admin(f"Fallo al reproducir {item.title}: {exc}")
                    return
                logger.info("Stream cacheado expirado, re-resolviendo: %s", item.url)
                self._stream_cache.pop(item.url, None)
                resolved = await self._stream_for(item.url)
                if not resolved:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    await self._notify_admin(f"Fallo al re-resolver {item.title}: {exc}")
                    return
                stream_url, audio_url = resolved
        self._schedule_prefetch(item)
        if update is not None:
            await self._show_card(update.effective_chat.id)
        await self._send_track_card(
            update, f"⏮️ Anterior: {item.title}", item
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Detener y rebobinar a 0:00 SIN borrar historial, cola, radio ni
        # navegacion: conserva todo para que ▶ reanude el video desde el inicio.
        if self.player.is_running:
            await self.player.rewind()
        self._queue_advance_needed = False
        self._paused = True
        self._persist_dirty()
        await self._render_card(self._track_status_text())
        await self._reply(update, "⏹️ Detenido. Usa ▶ para reanudar desde el inicio.")

    async def cmd_volume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await self._reply(update, "Uso: /volume <0-100>")
            return
        try:
            vol = int(context.args[0])
        except ValueError:
            await self._reply(update, "El volumen debe ser un numero entre 0 y 100.")
            return
        if not 0 <= vol <= 100:
            await self._reply(update, "El volumen debe estar entre 0 y 100.")
            return
        if not self.player.is_running:
            await self._reply(update, "No hay ninguna reproduccion activa: el reproductor no esta corriendo.")
            return
        await self.player.set_volume(vol)
        self._volume = vol
        self._persist_dirty()
        await self._reply(update, f"🔊 Volumen: {vol}")

    def _queue_list_text(self, page: int = 0) -> str | None:
        """Encabezado del mensaje de la lista (None si esta vacia).

        Muestra pagina actual y total: "Lista (pag 2/3):", o "Lista:" si hay
        una sola pagina. Es el UNICO texto del mensaje: el listado en si son
        los BOTONES paginados (decision 7b: la lista se muestra una sola vez,
        sin texto plano duplicado).
        """
        items = self.queue.all()
        cur = self.queue.current
        if not items:
            if cur is None:
                return None
            return "Lista:"
        pages = max(1, (len(items) + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
        if pages <= 1:
            return "Lista:"
        page = max(0, min(page, pages - 1))
        return f"Lista (pag {page + 1}/{pages}):"

    def _list_keyboard(self) -> InlineKeyboardMarkup:
        """Teclado del mensaje de la lista: pagina de 10 temas (solo si el
        mensaje lo abrió un admin/dj) + navegacion con el boton ❌ siempre
        presente.

        Cada boton es el tema con su posicion global y el titulo COMPLETO
        (sin truncar); el actual arranca con "▶️ ". En modo radio (sin
        playlist) no hay temas que listar: se muestra una fila con el tema
        SONANDO precedido de "▶️ " (informacion, no seleccion); tocarla
        cierra igual que el ❌. El flag fijo
        `_list_can_select` decide si hay botones de tema: al paginar no
        cambia aunque lo pages un user. La fila de navegacion mantiene 3
        slots para que el ❌ quede centrado; el slot vacio usa "·" (callback
        inerte `lst:noop`).
        """
        items = self.queue.all()
        current = self.queue.current
        pages = max(1, (len(items) + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
        page = max(0, min(self._list_page, pages - 1))
        start = page * _LIST_PAGE_SIZE

        rows: list[list[InlineKeyboardButton]] = []
        # Modo radio (sin playlist): no hay items que listar, pero se muestra
        # el tema que esta sonando con ▶️; tocarlo cierra igual que el ❌.
        if not self.queue.has_playlist and current is not None:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"▶️ {current.title.strip()}",
                        callback_data="lst:close",
                    )
                ]
            )
        elif self._list_can_select:
            segment = items[start : start + _LIST_PAGE_SIZE]
            for idx, it in enumerate(segment, start=start + 1):
                label = it.title.strip()
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"{'▶️ ' if it is current else ''}{idx}. {label}",
                            callback_data=f"lst:{idx}",
                        )
                    ]
                )

        prev = (
            InlineKeyboardButton("◀", callback_data="lst:prev")
            if pages > 1 and page > 0
            else InlineKeyboardButton("·", callback_data="lst:noop")
        )
        close = InlineKeyboardButton("❌", callback_data="lst:close")
        next_ = (
            InlineKeyboardButton("▶", callback_data="lst:next")
            if pages > 1 and page < pages - 1
            else InlineKeyboardButton("·", callback_data="lst:noop")
        )
        rows.append([prev, close, next_])
        return InlineKeyboardMarkup(rows)

    async def _close_list_message(self) -> None:
        """Borra el mensaje de la lista abierto (con su desvanecimiento) y
        limpia las referencias. Tolera que el mensaje ya no exista.

        La pagina del listado NO se resetea: queda persistida para que una
        reapertura (o un reinicio) continuen donde iba el usuario.
        """
        chat = self._list_chat_id
        msg = self._list_message_id
        self._list_chat_id = None
        self._list_message_id = None
        self._list_can_select = False
        if msg is not None and chat is not None:
            try:
                await self._app.bot.delete_message(chat, msg)
            except Exception as exc:  # noqa: BLE001 - no romper el flujo
                logger.warning("No se pudo borrar el mensaje de la lista: %s", exc)

    async def _open_queue_list(
        self, query, user_id: int | None, chat_id: int
    ) -> None:
        """Boton 📋 de la card: abre la lista como MENSAJE aparte.

        La card nunca se pisa: se envia un mensaje nuevo en el chat con la
        pagina ACTUAL (texto + botones paginados al unisono; el render clampa
        la pagina al rango valido). Si ya habia una lista abierta, se borra la
        anterior (con su desvanecimiento) y se manda una fresca (decision del
        usuario: nunca editar una lista existente). Cola vacia: solo responde
        el toast, no envia nada.
        """
        text = self._queue_list_text()
        if text is None:
            try:
                await query.answer("La lista esta vacia.")
            except Exception:  # noqa: BLE001
                pass
            return
        await self._close_list_message()
        self._list_can_select = user_id is not None and self.roles.has_role(user_id, "dj")
        try:
            msg = await self._app.bot.send_message(
                chat_id, text, reply_markup=self._list_keyboard()
            )
        except Exception as exc:  # noqa: BLE001 - no romper la card
            logger.warning("No se pudo enviar el mensaje de la lista: %s", exc)
            return
        self._list_chat_id = chat_id
        self._list_message_id = msg.message_id

    async def _edit_list_message(self) -> None:
        """Pagina el mensaje de la lista abierto (◀ ▶): edita TEXTO + botones
        de la pagina nueva (la misma pagina en ambos, se sincronizan). El
        mensaje no se recrea; el flag de seleccion no cambia con la pagina."""
        if self._list_chat_id is None or self._list_message_id is None:
            return
        text = self._queue_list_text(self._list_page)
        if text is None:
            return
        try:
            await self._app.bot.edit_message_text(
                text,
                chat_id=self._list_chat_id,
                message_id=self._list_message_id,
                reply_markup=self._list_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001 - no romper el flujo
            logger.warning("No se pudo paginar la lista: %s", exc)

    async def _list_answer(
        self, query, text: str | None = None, show_alert: bool = False
    ) -> None:
        """Responde un callback de la lista; el toast es prescindible, si
        falla la API se ignora."""
        try:
            await query.answer(text=text, show_alert=show_alert)
        except Exception:  # noqa: BLE001
            pass

    async def _on_list_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Botones del mensaje de la lista (callback `lst:*`).

        - `lst:close`: CUALQUIER usuario cierra: borra el mensaje de la lista
          (con su desvanecimiento).
        - `lst:prev`/`lst:next`: cualquiera pagina (se edita TEXTO + botones
          del mensaje abierto; ambos muestran la misma pagina).
        - `lst:N`: solo admin/dj (alerta si no): salta a la cancion N de la
          cola y la reproduce de inmediato; la lista se borra al salir.
        - `lst:noop`: placeholder inerte, se responde mudo.

        Cada callback se responde UNA sola vez (un answer por query).
        """
        query = update.callback_query
        data = query.data or ""
        action = data.split(":", 1)[1] if data.startswith("lst:") else ""
        user_id = query.from_user.id if query.from_user else None

        if action in ("", "noop"):
            await self._list_answer(query)
            return

        if action in ("prev", "next"):
            self._list_page += -1 if action == "prev" else 1
            await self._edit_list_message()
            await self._list_answer(query)
            return

        if action == "close":
            await self._list_answer(query)
            await self._close_list_message()
            return

        try:
            position = int(action)
        except ValueError:
            await self._list_answer(query)
            return

        if user_id is None or not self.roles.has_role(user_id, "dj"):
            await self._list_answer(
                query, "Solo el admin o un dj pueden elegir.", show_alert=True
            )
            return

        async with self._control_lock:
            self._from_card = True
            try:
                current = self.queue.current
                item = self.queue.jump_to(position)
                if item is None:
                    await self._list_answer(query, "Numero fuera de rango.")
                    return
                if item is current:
                    # Elegir el tema que YA suena: no salta, no cierra la lista.
                    # Se compara contra el actual ANTES del jump (jump_to ya
                    # movio el cursor y la posicion 1 es el item actual).
                    await self._list_answer(query, f"Ya esta sonando: {item.title}")
                    return
                await self._list_answer(query)
                await self._close_list_message()
                await self._play_item(update, item, preserve_current=True)
            finally:
                self._from_card = False

    async def cmd_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cur = self.queue.current
        if cur is None:
            await self._reply(update, "No hay ninguna cancion reproduciendose.")
        else:
            await self._send_track_card(update, f"▶️ Sonando: {cur.title}", cur)

    async def cmd_adduser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if len(args) != 2:
            await self._reply(update, "Uso: /adduser <telegram_user_id> <rol>")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await self._reply(update, "El ID debe ser un numero.")
            return
        role = args[1].lower()
        if role not in VALID_ROLES:
            await self._reply(update, f"Roles validos: {sorted(VALID_ROLES)}")
            return
        self.roles.set_role(user_id, role)
        await self._reply(update, f"Usuario {user_id} ahora es '{role}'.")

    async def cmd_removeuser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args:
            await self._reply(update, "Uso: /removeuser <telegram_user_id>")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await self._reply(update, "El ID debe ser un numero.")
            return
        if self.roles.remove_user(user_id):
            await self._reply(update, f"Usuario {user_id} removido.")
        else:
            await self._reply(update, f"El usuario {user_id} no estaba registrado.")

    async def cmd_solicitar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        if self.roles.get_role(user.id) != "user":
            await self._reply(update, "Ya tiene un rol asignado.")
            return
        admin_ids = self.roles.get_all_with_role("admin")
        for admin_id in admin_ids:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📩 Solicitud de acceso\n"
                    f"Usuario: {user.full_name} (ID: {user.id})\n"
                    f"Quiere ser DJ.\n\n"
                    f"Para darle acceso: /adduser {user.id} dj"
                ),
            )
        await self._reply(
            update,
            "Tu solicitud fue enviada a los admins. Te avisaran cuando te den acceso.",
        )