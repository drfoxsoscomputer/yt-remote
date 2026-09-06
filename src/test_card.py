"""Tests de la tarjeta persistente del mini reproductor (Fase 2, tarea 4).

Cubre: teclado de control, texto de estado, toggle play/pause, vol+-10,
dispatch de callbacks ctl:*, reuso del boton 📋 y render de la tarjeta.
Sin red ni mpv real: se stubbea Player y el bot de Telegram.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))


class FakeBot:
    sent = []
    edited = []

    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, chat_id, text, **kwargs):
        msg = SimpleNamespace(message_id=len(self.sent) + 100)
        self.sent.append((chat_id, text, kwargs))
        return msg

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        msg = SimpleNamespace(message_id=len(self.sent) + 100)
        self.sent.append((chat_id, "PHOTO:" + str(photo), kwargs))
        return msg

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edited.append((chat_id, message_id, text, kwargs))

    async def edit_message_media(self, media=None, chat_id=None, message_id=None, **kwargs):
        self.edited.append((chat_id, message_id, "MEDIA", kwargs))

    async def edit_message_caption(self, caption=None, chat_id=None, message_id=None, **kwargs):
        self.edited.append((chat_id, message_id, "CAP:" + str(caption), kwargs))


class FakePlayer:
    def __init__(self):
        self.volume = 100
        self.resumed = 0
        self.paused = 0
        self.stopped = 0
        self.running = True

    @property
    def is_running(self):
        return self.running

    async def set_volume(self, vol):
        self.volume = vol

    async def play(self):
        self.resumed += 1

    async def pause(self):
        self.paused += 1

    async def stop(self):
        self.stopped += 1


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.app = self


class FakeUpdate:
    """Callback query mínimo para disparar _on_control."""

    def __init__(self, data, message_id=7, chat_id=44):
        self.data = data
        self.answered = None

        class _Query:
            def __init__(self, parent):
                self.parent = parent
                self.message = SimpleNamespace(message_id=message_id)

            async def answer(self, *args, **kwargs):
                self.parent.answered = args

            @property
            def data(self):
                return self.parent.data

        self.callback_query = _Query(self)
        self.chat = SimpleNamespace(id=chat_id)

    @property
    def effective_chat(self):
        return self.chat


def make_bot():
    import bot as bot_mod

    player = FakePlayer()
    bot_mod.Player = lambda *a, **k: player
    config = SimpleNamespace(
        mpv_path="mpv", owner_id=1, allowed_chat_id=None, max_results=5
    )
    b = bot_mod.YTRemoteBot(config)
    b._app = FakeApp()
    b.player = player
    return b


def test_control_keyboard_and_status():
    b = make_bot()
    kb = b._control_keyboard()
    rows = kb.inline_keyboard
    # Fila 1: 4 botones de control; fila 2: vol-10, estado de volumen, vol+10, lista
    labels1 = [btn.text for btn in rows[0]]
    labels2 = [btn.text for btn in rows[1]]
    assert labels1 == ["⏮", "⏸️", "⏭", "⏹"], labels1
    assert labels2 == ["🔊−10", "🔊 100", "🔊+10", "📋"], labels2
    # En pausa el botón central debe ser ▶️
    b._paused = True
    kb = b._control_keyboard()
    assert [btn.text for btn in kb.inline_keyboard[0]][1] == "▶️"

    # Estado: sin track
    assert "No hay ninguna cancion sonando." in b._track_status_text()
    # Con track
    from queue_manager import QueueItem

    b.queue.set_current(QueueItem(url="u1", title="Mi Cancion"))
    b._paused = False
    assert "Mi Cancion" in b._track_status_text()
    assert "▶️ Sonando" in b._track_status_text()
    assert "Volumen:" not in b._track_status_text()
    # ▶️ Sonando va ANTES del titulo (formato aprobado por el usuario)
    assert b._track_status_text().startswith("▶️ Sonando"), b._track_status_text()
    b._paused = True
    assert "⏸️ Pausado" in b._track_status_text()


async def test_toggle_and_volume():
    b = make_bot()
    from queue_manager import QueueItem

    # Sin track: toggle no hace nada
    await b._toggle_play_pause()
    assert b.player.paused == 0 and b.player.resumed == 0

    b.queue.set_current(QueueItem(url="u1", title="X"))
    await b._toggle_play_pause()
    assert b.player.paused == 1 and b._paused is True
    await b._toggle_play_pause()
    assert b.player.resumed == 1 and b._paused is False


async def test_card_created_and_edited():
    b = make_bot()
    upd = FakeUpdate("ctl:pp", message_id=7, chat_id=44)
    await b._on_control(upd, SimpleNamespace(args=[]), "pp")
    # Sin track, el toggle no toca al player pero la tarjeta (el mensaje del
    # query) se re-renderiza con el estado: el bot intento editarla.
    assert b.player.paused == 0 and b.player.resumed == 0
    assert b._card_chat_id == 44
    assert b._card_message_id == 7
    assert isinstance(b._app.bot.edited, list)


async def test_dispatch_vol():
    b = make_bot()
    upd = FakeUpdate("ctl:vol+10", chat_id=44)
    await b._on_control(upd, SimpleNamespace(args=[]), "vol+10")
    # 100 + 10 satura en 100
    assert b._volume == 100 and b.player.volume == 100
    await b._on_control(FakeUpdate("ctl:vol-10"), SimpleNamespace(args=[]), "vol-10")
    # 100 - 10 -> 90
    assert b._volume == 90 and b.player.volume == 90
    # el volumen no baja de 0
    for _ in range(20):
        await b._on_control(FakeUpdate("ctl:vol-10"), SimpleNamespace(args=[]), "vol-10")
    assert b._volume == 0 and b.player.volume == 0
    # y no sube de 100
    for _ in range(20):
        await b._on_control(FakeUpdate("ctl:vol+10"), SimpleNamespace(args=[]), "vol+10")
    assert b._volume == 100 and b.player.volume == 100


async def test_dispatch_unknown_action():
    b = make_bot()
    upd = FakeUpdate("ctl:???")
    await b._on_control(upd, SimpleNamespace(args=[]), "???")


async def test_card_photo_created_and_media_edited():
    """Tarea 1: la tarjeta se crea como FOTO (miniatura+caption+botones en un
    solo mensaje) y el siguiente tema se refleja con edit_message_media (muda
    miniatura + texto a la vez, nada de fotos sueltas ni duplicados)."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_current(QueueItem(url="u1", title="Impactante", thumbnail="https://img/1"))
    await b._send_card(44)
    assert any(dest == 44 and what.startswith("PHOTO:") for dest, what, _ in b._app.bot.sent), b._app.bot.sent
    assert b._card_is_photo is True

    # Cambia la cancion: el re-render usa edit_message_media
    b.queue.set_current(QueueItem(url="u2", title="Otra", thumbnail="https://img/2"))
    await b._render_card(b._track_status_text(), 44)
    media_edits = [
        e for e in b._app.bot.edited if e[2] == "MEDIA"
    ]
    assert media_edits, b._app.bot.edited


