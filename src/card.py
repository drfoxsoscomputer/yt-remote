"""La tarjeta persistente del mini reproductor.

Antes vivia dentro de `bot.py`, entre el handler de `/start` y el de la cola.
Eso no era "muchas lineas": era que la tarjeta es UNA PIEZA con su propia
identidad (que mensaje es, en que chat, si es foto o texto) y su propio ciclo
de vida (crear, re-renderizar, re-colocar al final, borrar), y estaba
esparcida entre metodos que tambien hacen de todo lo demas.

Esta clase es esa pieza. El bot la instancia y le delega; los metodos del bot
(`_show_card`, `_render_card`...) quedan como delegacion de una linea para que
ni el bot ni las pruebas tengan que enterarse de que esto se movio de archivo.

QUE NECESITA DEL BOT, y solo esto:
    app.queue, app._paused, app._volume, app._max_height, app._restored,
    app._search_list_pending, app._from_card,
    app._persist_dirty(), app._reply(...), app._thumbnail_for(...)

Se pasa el bot entero en vez de inyectar diez colaboradores sueltos porque la
inversion de dependencias de este modulo no compra NADA todavia: no hay un
segundo cliente, no hay test que lo use sin Telegram, y las 15 delegaciones
del bot son el contrato real. Si aparece un segundo cliente (un panel web, un
test sin Telegram), SE INVIERTE; no antes.
"""

import asyncio
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from queue_manager import QueueItem

logger = logging.getLogger(__name__)

# Calidades que la tarjeta ofrece. Vive aqui porque el que decide es el boton
# "Calidad" de la tarjeta; `bot.py` lo reexporta para no romper sus imports.
QUALITY_LEVELS = (144, 240, 360, 480, 720, 1080)


