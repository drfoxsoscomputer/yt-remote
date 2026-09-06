"""Bot de Telegram de YT-Remote.

Controla un reproductor mpv local reproduciendo videos de YouTube.
Los comandos se enrutan por roles (admin > dj > user).
"""

import asyncio
import logging

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
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
        # Ancla de la radio: el ARTISTA fijado UNA sola vez, en la primera
        # reproduccion del usuario (al elegir la cancion del /play). /next y
        # el auto-advance lo reusan tal cual, sin re-derivar en cada salto.
        self._radio_artist: str = ""
        self._last_query: str = ""
        # cache: callback_data -> SearchResult para los botones de busqueda
        self._search_cache: dict[str, SearchResult] = {}
        # Prefetch (auto-continuacion): candidato a siguiente + stream resuelto
        self._prefetch_task: asyncio.Task | None = None
        self._prefetch_basis: str | None = None
        self._prefetch_candidate: tuple[QueueItem, bool] | None = None
        self._prefetch_resolved: tuple[tuple[QueueItem, bool], tuple[str, str | None]] | None = None
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
        self._register_owner()

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
            # Aviso de comandos perdidos: se lee el backlog ANTES de que el
            # polling lo consuma (post_init corre antes del start).
            await self._notify_pending_dropped(_app.bot)
            # Menú de comandos: al escribir "/" Telegram muestra esta lista.
            bot = _app.bot
            try:
                await bot.set_my_commands(
                    [
                        BotCommand("buscar", "Busca y reproduce un video o link de YouTube"),
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

        Fila 1: ⏮ anterior | ▶/⏸ alternar play-pause (dinamico) | ⏭ siguiente | ⏹ detener
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
        un solo mensaje. Si el tema no tiene miniatura, se crea como texto.
        """
        text = self._track_status_text()
        thumb = self._thumbnail_for(self.queue.current) if self.queue.current else ""
        try:
            if thumb:
                msg = await self._app.bot.send_photo(
                    chat_id,
                    photo=thumb,
                    caption=text,
                    reply_markup=self._control_keyboard(),
                )
                self._card_is_photo = True
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

    def _radio_seed(self, title: str) -> str:
        """A partir del titulo actual arma una semilla de busqueda limpia.

        Quita lo que sobra para buscar "una parecida": corchetes, parentesis,
        remixes, videos oficiales, lyric, etc. Ej:
        "Metallica - Nothing Else Matters (Official Video)" -> "Metallica - Nothing Else Matters"
        """
        import re as _re

        cleaned = _re.sub(r"[\(\[].*?[\)\]]", "", title)
        cleaned = _re.sub(
            r"\b(official video|official music video|official audio|lyric|lyrics|hq|hd|remix|mtv)\b",
            "",
            cleaned,
            flags=_re.IGNORECASE,
        )
        return " ".join(cleaned.split()).strip()

    @staticmethod
    def _artist_from_title(title: str, channel: str = "") -> str:
        """Extrae el artista (cantante/grupo/banda) del titulo de una cancion.

        Patrones soportados:
        1. Normal "ARTISTA - Cancion": "KENT LEROY - Quiero Cantar del Amor" -> "KENT LEROY".
        2. Invertido (estilo cristiano) "Cancion - Artista - Banda/Minist":
           "IMPACTANTE - Mafe Restrepo - GP BAND - Generacion Pentecostal".
           Con 3+ segmentos el artista es el segundo segmento.
        3. Desempate por canal: si el canal del video (quien lo subio) coincide
           con un segmento del titulo, ese segmento ES el grupo real (el canal
           suele ser la iglesia/banda del tema, p.ej. "Generacion Pentecostal").

        Prioridad: (3) desempate por canal > (1) formato normal > (2) invertido.
        Devuelve "" si no se parece a ningun patron.
        """
        import re as _re

        cleaned = _re.sub(r"[\(\[].*?[\)\]]", "", title or "")
        parts = [
            p.strip().strip(" -–—")
            for p in _re.split(r"[–—-]", cleaned)
            if p.strip().strip(" -–—")
        ]
        if not parts:
            return ""

        if channel:
            norm_channel = YTRemoteBot._normalizar(channel)
            if norm_channel:
                for part in parts:
                    if YTRemoteBot._normalizar(part) == norm_channel:
                        return part

        if len(parts) == 2:
            return parts[0]

        if len(parts) >= 3:
            return parts[1]

        return ""

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
        """Semilla de busqueda por el ARTISTA real (NUNCA el canal/uploader).

        El artista se fija UNA sola vez: en la primera reproduccion del
        usuario (cuando elige la cancion, ver on_callback). Aca NO se
        re-deriva en cada /next:

        1. Ancla de sesion (_radio_artist): el artista elegido en /play.
        2. Artista propagado por la radio (candidate.artist): respaldo si la
           sesion arranco sin ancla explicita (ej. playlist/link directo).
        3. Si no hay ninguno: artista parseado del titulo ("KENT LEROY - ...").
        4. Ultimo recurso: titulo limpio (_radio_seed).
        """
        if self._radio_artist:
            return self._radio_artist
        if item.artist:
            return item.artist
        artist = self._artist_from_title(item.title, item.channel)
        if artist:
            return artist
        return self._radio_seed(item.title)

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

        # Radio por semilla: buscar por el ARTISTA real (cantante/grupo/banda),
        # NUNCA por el canal que subio el video (suele ser un re-uploader o
        # un canal de charlas/sermones, no el musico).
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
            # El candidato de radio guarda el ARTISTA que lo genero (no su
            # canal): es el ANCLA que mantiene la cadena aunque este video
            # sea un re-upload en otro canal o su titulo venga al reves.
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

        import re as _re

        # Pasada 1: preferir resultados cuyo TITULO mencione al artista
        # (normalizado). Es lo que garantiza "otra cancion de ESE artista"
        # aunque la busqueda traiga mezclado algo de otro genero.
        anchor = self._normalizar(seed)
        if anchor:
            for r in results:
                if (
                    r.url != current.url
                    and not self.queue.is_recent(r.url)
                    and anchor in self._normalizar(r.title)
                ):
                    return _candidate(r)
        # Pasada 2: lo que parece musica (titulo con 'Artista - Cancion').
        for r in results:
            if (
                r.url != current.url
                and not self.queue.is_recent(r.url)
                and bool(_re.search(r"[–\-—]", r.title))
            ):
                return _candidate(r)
        # Pasada 3: cualquier no-reciente (para no apagar la radio).
        for r in results:
            if r.url != current.url and not self.queue.is_recent(r.url):
                return _candidate(r)
        return None, False

    def _schedule_prefetch(self, current: QueueItem) -> None:
        """Programa el prefetch del siguiente track en 2 fases.

        Fase A (inmediata): decide el candidato (playlist o radio por semilla).
        Fase B (programada): ~45s antes de que termine el actual, resuelve el
        stream del candidato y lo cachea para que el salto sea instantaneo.
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

            # Fase B: resolver el stream cerca del final, no al inicio
            # (las URLs de googlevideo expiran con el tiempo).
            wait = max(0, current.duration_seconds - 45)
            try:
                if wait:
                    await asyncio.sleep(wait)
            except asyncio.CancelledError:
                return
            if (
                self._prefetch_basis != current.url
                or self.queue.current is not current
            ):
                return
            try:
                resolved = await asyncio.to_thread(resolve_stream_url, candidate.url)
            except Exception:
                resolved = None
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

        # Sin prefetch listo: resolver el siguiente ahora mismo.
        current_item = self.queue.current
        if current_item is None:
            return
        candidate, from_queue = await self._pick_next_candidate(current_item)
        if candidate is None:
            await self._reply(None, "La cola quedo vacia.")
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
        lines.append("• /buscar <busqueda o link> — busca y reproduce (escribe PRIMERO el artista o grupo)")
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
        lines.append("Consejo: escribe PRIMERO el artista y DESPUES la cancion,")
        lines.append("ej: /buscar Mafe Restrepo Impactante. Si lo inviertes,")
        lines.append("la radio del siguiente tema puede saltarse a otra cancion.")

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
        text = (context.args or [])
        query = " ".join(text).strip()
        if not query:
            await self._reply(update, "Uso: /buscar <busqueda o link de YouTube>")
            return
        # Nuevo /play = nueva intencion: se descarta el ancla anterior de la
        # radio. Se redefine cuando el usuario elige la cancion (on_callback)
        # o en la playlist (primer track).
        self._radio_artist = ""
        self._last_query = query

        if is_youtube_link(query):
            # Playlist o mix: expandir y encolar todos sus tracks. El primero
            # suena YA y el resto queda en queue_manager para el prefetch.
            if is_playlist_url(query):
                await context.bot.send_message(
                    update.effective_chat.id,
                    "Expandiendo playlist/mix, un momento...",
                )
                tracks = await asyncio.to_thread(
                    expand_playlist, query, 50
                )
                if not tracks:
                    await self._reply(update, "No se pudo expandir esa lista.")
                    return
                # Playlist fija: se encola completa y el primero se reproduce
                # ya. El cursor avanza en orden y da la vuelta (bucle).
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
                # Ancla de la radio: se fija con el primer track de la
                # playlist (su artista), para cuando se agote el bucle.
                artist = self._artist_from_title(first.title, first.channel)
                if artist:
                    self._radio_artist = artist
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
            item = QueueItem(
                url=query, title=query, thumbnail=thumbnail_from_url(query)
            )
            started = await self._play_item(update, item)
            if not started:
                return

        # Busqueda por nombre -> mostrar botones con thumbnail
        await context.bot.send_message(
            update.effective_chat.id, "Buscando...", parse_mode=ParseMode.HTML
        )
        # yt-dlp es lento y bloqueante: ejecutarlo en un thread para no
        # congelar el bot mientras busca.
        results = await asyncio.to_thread(
            search, query, self.config.max_results
        )
        if not results:
            await self._reply(update, "No encontre resultados.")
            return

        self._search_cache.clear()
        keyboard = []
        for i, r in enumerate(results):
            cb = f"pick:{i}"
            self._search_cache[cb] = r
            label = f"{i+1}. {r.title} ({r.duration})"
            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        # Listado de resultados sin miniatura: la miniatura se muestra al
        # elegir un video (es la tarjeta persistente del mini reproductor).
        await context.bot.send_message(
            update.effective_chat.id,
            "Elegi un video:\n\n"
            "Consejo: escribe PRIMERO el artista y DESPUES la cancion, "
            "ej: /buscar Mafe Restrepo Impactante.",
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
            await query.edit_message_text("▶️ Listo, reproduciendo...")

        # ANCLA DE LA RADIO: la primera reproduccion del usuario es la que
        # define el artista. Se captura aca, UNA sola vez, de la cancion
        # elegida ("KENT LEROY - Quiero Cantar del Amor" -> "KENT LEROY";
        # "IMPACTANTE - Mafe Restrepo - GP BAND - ..." -> "Mafe Restrepo").
        # Si esa cancion no trae artista en el titulo, se usa el texto del
        # /play (el usuario lo escribio buscando a alguien).
        artist = self._artist_from_title(result.title, result.channel)
        if not artist and self._last_query:
            artist = self._last_query
        self._radio_artist = artist

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

        # Resolver el link de YouTube a URLs de stream directo que mpv
        # pueda reproducir sin necesitar yt-dlp propio.
        resolved = await asyncio.to_thread(resolve_stream_url, item.url)
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
        try:
            await self.player.load(stream_url, audio_url)
            await self.player.play()
            self._paused = False
            if not preserve_current:
                self.queue.set_current(item)
        except RuntimeError as exc:
            await self._reply(update, f"No se pudo reproducir: {exc}")
            return False
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
        try:
            # Quita el spinner del boton. Si un _reply interno ya respondio
            # con un toast, esto falla callado y conserva ese toast.
            await query.answer()
        except Exception:
            pass

    async def cmd_next(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Salta YA al siguiente tema: corta el actual y reproduce el siguiente.

        Igual que en el auto-advance: si el prefetch ya resolvio el stream,
        salta con cero silencio; si no, toma el proximo de la cola o hace
        radio por semilla del track actual.
        """

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
            if from_queue:
                self.queue.next()  # avanza el cursor de la playlist (bucle)
            else:
                self.queue.set_current(candidate)
            self._schedule_prefetch(candidate)
            await self._send_track_card(
                update, f"⏭️ Siguiente: {candidate.title}", candidate
            )
            return

        # Sin prefetch: decidir el siguiente ahora mismo (cola o radio).
        current_item = self.queue.current
        if current_item is None:
            await self._reply(update, "No hay ninguna cancion reproduciendose.")
            return
        candidate, from_queue = await self._pick_next_candidate(current_item)
        if candidate is None:
            await self._reply(update, "No tengo nada siguiente para reproducir.")
            return
        if from_queue:
            self.queue.next()  # avanza el cursor de la playlist (bucle)
        started = await self._play_item(update, candidate, preserve_current=from_queue)
        if not started:
            await self._reply(update, "No se pudo reproducir el siguiente tema.")
            return
        await self._send_track_card(
            update, f"⏭️ Siguiente: {candidate.title}", candidate
        )

    async def cmd_prev(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Retrocede al tema anterior.

        Con playlist fija: mueve el cursor una posicion atras (con wrap,
        simetrico a /next). En modo radio: vuelve a la cancion reproducida
        antes de la actual (historico de items). Si no hay anterior, avisa.
        """
        if self.queue.current is None:
            await self._reply(update, "No hay ninguna cancion reproduciendose.")
            return
        item = self.queue.previous()
        if item is None:
            await self._reply(update, "No hay cancion anterior en la cola.")
            return
        from_playlist = self.queue.has_playlist
        started = await self._play_item(update, item, preserve_current=from_playlist)
        if started:
            await self._send_track_card(
                update, f"⏮️ Anterior: {item.title}", item
            )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.player.stop()
        self.queue.clear()
        self._cancel_prefetch()
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