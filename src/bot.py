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
)

from config import Config
from player import Player
from queue_manager import QueueItem, QueueManager
from roles import VALID_ROLES, RoleManager
from search import SearchResult, is_playlist_url, expand_playlist, is_youtube_link, search, resolve_stream_url, thumbnail_from_url
import setup_cli as setup

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


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
        self._register_owner()

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

        # /buscar es el nombre principal; /play queda como alias.
        app.add_handler(CommandHandler("buscar", self._require_chat(self.cmd_play)))
        app.add_handler(CommandHandler("play", self._require_chat(self.cmd_play)))
        app.add_handler(CommandHandler("start", self._require_chat(self.cmd_start)))
        app.add_handler(
            CommandHandler(
                "pause", self._require_chat(self._require("dj", self.cmd_pause))
            )
        )
        app.add_handler(
            CommandHandler(
                "resume", self._require_chat(self._require("dj", self.cmd_resume))
            )
        )
        app.add_handler(
            CommandHandler(
                "next", self._require_chat(self._require("dj", self.cmd_next))
            )
        )
        app.add_handler(
            CommandHandler(
                "prev", self._require_chat(self._require("dj", self.cmd_prev))
            )
        )
        app.add_handler(
            CommandHandler(
                "stop", self._require_chat(self._require("dj", self.cmd_stop))
            )
        )
        app.add_handler(
            CommandHandler(
                "volume", self._require_chat(self._require("dj", self.cmd_volume))
            )
        )
        # /lista es el nombre principal; /queue queda como alias.
        app.add_handler(CommandHandler("lista", self._require_chat(self.cmd_queue)))
        app.add_handler(CommandHandler("queue", self._require_chat(self.cmd_queue)))
        app.add_handler(CommandHandler("now", self._require_chat(self.cmd_now)))
        app.add_handler(
            CommandHandler(
                "adduser", self._require_chat(self._require("admin", self.cmd_adduser))
            )
        )
        app.add_handler(
            CommandHandler(
                "removeuser",
                self._require_chat(self._require("admin", self.cmd_removeuser)),
            )
        )
        app.add_handler(CallbackQueryHandler(self._require_chat(self.on_callback)))

        self._app = app

        async def post_init(_app: Application) -> None:
            # Marca de arranque: si ves DOS de estas sin haber reiniciado, hay
            # mas de una instancia del bot con el mismo token (una segunda
            # instancia pierde mensajes en silencio).
            logger.info(
                "BOT ARRANCADO (pid=%s)",
                os.getpid(),
            )
            # Aviso de comandos perdidos: se lee el backlog ANTES de que el
            # polling lo consuma (post_init corre antes del start).
            await self._notify_pending_dropped(_app.bot)
            # Menú de comandos: al escribir "/" Telegram muestra esta lista.
            bot = _app.bot
            try:
                await bot.set_my_commands(
                    [
                        BotCommand("buscar", "Buscar <artista> - <cancion> o link"),
                        BotCommand("lista", "Ver la lista; /lista N reproduce el tema N"),
                        BotCommand("now", "Que esta sonando"),
                        BotCommand("pause", "Pausar la reproduccion"),
                        BotCommand("resume", "Reanudar la reproduccion"),
                        BotCommand("next", "Saltar al siguiente tema"),
                        BotCommand("stop", "Detener y limpiar la cola"),
                        BotCommand("volume", "Ajustar el volumen (0-100)"),
                        BotCommand("adduser", "Dar acceso con un rol (admin)"),
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
                logger.info("Rechazado mensaje de chat no permitido.")
                return
            await handler(update, context)

        wrapper.__name__ = getattr(handler, "__name__", "wrapper")
        return wrapper

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
                # no edita el mensaje.
                try:
                    await query.answer(
                        text, show_alert=False
                    )  # tooltip de respuesta
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
        return f"{state}\n🎵 {self._truncate(cur.title)}"

    def _control_keyboard(self) -> InlineKeyboardMarkup:
        """Teclado del mini reproductor persistente.

        Fila 1: ⏮ anterior | ▶/⏸ alternar play-pause | ⏭ siguiente | ⏹ detener
        Fila 2: 🔊−10 | <volumen actual> | 🔊+10 | 📋 lista
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
        ]
        return InlineKeyboardMarkup(keyboard)

    async def _render_card(
        self, text: str, chat_id: int | None = None
    ) -> None:
        """Edita el mensaje persistente del mini reproductor (si existe).

        Si la tarjeta es una FOTO, edita miniatura + texto + teclado juntos
        (edit_message_media), asi al pasar de cancion cambia TODO el mensaje
        y no queda la miniatura de la cancion anterior clavada. Si la tarjeta
        es un mensaje de texto (sin miniatura disponible), edita solo el texto.

        No envia una tarjeta nueva: para crear la primera usar _show_card.
        """
        if self._card_chat_id is None or self._card_message_id is None:
            return
        chat_id = chat_id or self._card_chat_id
        bot = self._app.bot
        thumb = self._thumbnail_for(self.queue.current) if self.queue.current else ""
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
            resolved = await asyncio.to_thread(resolve_stream_url, url)
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

    async def _pick_next_candidate(self, current: QueueItem) -> tuple[QueueItem | None, bool]:
        """Fase A: decide cual es el siguiente a reproducir.

        Prioridad:
        1. Si la cola (playlist del usuario) tiene items, ese es el siguiente.
        2. Si no, radio por semilla: busca "una parecida" al track actual.

        Devuelve (candidato, vino_de_la_cola).
        """
        peeked = self.queue.peek(0)
        if peeked is not None:
            return peeked, True

        # Radio por semilla: buscar por el ARTISTA que el usuario escribió en la
        # lupa (el ancla de sesion) o, si arranco de un link/playlist, por el
        # canal del video. Sobre el titulo del candidato NO se parsea nada:
        # es un filtro estricto, no una derivacion de artista.
        seed = self._artist_seed(current)
        if not seed:
            return None, False
        # Pedir mas resultados: mas catalogo del artista para poder avanzar
        # sin repetir (la radio busca 50 y elige entre los no-recientes).
        try:
            results = await asyncio.to_thread(search, seed, 50)
        except Exception:
            return None, False

        def _candidate(r) -> tuple[QueueItem, bool]:
            # El candidato de radio guarda el ancla que lo genero (no su
            # canal): es lo que mantiene la cadena de la radio.
            return (
                QueueItem(
                    url=r.url,
                    title=r.title,
                    duration_seconds=r.duration_seconds,
                    thumbnail=r.thumbnail,
                    channel=r.channel,
                    artist=seed,
                ),
                False,
            )

        # Unica pasada, estricta: el candidato tiene que MENCIONAR al artista
        # en su titulo (normalizado) o ser del MISMO canal. Todo lo demas
        # queda fuera, aunque suene parecido (nada de covers importados de
        # otros artistas: si el usuario escribió "GP Band", nunca entra un
        # "Denicher Pol - Inexplicable" por suerte).
        anchor = self._normalizar(seed)
        if not anchor:
            return None, False
        for r in results:
            if r.url == current.url or self.queue.is_recent(r.url):
                continue
            if anchor in self._normalizar(r.title) or anchor in self._normalizar(r.channel):
                return _candidate(r)
        return None, False

    def _radio_over_message(self) -> str:
        """Aviso cuando la radio estricta no encuentra mas temas del artista."""
        ancla = self._radio_artist
        if ancla:
            return f"Se acabó la radio de {ancla}: no hay mas canciones de este artista en YouTube."
        return "No encontre otra cancion para la radio."

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
                candidate, from_queue = await self._pick_next_candidate(current)
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
        candidate, from_queue = None, False
        if (
            self._prefetch_candidate is not None
            and self._prefetch_basis == current_item.url
        ):
            candidate, from_queue = self._prefetch_candidate
        else:
            candidate, from_queue = await self._pick_next_candidate(current_item)
        if candidate is None:
            await self._reply(None, self._radio_over_message())
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

        def add(required: str, line: str) -> None:
            lines.append(line) if rank.get(required, 0) <= level else None

        lines: list[str] = []

        # user
        lines.append("• /buscar <artista> - <cancion> — busca y reproduce en 1 paso (lo que va antes del guion es el artista)")
        lines.append("• /lista — ver la lista; /lista N reproduce el tema N")
        lines.append("• /now — que esta sonando")

        if level >= 1:  # dj
            lines.append("• /pause /resume — pausar y reanudar")
            lines.append("• /next — saltar al siguiente tema")
            lines.append("• /stop — detener y limpiar cola")
            lines.append("• /volume <0-100> — ajustar el volumen")

        if level >= 2:  # admin
            lines.append("• /adduser <id> <rol> — dar acceso con un rol")
            lines.append("• /removeuser <id> — quitar acceso")

        lines.append("")
        lines.append("¿Cómo buscar? Escribe el ARTISTA tal cual: la radio del")
        lines.append("siguiente tema saldrá exactamente de lo que escribas.")
        lines.append("Ej: /buscar GP Band - Inexplicable")

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
                await self._reply(update, "Configurado: este chat quedo habilitado para el bot.\n\n" + "Comandos disponibles para tu rol (admin):\n" + self.help_for_role("admin"))
                return

        if not self._chat_allowed(update):
            await self._reply(update, "Este bot no esta habilitado en este chat.")
            return

        await self._reply(update, "YT-Remote activo.\n\n" + f"Comandos disponibles para tu rol ({user_role}):\n" + self.help_for_role(user_role))

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
            await context.bot.send_message(
                update.effective_chat.id,
                "Expandiendo playlist/mix, un momento...",
            )
            tracks = await asyncio.to_thread(expand_playlist, query, 50)
            if not tracks:
                await self._reply(update, "No se pudo expandir esa lista.")
                return
            items = [
                QueueItem(
                    url=t.url,
                    title=t.title,
                    duration_seconds=t.duration_seconds,
                    thumbnail=t.thumbnail,
                    channel=t.channel,
                )
                for t in tracks
            ]
            self.queue.set_playlist(items)
            first = items[0]
            self._radio_artist = first.channel or ""
            started = await self._play_item(update, first, preserve_current=True)
            if started:
                await self._reply(
                    update,
                    f"▶️ Playlist ({len(tracks)}): {first.title}\n"
                    f"Bucle activo: suena en orden y se repite; /lista para verla.",
                )
            return

        await context.bot.send_message(
            update.effective_chat.id, "Reproduciendo link directo..."
        )
        item = QueueItem(url=query, title=query, thumbnail=thumbnail_from_url(query))
        started = await self._play_item(update, item)
        if not started:
            return

    async def _run_search(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
    ) -> None:
        """Busca en YouTube y muestra el listado de resultados con botones."""
        await context.bot.send_message(
            update.effective_chat.id, "Buscando...", parse_mode=ParseMode.HTML
        )
        # yt-dlp es lento y bloqueante: ejecutarlo en un thread para no
        # congelar el bot mientras busca.
        results = await asyncio.to_thread(search, query, self.config.max_results)
        if not results:
            await self._reply(update, "No encontre resultados.")
            return

        # Nueva busqueda reemplaza el cache de streams: queda solo con las
        # URLs de los resultados que van a anticiparse abajo.
        self._clear_stream_cache()
        self._search_cache.clear()
        keyboard = []
        for i, r in enumerate(results):
            cb = f"pick:{i}"
            self._search_cache[cb] = r
            label = f"{i+1}. {r.title} ({r.duration})"
            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

        # El mesonero: se anticipan los streams de los resultados en la cola
        # de resolucion serial, para que elegir uno suene casi al instante.
        self._anticipate_urls([r.url for r in results])

        reply_markup = InlineKeyboardMarkup(keyboard)
        # Listado de resultados sin miniatura: la miniatura se muestra al
        # elegir un video (es la tarjeta persistente del mini reproductor).
        await context.bot.send_message(
            update.effective_chat.id,
            "Elige un video:",
            reply_markup=reply_markup,
        )

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query.data and query.data.startswith("ctl:"):
            # Un boton de la tarjeta persistente del mini reproductor.
            await self._on_control(update, context, query.data.split(":", 1)[1])
            return
        await query.answer()
        if not query.data or not query.data.startswith("pick:"):
            return
        result = self._search_cache.get(query.data)
        if result is None:
            await query.edit_message_text("Esa busqueda ya expiro, busca de nuevo.")
            return

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

        # Forzar creación de tarjeta fresca: limpiar estado previo por si quedó
        # de una sesión anterior y coincidía con el chat actual.
        self._card_chat_id = None
        self._card_message_id = None
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
            await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
            return False
        stream_url, audio_url = resolved

        # empezar reproduccion desde cero
        try:
            await self.player.start()
        except RuntimeError as exc:
            await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
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
                break
            except RuntimeError as exc:
                if attempt == 1 or item.url not in self._stream_cache:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
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
        await self._reply(update, "⏸️ Pausado")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.queue.current is None or not self.player.is_running:
            await self._reply(update, "No hay ninguna reproduccion activa.")
            return
        await self.player.play()
        self._paused = False
        await self._reply(update, "▶️ Reanudando")

    async def _toggle_play_pause(self) -> None:
        """Alterna reproducir/pausar (boton ▶/⏸ de la tarjeta)."""
        if self.queue.current is None or not self.player.is_running:
            return
        if self._paused:
            await self.player.play()
            self._paused = False
        else:
            await self.player.pause()
            self._paused = True

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
        self._card_chat_id = update.effective_chat.id
        self._card_message_id = query.message.message_id
        chat_id = self._card_chat_id

        self._from_card = True
        reflect_status = True
        # Quita el spinner YA: las acciones re-renderizan la tarjeta o mandan
        # toast despues; el boton no debe quedar clavado esperando al render.
        # (vol-info re-responde despues con su toast, y eso gana sobre este.)
        try:
            await query.answer()
        except Exception:
            pass
        try:
            if action == "pp":
                await self._toggle_play_pause()
            elif action == "prev":
                await self.cmd_prev(update, context)
            elif action == "next":
                await self.cmd_next(update, context)
            elif action == "stop":
                await self.cmd_stop(update, context)
                self._paused = False
            elif action in ("vol-10", "vol+10"):
                delta = -10 if action == "vol-10" else 10
                self._volume = max(0, min(100, self._volume + delta))
                if self.player.is_running:
                    await self.player.set_volume(self._volume)
            elif action == "vol-info":
                # Boton de estado: el volumen ya se lee en su etiqueta.
                await query.answer(f"Volumen: {self._volume}%")
                reflect_status = False
            elif action == "lista":
                # El boton 📋 muestra la lista encima de la tarjeta: se
                # edita el mismo mensaje, ahora con el listado (los botones
                # de control siguen pegados abajo).
                text = self._queue_list_text() or "La lista esta vacia."
                await self._render_card(text, chat_id)
                reflect_status = False  # no pisa la lista recien mostrada
            else:
                return
        finally:
            self._from_card = False

        # Estado final: el texto de la tarjeta refleja el cambio de la
        # accion (nuevo tema, pausa, volumen, detenido, etc.).
        if reflect_status:
            await self._render_card(self._track_status_text(), chat_id)

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
            candidate, from_queue = None, False
            if (
                self._prefetch_candidate is not None
                and self._prefetch_basis == current_item.url
            ):
                candidate, from_queue = self._prefetch_candidate
            else:
                candidate, from_queue = await self._pick_next_candidate(current_item)
            if candidate is None:
                await self._reply(update, self._radio_over_message())
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
            resolved = await self._stream_for(item.url)
            if not resolved:
                await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
                return
            stream_url, audio_url = resolved
            try:
                await self.player.start()
            except RuntimeError as exc:
                await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
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
                        return
                    logger.info("Stream cacheado expirado, re-resolviendo: %s", item.url)
                    self._stream_cache.pop(item.url, None)
                    resolved = await self._stream_for(item.url)
                    if not resolved:
                        await self._reply(update, f"No se pudo reproducir: {exc}")
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
                await self.player.load(stream_url, audio_url)
                await self.player.play()
                self._paused = False
            except RuntimeError as exc:
                await self._reply(update, f"No se pudo saltar: {exc}")
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
        candidate, from_queue = None, False
        if (
            self._prefetch_candidate is not None
            and self._prefetch_basis == current_item.url
        ):
            candidate, from_queue = self._prefetch_candidate
        else:
            candidate, from_queue = await self._pick_next_candidate(current_item)
        if candidate is None:
            await self._reply(update, self._radio_over_message())
            return
        # Navegacion pura: seteamos _current directo para evitar el doble push
        # al stack de navegacion (ya se guardo el item en _push_to_nav_back).
        self.queue._current = candidate
        self.queue._items = []
        self.queue._cursor = 0
        resolved = await self._stream_for(candidate.url)
        if not resolved:
            await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
            return
        stream_url, audio_url = resolved
        try:
            await self.player.start()
        except RuntimeError as exc:
            await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
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
                    return
                logger.info("Stream cacheado expirado, re-resolviendo: %s", candidate.url)
                self._stream_cache.pop(candidate.url, None)
                resolved = await self._stream_for(candidate.url)
                if not resolved:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    return
                stream_url, audio_url = resolved
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
            resolved = await self._stream_for(item.url)
            if not resolved:
                await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
                return
            stream_url, audio_url = resolved
            try:
                await self.player.start()
            except RuntimeError as exc:
                await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
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
                        return
                    logger.info("Stream cacheado expirado, re-resolviendo: %s", item.url)
                    self._stream_cache.pop(item.url, None)
                    resolved = await self._stream_for(item.url)
                    if not resolved:
                        await self._reply(update, f"No se pudo reproducir: {exc}")
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
        resolved = await self._stream_for(item.url)
        if not resolved:
            await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
            return
        stream_url, audio_url = resolved
        try:
            await self.player.start()
        except RuntimeError as exc:
            await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
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
                    return
                logger.info("Stream cacheado expirado, re-resolviendo: %s", item.url)
                self._stream_cache.pop(item.url, None)
                resolved = await self._stream_for(item.url)
                if not resolved:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    return
                stream_url, audio_url = resolved
        self._schedule_prefetch(item)
        if update is not None:
            await self._show_card(update.effective_chat.id)
        await self._send_track_card(
            update, f"⏮️ Anterior: {item.title}", item
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.player.stop()
        self.queue.clear()
        self._cancel_prefetch()
        self._clear_stream_cache()
        self._clear_nav_stacks()
        self._queue_advance_needed = False
        self._radio_artist = ""
        self._paused = False
        await self._render_card(self._track_status_text())
        await self._reply(update, "⏹️ Detenido y cola limpia")

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
        await self._reply(update, f"🔊 Volumen: {vol}")

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        n = None
        if context.args:
            try:
                n = int(context.args[0])
            except ValueError:
                n = None

        # Reproducir el tema de la posición elegida (/lista N). La lista es
        # fija y queda intacta: jump_to solo mueve el cursor (bucle).
        if n is not None:
            cur = self.queue.current
            item = self.queue.jump_to(n)
            if item is None:
                await self._reply(update, "Numero fuera de rango. /lista para ver la lista.")
                return
            if item is cur:
                await self._reply(update, f"Ya esta sonando: {item.title}")
                return
            started = await self._play_item(update, item, preserve_current=True)
            if started:
                await self._reply(update, f"▶️ Reproduciendo: {item.title}")
            return

        # Lista visible: la playlist completa SIEMPRE con el actual marcado
        # [▶️] en su posición (los 25 temas se ven siempre, no se consumen).
        # La lista la renderiza un helper reutilizado por el boton 📋 de la
        # tarjeta persistente, para que ambos muestren el mismo formato.
        text = self._queue_list_text()
        if text is None:
            await self._reply(update, "La lista esta vacia.")
            return
        await self._reply(update, text)

    def _queue_list_text(self) -> str | None:
        """Texto de la lista actual (None si esta vacia).

        Compartido entre /lista y el boton 📋 de la tarjeta persistente.
        """
        cur = self.queue.current
        items = self.queue.all()
        if cur is None and not items:
            return None
        lines = []
        if items:
            for i, it in enumerate(items, start=1):
                mark = " [▶️]" if it is cur else ""
                lines.append(f"{i}.{mark} {it.title}")
        else:
            if cur is not None:
                lines.append(f"1. [▶️] {cur.title}")
        return "Lista:\n" + "\n".join(lines)

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