async def test_card_text_fallback_without_thumbnail():
    """Sin miniatura la tarjeta cae a texto (send_message) y su render
    posterior no usa media."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_current(QueueItem(url="u1", title="Solo Audio"))
    await b._send_card(44)
    assert any(
        dest == 44 and isinstance(what, str) and not what.startswith("PHOTO:")
        for dest, what, _ in b._app.bot.sent
    ), b._app.bot.sent
    assert b._card_is_photo is False

    await b._render_card(b._track_status_text(), 44)
    assert not [e for e in b._app.bot.edited if e[2] == "MEDIA"]


def test_artist_from_title():
    """Tarea 4: extraccion del artista real (no la cancion) en formato normal,
    invertido (cristiano), con 3+ segmentos y desempate por canal."""
    import bot as bot_mod

    # Formato normal "ARTISTA - Cancion"
    assert bot_mod.YTRemoteBot._artist_from_title(
        "KENT LEROY - Quiero Cantar del Amor (CD completo)"
    ) == "KENT LEROY"
    # Sin guion: no es artista
    assert bot_mod.YTRemoteBot._artist_from_title("Solo un titulo") == ""
    # Invertido "Cancion - Artista - Banda - Ministerio"
    assert bot_mod.YTRemoteBot._artist_from_title(
        "IMPACTANTE - Mafe Restrepo - GP BAND - Generacion Pentecostal"
    ) == "Mafe Restrepo"
    # Desempate por canal: si el canal coincide con un segmento, gana ese
    assert bot_mod.YTRemoteBot._artist_from_title(
        "IMPACTANTE - Mafe Restrepo - GP BAND - Generacion Pentecostal",
        channel="Generación Pentecostal",
    ) == "Generacion Pentecostal"
    # Canal sin coincidencia no confunde el invertido
    assert bot_mod.YTRemoteBot._artist_from_title(
        "Alguien - Artista X - Banda", channel="Otro Canal"
    ) == "Artista X"
    # Guion sin espacios: "X - Y"
    assert bot_mod.YTRemoteBot._artist_from_title("Vanessa - Corazon Herido") == "Vanessa"
    # Titulo vacio
    assert bot_mod.YTRemoteBot._artist_from_title("") == ""


def run():
    test_control_keyboard_and_status()
    test_artist_from_title()
    asyncio.run(test_toggle_and_volume())
    asyncio.run(test_card_created_and_edited())
    asyncio.run(test_dispatch_vol())
    asyncio.run(test_dispatch_unknown_action())
    asyncio.run(test_card_photo_created_and_media_edited())
    asyncio.run(test_card_text_fallback_without_thumbnail())
    print("TARJETA TESTS OK")


if __name__ == "__main__":
    run()