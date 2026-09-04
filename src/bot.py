"""Bot de Telegram de YT-Remote.

Controla un reproductor mpv local reproduciendo videos de YouTube.
Los comandos se enrutan por roles (admin > dj > user).
"""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from search import SearchResult, is_youtube_link, search
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
        self.queue = QueueManager()
        # cache: callback_data -> SearchResult para los botones de busqueda
        self._search_cache: dict[str, SearchResult] = {}
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
        app.run_polling  # noqa: B018  (validar metodo disponible)
        return app

    def _require(self, role: str, handler):
        """Envuelve un handler exigiendo un rol minimo."""

        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            if user is None:
                return
            if not self.roles.has_role(user.id, role):
                await update.message.reply_text(
                    f"Acceso denegado: necesitas rol '{role}' para este comando."
                )
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

    def help_for_role(self, role: str) -> str:
        """Devuelve el listado de comandos permitidos para un rol."""
        rank = {"user": 0, "dj": 1, "admin": 2}
        level = rank.get(role, 0)

        def add(required: str, line: str) -> None:
            lines.append(line) if rank.get(required, 0) <= level else None

        lines: list[str] = []

        # user
        lines.append("• /play <busqueda o link> — busca y reproduce")
        lines.append("• /queue — ver la cola de temas")
        lines.append("• /now — que esta sonando")

        if level >= 1:  # dj
            lines.append("• /pause /resume — pausar y reanudar")
            lines.append("• /next /prev — cambiar de tema")
            lines.append("• /stop — detener y limpiar cola")
            lines.append("• /volume <0-100> — ajustar el volumen")

        if level >= 2:  # admin
            lines.append("• /adduser <id> <rol> — dar acceso con un rol")
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
                await update.message.reply_text(
                    "Configurado: este chat quedo habilitado para el bot.\n\n"
                    "Comandos disponibles para tu rol (admin):\n"
                    + self.help_for_role("admin")
                )
                return

        if not self._chat_allowed(update):
            await update.message.reply_text("Este bot no esta habilitado en este chat.")
            return

        await update.message.reply_text(
            "YT-Remote activo.\n\n"
            f"Comandos disponibles para tu rol ({user_role}):\n"
            + self.help_for_role(user_role)
        )

    async def cmd_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (context.args or [])
        query = " ".join(text).strip()
        if not query:
            await update.message.reply_text("Uso: /play <busqueda o link de YouTube>")
            return

        if is_youtube_link(query):
            await context.bot.send_message(
                update.effective_chat.id, "Reproduciendo link directo..."
            )
            await self._load_url(update, query)
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
            await update.message.reply_text("No encontre resultados.")
            return

        self._search_cache.clear()
        keyboard = []
        for i, r in enumerate(results):
            cb = f"pick:{i}"
            self._search_cache[cb] = r
            label = f"{i+1}. {r.title} ({r.duration})"
            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        thumb = results[0].thumbnail
        sent_photo = False
        if thumb:
            try:
                await context.bot.send_photo(
                    update.effective_chat.id,
                    photo=thumb,
                    caption="Elegi un video:",
                    reply_markup=reply_markup,
                )
                sent_photo = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo enviar foto con thumbnail: %s", exc)
        if not sent_photo:
            # Sin thumbnail (o fallo): mostrar resultados como texto.
            await context.bot.send_message(
                update.effective_chat.id,
                "Elegi un video:",
                reply_markup=reply_markup,
            )

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if not query.data or not query.data.startswith("pick:"):
            return
        result = self._search_cache.get(query.data)
        if result is None:
            await query.edit_message_text("Esa busqueda ya expiro, busca de nuevo.")
            return
        await query.edit_message_caption(caption=f"Reproduciendo: {result.title}")
        await self._load_url(update, result.url, caption=result.title)

    async def _load_url(
        self, update: Update, url: str, caption: str | None = None
    ) -> None:
        item = QueueItem(url=url, title=caption or url)

        # ya hay algo reproduciendo -> poner en cola
        if self.player.is_playing:
            self.queue.add(item)
            await update.message.reply_text(f"Agregado a la cola: {item.title}")
            return

        # empezar reproduccion desde cero
        try:
            await self.player.start()
        except RuntimeError as exc:
            await update.message.reply_text(
                f"Problema al iniciar el reproductor: {exc}"
            )
            return
        try:
            self.queue.add(item)
            self.queue.next()  # marca el actual
            await self.player.load(url)
            await self.player.play()
        except RuntimeError as exc:
            await update.message.reply_text(
                f"No se pudo reproducir: {exc}"
            )
            return
        await update.message.reply_text(f"▶️ Reproduciendo: {item.title}")

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.player.pause()
        await update.message.reply_text("⏸️ Pausado")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.player.play()
        await update.message.reply_text("▶️ Reanudando")

    async def cmd_next(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        item = self.queue.next()
        if item is None:
            await update.message.reply_text("No hay mas canciones en la cola.")
            return
        await self.player.load(item.url)
        await update.message.reply_text(f"⏭️ Siguiente: {item.title}")

    async def cmd_prev(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.player.command("cycle", "ab-loop")  # placeholder; sin historial no aplica
        await update.message.reply_text("No hay cancion anterior en la cola.")

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.player.stop()
        self.queue.clear()
        await update.message.reply_text("⏹️ Detenido y cola limpia")

    async def cmd_volume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            vol = int(context.args[0]) if context.args else 50
        except (ValueError, IndexError):
            await update.message.reply_text("Uso: /volume <0-100>")
            return
        await self.player.set_volume(vol)
        await update.message.reply_text(f"🔊 Volumen: {vol}")

    async def cmd_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        items = self.queue.all()
        if not items:
            await update.message.reply_text("La cola esta vacia.")
            return
        lines = [f"{i+1}. {it.title}" for i, it in enumerate(items)]
        await update.message.reply_text("Cola:\n" + "\n".join(lines))

    async def cmd_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cur = self.queue.current
        if cur is None:
            await update.message.reply_text("No hay ninguna cancion reproduciendose.")
        else:
            await update.message.reply_text(f"▶️ Sonando: {cur.title}")

    async def cmd_adduser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if len(args) != 2:
            await update.message.reply_text("Uso: /adduser <telegram_user_id> <rol>")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await update.message.reply_text("El ID debe ser un numero.")
            return
        role = args[1].lower()
        if role not in VALID_ROLES:
            await update.message.reply_text(f"Roles validos: {sorted(VALID_ROLES)}")
            return
        self.roles.set_role(user_id, role)
        await update.message.reply_text(
            f"Usuario {user_id} ahora es '{role}'."
        )

    async def cmd_removeuser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args:
            await update.message.reply_text("Uso: /removeuser <telegram_user_id>")
            return
        try:
            user_id = int(args[0])
        except ValueError:
            await update.message.reply_text("El ID debe ser un numero.")
            return
        if self.roles.remove_user(user_id):
            await update.message.reply_text(f"Usuario {user_id} removido.")
        else:
            await update.message.reply_text(f"El usuario {user_id} no estaba registrado.")