class CardManager:
    """Dibuja, refresca y re-coloca la tarjeta del mini reproductor.

    No sabe de comandos, de cola ni de permisos: solo del mensaje.
    """

    def __init__(self, app) -> None:
        self._app = app
        # Identidad del mensaje-tarjeta. None = todavia no hay tarjeta.
        self.chat_id: int | None = None
        self.message_id: int | None = None
        self.is_photo: bool = False
        # Serial la re-creacion (borrar + enviar de nuevo): dos updates seguidos
        # no deben borrar dos veces para duplicar la card.
        self.lock: asyncio.Lock = asyncio.Lock()

    # --- Identidad -------------------------------------------------------

    async def exists_in(self, chat_id: int | None) -> bool:
        return chat_id is not None and self.chat_id == chat_id and self.message_id is not None

    # --- Texto y teclados ------------------------------------------------

    def truncate(self, text: str, limit: int = 80) -> str:
        """Trunca un texto con elipsis para que no rompa la tarjeta."""
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    def status_text(self) -> str:
        """Estado actual para la tarjeta persistente del mini reproductor.

        Linea 1: estado (Sonando/Pausado). Linea 2: titulo (truncado). El
        volumen no va en el texto: vive en la fila de botones de la tarjeta.
        """
        cur = self._app.queue.current
        if cur is None:
            return "No hay ninguna cancion sonando."
        state = "⏸️ Pausado" if self._app._paused else "▶️ Sonando"
        text = f"{state}\n🎵 {self.truncate(cur.title)}"
        if self._app._restored:
            self._app._restored = False
            text += "\n🔄 Retomada del cierre anterior: usa ▶ para reanudar."
        return text

    def control_keyboard(self) -> InlineKeyboardMarkup:
        """Teclado del mini reproductor persistente.

        Fila 1: ⏮ anterior | ▶/⏸ alternar play-pause | ⏭ siguiente | ⏹ detener
        Fila 2: 🔊−10 | <volumen actual> | 🔊+10 | 📋 lista
        Fila 3: ⚙️ Calidad: Np | 👥 Usuarios (solo admin puede usarlas)
        """
        play_pause = "▶️" if self._app._paused else "⏸️"
        keyboard = [
            [
                InlineKeyboardButton("⏮", callback_data="ctl:prev"),
                InlineKeyboardButton(play_pause, callback_data="ctl:pp"),
                InlineKeyboardButton("⏭", callback_data="ctl:next"),
                InlineKeyboardButton("⏹", callback_data="ctl:stop"),
            ],
            [
                InlineKeyboardButton("🔊−10", callback_data="ctl:vol-10"),
                InlineKeyboardButton(f"🔊 {self._app._volume}", callback_data="ctl:vol-info"),
                InlineKeyboardButton("🔊+10", callback_data="ctl:vol+10"),
                InlineKeyboardButton("📋", callback_data="ctl:lista"),
            ],
            [
                InlineKeyboardButton(
                    f"⚙️ Calidad: {(self._app._max_height or 1080)}p",
                    callback_data="ctl:calidad",
                ),
                InlineKeyboardButton("👥 Usuarios", callback_data="ctl:usuarios"),
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

    def quality_keyboard(self) -> InlineKeyboardMarkup:
        """Grilla de calidades para el admin (el vigente se marca con ✓)."""
        current = self._app._max_height or 1080
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

    # --- Dibujar ---------------------------------------------------------

    async def render_card(
        self, text: str, chat_id: int | None = None, item: QueueItem | None = None
    ) -> None:
        """Edita el mensaje persistente del mini reproductor (si existe).

        Si la tarjeta es una FOTO, edita miniatura + texto + teclado juntos
        (edit_message_media), asi al pasar de cancion cambia TODO el mensaje
        y no queda la miniatura de la cancion anterior clavada. Si la tarjeta
        es un mensaje de texto (sin miniatura disponible), edita solo el texto.

        No envia una tarjeta nueva: para crear la primera usar show_card.

        Si se pasa `item`, la miniatura se toma de ese item (feedback de carga
        del candidato); si no, del tema actual. La presentacion nunca altera
        queue._current: ese es estado de dominio y se commitea solo al exito.
        """
        if self.chat_id is None or self.message_id is None:
            return
        chat_id = chat_id or self.chat_id
        bot = self._app.app.bot
        source = item if item is not None else self._app.queue.current
        thumb = self._app._thumbnail_for(source) if source else ""
        try:
            if self.is_photo:
                if thumb:
                    await bot.edit_message_media(
                        media=InputMediaPhoto(media=thumb, caption=text),
                        chat_id=chat_id,
                        message_id=self.message_id,
                        reply_markup=self.control_keyboard(),
                    )
                else:
                    # Sin miniatura para el tema nuevo: se actualiza solo el
                    # texto (no se puede poner un mensaje de texto sobre una
                    # foto editando el media a vacio).
                    await bot.edit_message_caption(
                        caption=text,
                        chat_id=chat_id,
                        message_id=self.message_id,
                        reply_markup=self.control_keyboard(),
                    )
            else:
                try:
                    await bot.edit_message_text(
                        text,
                        chat_id=chat_id,
                        message_id=self.message_id,
                        reply_markup=self.control_keyboard(),
                    )
                except Exception as exc_text:  # noqa: BLE001
                    if "no text in the message" not in str(exc_text).lower():
                        raise
                    # El mensaje real es una FOTO (la identidad de la tarjeta
                    # se desincrono entre sesiones: el tipo persistido dice
                    # texto pero el mensaje es foto). El edit de texto falla
                    # con "There is no text in the message to edit"; se
                    # reintenta como caption y se corrige el tipo para que
                    # los proximos re-renders usen la ruta correcta.
                    await bot.edit_message_caption(
                        caption=text,
                        chat_id=chat_id,
                        message_id=self.message_id,
                        reply_markup=self.control_keyboard(),
                    )
                    self.is_photo = True
                    self._app._persist_dirty()
        except Exception as exc:  # noqa: BLE001 - no debe romper el control
            if "message is not modified" in str(exc):
                # Re-render con el mismo texto/teclado: es un no-op valido.
                return
            logger.warning("No se pudo editar la tarjeta: %s", exc)

    async def render_pending(self, title: str, item: QueueItem | None = None) -> None:
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
        await self.render_card(f"⏳ Cargando…\n🎵 {self.truncate(title)}", item=item)

    async def swap_keyboard(self, keyboard: InlineKeyboardMarkup) -> None:
        """Cambia SOLO el teclado de la tarjeta (edit_message_reply_markup).

        Ni borra la card ni re-renderiza texto/miniatura: la grilla de calidad
        reemplaza a los controles y al elegir/cerrar vuelven los controles,
        todo sobre el MISMO mensaje (sin desvanecimiento ni recreacion).
        """
        if self.chat_id is None or self.message_id is None:
            return
        try:
            await self._app.app.bot.edit_message_reply_markup(
                chat_id=self.chat_id,
                message_id=self.message_id,
                reply_markup=keyboard,
            )
        except Exception as exc:  # noqa: BLE001 - no debe romper la card
            if "message is not modified" in str(exc):
                return
            logger.warning("No se pudo cambiar el teclado de la tarjeta: %s", exc)

    # --- Ciclo de vida ---------------------------------------------------

    async def send_card(self, chat_id: int) -> None:
        """Crea la tarjeta persistente del mini reproductor en un chat.

        Primera creacion: se usa una FOTO (miniatura) con caption + teclado en
        un solo mensaje. Si el tema no tiene miniatura o falla el envio de foto,
        se crea como texto con botones.
        """
        text = self.status_text()
        thumb = (
            self._app._thumbnail_for(self._app.queue.current)
            if self._app.queue.current
            else ""
        )
        try:
            if thumb:
                try:
                    msg = await self._app.app.bot.send_photo(
                        chat_id,
                        photo=thumb,
                        caption=text,
                        reply_markup=self.control_keyboard(),
                    )
                    self.is_photo = True
                except Exception as exc_photo:  # noqa: BLE001
                    logger.warning("Fallo send_photo, cayendo a send_message: %s", exc_photo)
                    msg = await self._app.app.bot.send_message(
                        chat_id,
                        text,
                        reply_markup=self.control_keyboard(),
                    )
                    self.is_photo = False
            else:
                msg = await self._app.app.bot.send_message(
                    chat_id,
                    text,
                    reply_markup=self.control_keyboard(),
                )
                self.is_photo = False
            self.chat_id = chat_id
            self.message_id = msg.message_id
            # Persistir identidad y tipo YA: si el bot se reinicia, sabe que
            # tarjeta borrar al arrancar y con que tipo re-renderizar.
            self._app._persist_dirty()
        except Exception as exc:  # noqa: BLE001 - no debe romper el control
            logger.warning("No se pudo crear la tarjeta: %s", exc)

    async def show_card(self, chat_id: int) -> None:
        """Muestra o refresca la tarjeta del mini reproductor en un chat."""
        if self.chat_id is None or self.message_id is None:
            await self.send_card(chat_id)
            return
        if self.chat_id != chat_id:
            await self.send_card(chat_id)
            return
        await self.render_card(self.status_text(), chat_id)

    async def remove(self) -> None:
        """Borra la tarjeta persistente si existe (con su desvanecimiento).

        No envia ninguna nueva: deja la card en None para que el siguiente
        show_card/send_card cree una fresca al final de la conversacion.
        Seriada con lock y tolerante a mensajes que Telegram no deja
        borrar (viejos).
        """
        async with self.lock:
            old_chat = self.chat_id
            old_msg = self.message_id
            self.chat_id = None
            self.message_id = None
            if old_msg is not None and old_chat is not None:
                try:
                    await self._app.app.bot.delete_message(old_chat, old_msg)
                except Exception as exc:  # noqa: BLE001 - no debe romper la card
                    logger.warning("No se pudo borrar la tarjeta vieja: %s", exc)

    async def reposition(self, chat_id: int) -> None:
        """Re-crea la tarjeta como el ULTIMO mensaje del chat.

        Borra la tarjeta vieja (con su animacion de desvanecimiento) y la
        re-envia al final de la conversacion: los mensajes y comandos quedan
        arriba y la tarjeta pasa a ser siempre la ultima visualizacion.

        Si Telegram no deja borrar la vieja (mensaje muy antiguo), se tolera
        el fallo y solo se envía la nueva.
        """
        await self.remove()
        await self.send_card(chat_id)

    async def dejar_al_final(self, chat_id: int) -> None:
        """Re-envia la tarjeta al final del chat si vive ahi.

        Se llama despues de CADA mensaje que el bot suelta por su cuenta
        (bienvenida, aviso de red, aviso de rol). Antes cada sitio tenia que
        acordarse de reposicionar la tarjeta, y los que no se acordaban la
        dejaban clavada arriba del aviso.

        Excepcion: con el listado de /buscar en pantalla NO se mueve, porque
        ese listado va debajo a proposito y lo gestiona el boton de elegir o de
        cancelar.
        """
        if not await self.exists_in(chat_id):
            return
        if self._app._search_list_pending:
            return
        await self.reposition(chat_id)

    async def enviar_al_chat(self, chat_id, text, **kwargs):
        """Envia un mensaje al chat del grupo dejando la tarjeta al final.

        Es el UNICO punto por donde el bot escribe por su cuenta, para que la
        tarjeta vuelva sola sin depender de que cada sitio se acuerde. No se
        usa para el listado de /buscar ni para la lista de la tarjeta (los dos
        son paneles que abre el usuario y que viven debajo a proposito).
        """
        msg = await self._app.app.bot.send_message(chat_id, text, **kwargs)
        await self.dejar_al_final(chat_id)
        return msg

    # --- Integracion con los handlers ------------------------------------

    async def send_track_card(
        self, update, caption: str, item: QueueItem | None
    ) -> None:
        """Refresca la tarjeta persistente del mini reproductor.

        Es la unica confirmacion visual: miniatura + estado + botones se
        re-renderizan en el mismo mensaje (nada de fotos sueltas duplicadas).
        El caption se usa solo como fallback si no hay item que reflejar.
        """
        if self._app._from_card:
            # Desde un boton de la tarjeta, el cierre de _on_control ya
            # re-renderiza la tarjeta en su lugar.
            return
        if item is None:
            await self._app._reply(update, caption)
            return
        chat = update.effective_chat
        if chat is None:
            return
        await self.show_card(chat.id)

    def with_reposition(self, handler):
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

        async def wrapper(update, context) -> None:
            chat_id = update.effective_chat.id if update.effective_chat else None
            existia = await self.exists_in(chat_id)
            await handler(update, context)
            if existia and await self.exists_in(chat_id):
                if self._app._search_list_pending:
                    # Listado de /buscar visible: la card no se mueve.
                    return
                await self.reposition(chat_id)  # type: ignore[arg-type]

        wrapper.__name__ = getattr(handler, "__name__", "wrapper")
        return wrapper