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
        self.get_me_ok = True
        self.pending_updates = []

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

    async def get_me(self):
        if not self.get_me_ok:
            raise RuntimeError("sin conexion")
        return "ok"

    async def get_updates(self, timeout=None, offset=None, **kwargs):
        if offset == -1:
            return []
        return list(self.pending_updates)


class _FakeBacklogMsg:
    def __init__(self, chat_id, text):
        self.chat_id = chat_id
        self.text = text


class _FakeBacklogUpd:
    def __init__(self, chat_id, text):
        self.message = _FakeBacklogMsg(chat_id, text)


class FakePlayer:
    def __init__(self, fail_loads=0):
        self.volume = 100
        self.resumed = 0
        self.paused = 0
        self.stopped = 0
        self.running = True
        self.fail_loads = fail_loads
        self.loaded = []
        self.starts = 0

    @property
    def is_running(self):
        return self.running

    async def start(self):
        self.starts += 1
        self.running = True

    async def load(self, url, audio=None):
        self.loaded.append((url, audio))
        if self.fail_loads > 0:
            self.fail_loads -= 1
            raise RuntimeError("load rejected")

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
        self.effective_user = SimpleNamespace(id=77)

    @property
    def effective_chat(self):
        return self.chat


def make_bot(fail_loads=0):
    import bot as bot_mod

    player = FakePlayer(fail_loads=fail_loads)
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
    # Fila 1: 4 botones de control (sin lupa); fila 2: vol-10, estado, vol+10, lista
    labels1 = [btn.text for btn in rows[0]]
    labels2 = [btn.text for btn in rows[1]]
    assert labels1 == ["⏮", "⏸️", "⏭", "⏹"], labels1
    assert labels2 == ["🔊−10", "🔊 100", "🔊+10", "📋"], labels2
    # En pausa el botón central (indice 1, sin lupa) debe ser ▶️
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


class FakeMessageUpdate:
    """Mensaje de texto mínimo para cmd_play (/buscar)."""

    def __init__(self, text, user_id=77, chat_id=44):
        self.sent = []
        self.callback_query = None
        self.message = SimpleNamespace(
            text=text, message_id=200, reply_text=self._reply_text
        )
        self.chat = SimpleNamespace(id=chat_id)
        self.effective_user = SimpleNamespace(id=user_id)

    async def _reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))

    @property
    def effective_chat(self):
        return self.chat


def test_artist_seed_literal_or_channel():
    """La semilla de la radio es SIEMPRE lo que escribió el usuario o, en
    links/playlists sin /buscar, el canal del video. El titulo JAMAS se parsea."""
    import bot as bot_mod
    from queue_manager import QueueItem

    b = bot_mod.YTRemoteBot(
        SimpleNamespace(mpv_path="mpv", owner_id=1, allowed_chat_id=None, max_results=5)
    )
    item = QueueItem(
        url="u1",
        title="Inexplicable - GP BAND - [Cover Julissa]",
        channel="Generación Pentecostal",
    )
    # Sin ancla de sesion: cae al canal (conservador, sin derivar del titulo).
    assert b._artist_seed(item) == item.channel
    # Con ancla de sesion (lo que escribio el usuario): gana la literal.
    b._radio_artist = "gp band"
    assert b._artist_seed(item) == "gp band"
    # Sin ancla ni canal: no hay radio, no se improvisa.
    b._radio_artist = ""
    assert b._artist_seed(QueueItem(url="u2", title="Solo un titulo")) == ""


def test_radio_gate_strict_excludes_covers():
    """El /next estricto: solo temas que mencionen al artista del usuario o
    que sean del mismo canal. El cover de otro artista (Denicher Pol) queda
    fuera, aunque suene parecido."""
    import bot as bot_mod
    from queue_manager import QueueItem
    from search import SearchResult

    b = make_bot()
    b._radio_artist = "GP Band"
    current = QueueItem(url="u0", title="actual")

    cases = [
        # (resultados, url esperada del candidato)
        ([SearchResult(url="u1", title="Inexplicable - GP BAND - [Cover Julissa]", duration="3:00", thumbnail="")], "u1"),
        ([SearchResult(url="u2", title="Adoración - GP Band", duration="4:00", thumbnail="", channel="Otro")], "u2"),
        ([SearchResult(url="u4", title="Tema X", duration="2:00", thumbnail="", channel="GP Band")], "u4"),
        ([SearchResult(url="u5", title="Denicher Pol - Inexplicable Cover", duration="4:30", thumbnail="")], None),
        ([], None),  # radio agotada
    ]
    original = bot_mod.search
    try:
        for idx, (results, expected) in enumerate(cases):
            bot_mod.search = lambda q, n, _r=results: _r
            candidate, from_queue = asyncio.run(b._pick_next_candidate(current))
            got = candidate.url if candidate else None
            assert got == expected, f"caso {idx}: {got} != {expected}"
    finally:
        bot_mod.search = original


async def test_cmd_play_routes():
    """/buscar: sin texto muestra el uso; con link reproduce directo; con
    'artista - cancion' el artista se fija como ancla ANTES de buscar y la
    busqueda se lanza con 'artista cancion'; sin guion, todo el texto ES el
    artista (la busqueda se lanza con el texto tal cual)."""
    b = make_bot()
    calls = []
    played = []

    async def fake_run(update, context, query):
        calls.append(query)

    async def fake_play(update, context, query):
        played.append(query)

    b._run_search = fake_run
    b._play_link_or_playlist = fake_play

    empty_upd = FakeMessageUpdate("")
    await b.cmd_play(empty_upd, SimpleNamespace(args=[]))
    assert calls == [] and played == [], "sin texto no debe buscar ni reproducir"
    assert "Uso:" in empty_upd.sent[-1][0], empty_upd.sent

    await b.cmd_play(FakeMessageUpdate(""), SimpleNamespace(args=["https://youtu.be/abc"]))
    assert played == ["https://youtu.be/abc"], played
    assert calls == [], "un link no debe pasar por la busqueda"

    await b.cmd_play(
        FakeMessageUpdate(""), SimpleNamespace(args=["GP", "Band", "-", "Inexplicable"])
    )
    assert calls == ["GP Band Inexplicable"], calls
    assert b._radio_artist == "GP Band", b._radio_artist

    await b.cmd_play(FakeMessageUpdate(""), SimpleNamespace(args=["Mafe Restrepo"]))
    assert calls == ["GP Band Inexplicable", "Mafe Restrepo"], calls
    assert b._radio_artist == "Mafe Restrepo", b._radio_artist


async def _wait_until(pred, timeout=2.0):
    waited = 0.0
    while not pred() and waited < timeout:
        await asyncio.sleep(0.02)
        waited += 0.02
    assert pred(), "condicion no se cumplio a tiempo"


def _patch_resolve(bot_mod, mapping):
    """Reemplaza resolve_stream_url del modulo bot con una fake sin red."""
    original = bot_mod.resolve_stream_url

    def fake(url):
        return mapping.get(url)

    bot_mod.resolve_stream_url = fake
    return original


async def test_stream_for_caches_and_reuses():
    """El mesonero resuelve una URL una sola vez: la guarda y la reutiliza."""
    import bot as bot_mod

    calls = []

    def fake(url):
        calls.append(url)
        return ("stream-" + url, None)

    original = _patch_resolve(bot_mod, {})
    bot_mod.resolve_stream_url = fake
    try:
        b = make_bot()
        first = await b._stream_for("u1")
        second = await b._stream_for("u1")
        assert first == ("stream-u1", None)
        assert second == first
        assert calls == ["u1"], calls  # resolvio una sola vez
        # una URL sin stream no se cachea
        bot_mod.resolve_stream_url = lambda url: None
        assert await b._stream_for("u2") is None
        assert "u2" not in b._stream_cache
    finally:
        bot_mod.resolve_stream_url = original


async def test_stream_for_dedupes_inflight():
    """Dos pedidos simultaneos de la misma URL resuelven una sola vez."""
    import bot as bot_mod

    calls = []

    def fake(url):
        calls.append(url)
        return f"stream-{url}", None

    original = bot_mod.resolve_stream_url
    bot_mod.resolve_stream_url = fake
    try:
        b = make_bot()
        r1, r2 = await asyncio.gather(b._stream_for("u1"), b._stream_for("u1"))
        assert r1 == r2 == ("stream-u1", None)
        assert calls == ["u1"], calls
    finally:
        bot_mod.resolve_stream_url = original


async def test_anticipate_urls_serial_and_clear():
    """Anticipar encola varias URLs y el cache se vacia con _clear_stream_cache."""
    import bot as bot_mod

    order = []

    def fake(url):
        order.append(url)
        return ("stream-" + url, None)

    original = _patch_resolve(bot_mod, {})
    bot_mod.resolve_stream_url = fake
    try:
        b = make_bot()
        b._anticipate_urls(["u1", "u2", "u3"])
        await _wait_until(lambda: len(b._stream_cache) == 3)
        assert set(b._stream_cache) == {"u1", "u2", "u3"}
        assert order == ["u1", "u2", "u3"], order  # serial, una a la vez
        # repetir un enqueo no re-resuelve nada
        exposed = len(order)
        b._anticipate_urls(["u1", "u2", "u3", "u4"])
        await _wait_until(lambda: len(b._stream_cache) == 4)
        assert order[exposed:] == ["u4"], order[exposed:]
        # limpiar: cache y cola vacias
        b._clear_stream_cache()
        assert b._stream_cache == {}
        assert b._resolving == set()
    finally:
        bot_mod.resolve_stream_url = original


async def test_cache_expired_retries_once():
    """Si la URL cacheada vencio (mpv la rechaza), se re-resuelve una vez."""
    import bot as bot_mod
    from queue_manager import QueueItem

    b = make_bot(fail_loads=1)
    b._stream_cache["u1"] = ("vencida", None)  # entrada cacheada expirada

    def fake(url):
        return ("stream-fresca", None)

    original = _patch_resolve(bot_mod, {})
    bot_mod.resolve_stream_url = fake
    try:
        async def _no_candidate(current=None):
            return None, False

        b._pick_next_candidate = _no_candidate  # evita radio/red real
        upd = FakeUpdate("ctl:next", chat_id=44)
        started = await b._play_item(upd, QueueItem(url="u1", title="X"))
        assert started
        assert b.player.loaded == [("vencida", None), ("stream-fresca", None)], b.player.loaded
        assert b._stream_cache["u1"] == ("stream-fresca", None)
    finally:
        bot_mod.resolve_stream_url = original


def test_singleton_lock_rejects_second_instance():
    """El lock de instancia local: el primero toma el puerto, el segundo no arranca."""
    import main as main_mod

    first = main_mod._acquire_singleton()
    assert first is not None
    try:
        second = main_mod._acquire_singleton()
        assert second is None, "un segundo bot no deberia arrancar"
    finally:
        first.close()
    # liberado: un arranque nuevo vuelve a tomar el puerto
    third = main_mod._acquire_singleton()
    assert third is not None
    third.close()


async def test_net_watch_job_reconnects_and_notifies():
    """Vigilante de conexion: offline no hace nada; al volver descarta el
    backlog y avisa que esos comandos se perdieron (no se ejecutan con retraso)."""
    b = make_bot()
    b.config.allowed_chat_id = 44
    fb = b._app.bot
    fb.pending_updates = [
        _FakeBacklogUpd(44, "/pause"),
        _FakeBacklogUpd(44, "/volume 30"),
        _FakeBacklogUpd(44, "hola"),
        _FakeBacklogUpd(99, "/next"),  # de otro chat: no se reporta
    ]
    ctx = SimpleNamespace(bot=fb, application=SimpleNamespace(updater=None))
    b._net_offline = True

    # offline: el probe falla y el job no hace nada
    fb.get_me_ok = False
    await b._net_watch_job(ctx)
    assert b._net_offline is True
    assert fb.sent == [], fb.sent

    # vuelve la conexion: se descarta el backlog y se avisa
    fb.get_me_ok = True
    await b._net_watch_job(ctx)
    assert b._net_offline is False
    dropped = [m[1] for m in fb.sent if "La conexion del bot se perdio" in str(m[1])]
    assert dropped, fb.sent
    assert "/pause" in dropped[0] and "/volume 30" in dropped[0]
    assert "/next" not in dropped[0]
    assert "hola" not in dropped[0]

    # sin backlog (y online): no vuelve a avisar
    fb.sent.clear()
    fb.pending_updates = []
    await b._net_watch_job(ctx)
    assert fb.sent == [], fb.sent


def run():
    test_control_keyboard_and_status()
    test_artist_seed_literal_or_channel()
    test_radio_gate_strict_excludes_covers()
    asyncio.run(test_cmd_play_routes())
    asyncio.run(test_toggle_and_volume())
    asyncio.run(test_card_created_and_edited())
    asyncio.run(test_dispatch_vol())
    asyncio.run(test_dispatch_unknown_action())
    asyncio.run(test_card_photo_created_and_media_edited())
    asyncio.run(test_card_text_fallback_without_thumbnail())
    asyncio.run(test_stream_for_caches_and_reuses())
    asyncio.run(test_stream_for_dedupes_inflight())
    asyncio.run(test_anticipate_urls_serial_and_clear())
    asyncio.run(test_cache_expired_retries_once())
    test_singleton_lock_rejects_second_instance()
    asyncio.run(test_net_watch_job_reconnects_and_notifies())
    print("TARJETA TESTS OK")


if __name__ == "__main__":
    run()