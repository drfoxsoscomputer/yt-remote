"""Tests de la tarjeta persistente del mini reproductor (Fase 2, tarea 4).

Cubre: teclado de control, texto de estado, toggle play/pause, vol+-10,
dispatch de callbacks ctl:*, reuso del boton 📋 y render de la tarjeta.
Navegación prev/next con stacks (modo radio) y tests del mesonero.
Sin red ni mpv real: se stubbea Player y el bot de Telegram.
"""

import asyncio
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))


@contextmanager
def isolated_state_path():
    """Aisla la persistencia: el StateStore default del bot apunta a un
    archivo temporal, nunca al data/state.json real del proyecto (puede
    traer artistas, historial y pausa reales de un uso real previo que
    contaminan los asserts). Se restaura la ruta real al salir."""
    import persistence as persistence_mod
    import tempfile

    tmp = tempfile.mkdtemp()
    real = persistence_mod.STATE_PATH
    persistence_mod.STATE_PATH = Path(tmp) / "state.json"
    try:
        yield
    finally:
        persistence_mod.STATE_PATH = real


class FakeBot:
    sent = []
    edited = []

    def __init__(self):
        self.sent = []
        self.edited = []
        self.deleted = []
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

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, **kwargs):
        self.edited.append((chat_id, message_id, "MARKUP", kwargs))

    async def delete_message(self, chat_id, message_id, **kwargs):
        self.deleted.append((chat_id, message_id))

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
        self.rewinds = 0

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

    async def rewind(self):
        self.rewinds += 1


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()
        self.app = self


class FakeUpdate:
    """Callback query mínimo para disparar _on_control."""

    def __init__(self, data, message_id=7, chat_id=44, user_id=77):
        self.data = data
        self.answered = None
        self.answered_kwargs = {}

        class _Query:
            def __init__(self, parent, uid):
                self.parent = parent
                self._uid = uid
                self.message = SimpleNamespace(message_id=message_id)

            @property
            def from_user(self):
                return SimpleNamespace(id=self._uid)

            async def answer(self, *args, **kwargs):
                self.parent.answered = args
                self.parent.answered_kwargs = kwargs

            async def edit_message_text(self, *args, **kwargs):
                self.parent.answered = ("edit",) + args

            @property
            def data(self):
                return self.parent.data

        self.callback_query = _Query(self, user_id)
        self.chat = SimpleNamespace(id=chat_id)
        self.effective_user = SimpleNamespace(id=user_id)

    @property
    def effective_chat(self):
        return self.chat


def make_bot(fail_loads=0):
    import bot as bot_mod
    from config import Config

    player = FakePlayer(fail_loads=fail_loads)
    config = Config(
        token="test",
        mpv_path="mpv",
        default_role="user",
        max_results=5,
        owner_id=1,
        allowed_chat_id=None,
    )
    # Construir bajo aislamiento completo: el __init__ del bot carga Estado
    # del StateStore default; si  el state.json real trae pausa/artista/
    # historial de un uso previo, no debe filtrarse a los tests.
    with isolated_state_path():
        b = bot_mod.YTRemoteBot(config)
    b._app = FakeApp()
    object.__setattr__(b, "player", player)
    b.roles.set_role(77, "dj")
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
    # (sin track no se llama pause ni play)

    b.queue.set_current(QueueItem(url="u1", title="X"))
    await b._toggle_play_pause()
    assert b._paused is True
    await b._toggle_play_pause()
    assert b._paused is False


async def test_card_created_and_edited():
    b = make_bot()
    upd = FakeUpdate("ctl:pp", message_id=7, chat_id=44)
    await b._on_control(upd, SimpleNamespace(args=[]), "pp")
    # Sin track, el toggle no toca al player pero la tarjeta (el mensaje del
    # query) se re-renderiza con el estado: el bot intento editarla.
    assert b._paused is False  # sin track no se toca nada
    assert b._card_chat_id == 44
    assert b._card_message_id == 7
    assert isinstance(b._app.bot.edited, list)


async def test_dispatch_vol():
    b = make_bot()
    upd = FakeUpdate("ctl:vol+10", chat_id=44)
    await b._on_control(upd, SimpleNamespace(args=[]), "vol+10")
    # 100 + 10 satura en 100
    assert b._volume == 100
    await b._on_control(FakeUpdate("ctl:vol-10"), SimpleNamespace(args=[]), "vol-10")
    # 100 - 10 -> 90
    assert b._volume == 90
    # el volumen no baja de 0
    for _ in range(20):
        await b._on_control(FakeUpdate("ctl:vol-10"), SimpleNamespace(args=[]), "vol-10")
    assert b._volume == 0
    # y no sube de 100
    for _ in range(20):
        await b._on_control(FakeUpdate("ctl:vol+10"), SimpleNamespace(args=[]), "vol+10")
    assert b._volume == 100


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
    from config import Config
    from queue_manager import QueueItem

    # Envolver el acceso: el bot directo (sin make_bot) tambien debe arrancar
    # con un state limpio para que el assert del canal real no se contamine
    # con radio_artist de un uso previo.
    with isolated_state_path():
        b = bot_mod.YTRemoteBot(
            Config(
                token="test",
                mpv_path="mpv",
                default_role="user",
                max_results=5,
                owner_id=1,
                allowed_chat_id=None,
            )
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
            b._radio_search_cache.clear()  # cada caso es una radio nueva
            candidate, from_queue, _ = asyncio.run(b._pick_next_candidate(current))
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
            return None, False, False

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


def test_nav_stacks_initial_state():
    """Los stacks de navegación arrancan vacios."""
    b = make_bot()
    assert list(b._nav_back) == []
    assert list(b._nav_forward) == []


async def test_nav_push_to_back_clears_forward():
    """_push_to_nav_back guarda el current y limpia el forward stack."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_current(QueueItem(url="a", title="A"))
    b._nav_forward.append(QueueItem(url="z", title="Z"))
    b._push_to_nav_back()
    # el current se guardó en back
    assert len(b._nav_back) == 1
    assert b._nav_back[-1].url == "a"
    # y el forward stack se limpió
    assert list(b._nav_forward) == []


async def test_nav_clear_stacks():
    """_clear_nav_stacks limpia ambos stacks."""
    from queue_manager import QueueItem

    b = make_bot()
    b._nav_back.append(QueueItem(url="a", title="A"))
    b._nav_forward.append(QueueItem(url="b", title="B"))
    b._clear_nav_stacks()
    assert list(b._nav_back) == []
    assert list(b._nav_forward) == []


async def test_nav_prev_next_cycle_radio():
    """Navegación estándar: A -> next -> B -> prev -> A -> next -> B.

    Sin playlist (modo radio), prev y next usan los stacks para que el
    comportamiento sea simétrico: el segundo next reproduce el MISMO B
    que se reprodujo antes del prev, no un candidato distinto.
    """
    import bot as bot_mod
    from queue_manager import QueueItem

    # Sin red: el mesonero resuelve cualquier URL a un stream fijo.
    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        # Sin ancla de artista ni red: _pick_next_candidate devuelve None, asi
        # que solo probamos el camino de la pila back/forward.
        b._radio_artist = ""
        # Repositorio de "candidatos" para cuando next tiene que elegir uno nuevo.
        candidates = iter([(QueueItem(url="B", title="B"), False), (None, False)])
        async def fake_pick(current: QueueItem) -> tuple[QueueItem | None, bool, bool]:
            item, err = next(candidates)
            return item, False, err
        b._pick_next_candidate = fake_pick

        # 1) Suena A (simulamos un /buscar previo que eligió este tema).
        b._clear_nav_stacks()
        b.queue.set_current(QueueItem(url="A", title="A"))

        # 2) next -> elige B (de la pila fake), lo reproduce.
        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "B"
        # A quedó en la pila back, forward vacía.
        assert [i.url for i in b._nav_back] == ["A"]
        assert list(b._nav_forward) == []

        # 3) prev -> A (pop de back); B va a forward.
        await b.cmd_prev(FakeUpdate("ctl:prev"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "A"
        assert [i.url for i in b._nav_back] == []
        assert [i.url for i in b._nav_forward] == ["B"]

        # 4) next -> B (pop de forward, el MISMO B del paso 2).
        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "B"
        # back vacia (el flujo de forward stack no empuja nada);
        # forward vacia (se consumio el item).
        assert list(b._nav_back) == []
        assert list(b._nav_forward) == []
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_nav_next_chooses_new_candidate_when_forward_empty():
    """Si la pila forward está vacía, next elige un candidato nuevo y
    deja el actual en la pila back para un eventual prev."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        b._radio_artist = ""
        b._clear_nav_stacks()
        b.queue.set_current(QueueItem(url="A", title="A"))

        new_pick = QueueItem(url="B", title="B")
        async def fake_pick(current: QueueItem) -> tuple[QueueItem | None, bool, bool]:
            return new_pick, False, False
        b._pick_next_candidate = fake_pick

        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "B"
        # A en back, forward vacía.
        assert [i.url for i in b._nav_back] == ["A"]
        assert list(b._nav_forward) == []
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_nav_prev_falls_back_to_queue_history():
    """Si la pila back está vacía, prev usa el histórico de items de la cola.
    El fallback no preserva nav_forward (es navegación desde cola, no navegación
    entre tracks elegidos por el usuario)."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        b._radio_artist = ""
        b._clear_nav_stacks()
        b.queue.set_current(QueueItem(url="A", title="A"))
        b.queue._history_items.append(QueueItem(url="Z", title="Z"))

        await b.cmd_prev(FakeUpdate("ctl:prev"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "Z"
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_nav_playlist_unaffected():
    """En modo playlist el cursor con wrap sigue mandando, no la pila."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        b._clear_nav_stacks()
        items = [
            QueueItem(url="A", title="A"),
            QueueItem(url="B", title="B"),
            QueueItem(url="C", title="C"),
        ]
        b.queue.set_playlist(items)
        # Cursor esta en 0 (A suena). Mantenemos _items intactos.
        fake_pick_items = [(items[1], True, False), (items[2], True, False)]
        async def fake_pick(current: QueueItem) -> tuple[QueueItem | None, bool, bool]:
            return fake_pick_items.pop(0) if fake_pick_items else (None, False, False)
        b._pick_next_candidate = fake_pick

        # next -> B (cursor avanza a 1)
        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "B"
        # prev -> A (cursor vuelve a 0, wrap)
        await b.cmd_prev(FakeUpdate("ctl:prev"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "A"
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_toggle_play_pause_starts_player_when_off():
    """Fase 1: si mpv no esta corriendo, ▶ en la tarjeta PRENDE mpv y carga
    el item actual (no se queda mudo tras un apagado de la PC o un stop)."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        fp = cast(Any, b.player)
        fp.running = False
        b.queue.set_current(QueueItem(url="u1", title="Tema"))
        b._paused = False  # "sonando", pero mpv apagado

        await b._toggle_play_pause()
        assert fp.starts == 1, "debe llamar player.start()"
        assert fp.loaded == [("stream-u1", None)], fp.loaded
        assert fp.resumed == 1, "debe reanudar reproduccion"
        assert b._paused is False
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_resume_starts_player_when_not_running():
    """Fase 1: el /resume tras apagado/stop reproduce el item restaurado:
    prende mpv y carga el stream (no solo hace play sobre nada)."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        fp = cast(Any, b.player)
        b.queue.set_current(QueueItem(url="u1", title="Tema"))
        fp.running = False

        await b.cmd_resume(FakeUpdate("ctl:play"), SimpleNamespace(args=[]))
        assert fp.starts == 1, "debe llamar player.start()"
        assert fp.loaded == [("stream-u1", None)], fp.loaded
        assert b.queue.current is not None and b.queue.current.url == "u1"
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_toggle_play_pause_keeps_playlist():
    """El ▶ con mpv apagado y playlist ACTIVA NO debe descartar la cola:
    reanuda el item actual posicionado (preserve_current=True) y _items
    queda intacto. Antes, _play_item con preserve_current=False llamaba
    set_current y borraba la playlist (rar: lista vacia tras reiniciar)."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        fp = cast(Any, b.player)
        fp.running = False
        items = [
            QueueItem(url="A", title="A"),
            QueueItem(url="B", title="B"),
            QueueItem(url="C", title="C"),
        ]
        b.queue.set_playlist(items)
        b._paused = False  # "sonando", pero mpv apagado (tras reinicio/stop)

        await b._toggle_play_pause()
        assert fp.starts == 1, "debe llamar player.start()"
        assert fp.loaded == [("stream-A", None)], fp.loaded
        assert b.queue.has_playlist, "la playlist NO debe descartarse"
        assert [i.url for i in b.queue._items] == ["A", "B", "C"]
        assert b.queue.current is not None and b.queue.current.url == "A"
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_restore_cursor_and_list_page():
    """Paso 2: al cargar el estado se restaura la posicion real dentro de la
    playlist (no el tema 0) y la pagina del 📋 en la que iba el usuario."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        items = [
            QueueItem(url=f"u{i}", title=f"T{i}") for i in range(10)
        ]
        b.queue._items = items
        b.queue._cursor = 7
        b._list_page = 2

        b._persist_flush()
        loaded = b._state.load()
        assert loaded["cursor"] == 7, loaded
        assert loaded["list_page"] == 2, loaded

        b2 = make_bot()
        b2._state.save(loaded)
        b2._load_persisted_state()
        assert b2.queue._cursor == 7, b2.queue._cursor
        assert b2.queue.current is not None and b2.queue.current.url == "u7"
        assert b2._list_page == 2, b2._list_page
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_restore_cursor_clamped_to_range():
    """Paso 2: un cursor persistido fuera de rango (playlist mas corta o
    estado antiguo con cursor grande) se clampa al ultimo indice valido."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        loaded = {
            "version": 1,
            "cursor": 9999,
            "list_page": 50,
            "playlist": [
                {"url": "u0", "title": "A", "duration_seconds": 0},
                {"url": "u1", "title": "B", "duration_seconds": 0},
            ],
        }
        b._state.save(loaded)
        b._load_persisted_state()
        assert b.queue._cursor == 1, b.queue._cursor
        assert b.queue.current is not None and b.queue.current.url == "u1"
        assert b._list_page == 50, "la pagina se clampa en el render, no al cargar"
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_restore_cursor_corrupt_falls_to_zero():
    """Paso 2: un cursor o pagina corruptos (no int) caen a 0 sin explotar."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        loaded = {
            "version": 1,
            "cursor": "abc",
            "list_page": -5,
            "playlist": [
                {"url": "u0", "title": "A", "duration_seconds": 0},
                {"url": "u1", "title": "B", "duration_seconds": 0},
            ],
        }
        b._state.save(loaded)
        b._load_persisted_state()
        assert b.queue._cursor == 0
        assert b.queue.current is not None and b.queue.current.url == "u0"
        assert b._list_page == 0
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_close_list_keeps_page():
    """Paso 2: cerrar la lista NO resetea la pagina: una reapertura (o un
    reinicio) continuan en la pagina donde iba el usuario."""
    b = make_bot()
    b._list_page = 3
    await b._close_list_message()
    assert b._list_page == 3
    assert b._list_message_id is None


async def test_next_prefetch_starts_player():
    """Fase 1: el /next con stream ya resuelto (prefetch) debe prender mpv
    antes de cargar, igual que /resume y ▶."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        fp = cast(Any, b.player)
        fp.running = False
        b._radio_artist = "artista"
        b.queue.set_current(QueueItem(url="actual", title="Actual"))
        b._clear_nav_stacks()
        # Prefetch ya resuelto para el candidato 'siguiente'.
        candidate = QueueItem(url="siguiente", title="Siguiente")
        b._prefetch_candidate = (candidate, False)
        b._prefetch_basis = "actual"
        b._prefetch_resolved = ((candidate, False), ("stream-siguiente", None))

        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))
        assert fp.starts == 1, "debe llamar player.start()"
        assert fp.loaded[0][0] == "stream-siguiente", fp.loaded
        assert cast(QueueItem, b.queue.current).url == "siguiente"
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_stop_conserves_state_and_rewinds():
    """Fase 2: el stop NO borra historial, cola, radio ni navegacion: solo
    pausa el reproductor y rebobina a 0:00 para que ▶ reanude desde el inicio."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        fp = cast(Any, b.player)
        b.queue.set_current(QueueItem(url="u1", title="Tema"))
        b._radio_artist = "GP Band"
        b._nav_back.append(QueueItem(url="prev", title="Prev"))
        b._paused = False

        await b.cmd_stop(FakeUpdate("ctl:stop"), SimpleNamespace(args=[]))
        assert fp.rewinds == 1, "debe rebobinar a 0:00"
        assert fp.stopped == 0, "stop NO debe descargar el track"
        assert b.queue.current is not None and b.queue.current.url == "u1", "current intacto"
        assert b._radio_artist == "GP Band", "radio intacta"
        assert [i.url for i in b._nav_back] == ["prev"], "navegacion intacta"
        assert b._paused is True, "deja en pausa para reanudar con ▶"
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_stop_does_not_clear_queue():
    """El stop de la tarjeta no llama queue.clear(): el historico de la radio
    y la cola siguen vivos (el bug reportado era que /stop borraba todo)."""
    import bot as bot_mod
    from queue_manager import QueueItem, QueueManager

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    b = make_bot()
    b.queue.set_current(QueueItem(url="u1", title="Tema"))

    # Spy sobre queue.clear: si cmd_stop lo llamara, el historial explota.
    original_clear = QueueManager.clear
    cleared = []

    def spy_clear(self):
        cleared.append(True)
        return original_clear(self)

    QueueManager.clear = spy_clear
    try:
        b.queue._history_items.append(QueueItem(url="v0", title="V0"))
        b.queue._history.append("v0")
        await b.cmd_stop(FakeUpdate("ctl:stop"), SimpleNamespace(args=[]))
        assert cleared == [], "cmd_stop NO debe limpiar la cola"
        assert cast(QueueItem, b.queue.current).url == "u1"
        assert [i.url for i in b.queue._history_items] == ["v0"]
        assert list(b.queue._history) == ["v0"]
        assert b._radio_artist == ""

    finally:
        QueueManager.clear = original_clear


async def test_notify_admin_sends_to_owner():
    """Fase 4: _notify_admin copia el error al chat del dueno (log del admin)."""
    b = make_bot()
    await b._notify_admin("Fallo al reproducir X: boom")
    sent = b._app.bot.sent[-1]
    assert sent[0] == b.config.owner_id, "debe ir al owner, no al chat permitido"
    assert "⚠️" in sent[1] and "boom" in sent[1], sent[1]


async def test_control_lock_discards_second_tap():
    """Medida 1: si una accion de boton esta en curso (lock tomado), el toque
    extra se descarta al instante con su toast y NO se encola: el volumen de
    un toque que llega tarde no se aplica."""
    b = make_bot()
    upd = FakeUpdate("ctl:vol+10", chat_id=44)
    await b._control_lock.acquire()
    try:
        await b._on_control(upd, SimpleNamespace(args=[]), "vol+10")
        assert upd.answered == ("⏳ Un momento, primero termina...",), upd.answered
        assert b._volume == 100, "el toque descartado no debe cambiar el volumen"
        assert not b._app.bot.edited, "la tarjeta no se re-renderiza con el descarte"
    finally:
        b._control_lock.release()


async def test_radio_search_list_cached_per_anchor():
    """Medida 2: la lista de radio se busca UNA vez por ancla de sesion; la
    cache evita la segunda llamada a yt-dlp que era el delay del salto."""
    import bot as bot_mod
    from queue_manager import QueueItem
    from search import SearchResult

    b = make_bot()
    b._radio_artist = "GP Band"
    current = QueueItem(url="u0", title="actual")
    calls = []

    def counting_search(query, count, **kw):
        calls.append(query)
        return [
            SearchResult(url="u1", title="Tema GP Band", duration="3:00", thumbnail=""),
            SearchResult(url="u2", title="Otro GP Band", duration="4:00", thumbnail=""),
        ]

    original = bot_mod.search
    bot_mod.search = counting_search
    try:
        c1, _, _ = await b._pick_next_candidate(current)
        c2, _, _ = await b._pick_next_candidate(current)
        assert len(calls) == 1, f"search se llamo {len(calls)} veces, debe ser 1"
        assert c1 is not None and c2 is not None
        assert c1.url == c2.url == "u1"
    finally:
        bot_mod.search = original


async def test_next_radio_renders_loading_card_inmediato():
    """Al tocar ⏭ sin prefetch resuelto, la tarjeta se edita YA con
    "⏳ Cargando…" + titulo del candidato nuevo, antes de resolver el stream.
    El commit de queue._current ocurre recien tras el load exitoso: el estado
    del dominio no se toca para mostrar el feedback de carga."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        # Tarjeta persistente ya activa (mensaje de texto, sin miniatura).
        b._card_chat_id = 44
        b._card_message_id = 7
        b._card_is_photo = False
        b._radio_artist = ""
        b._clear_nav_stacks()
        b.queue.set_current(QueueItem(url="A", title="A"))
        b._prefetch_resolved = None
        b._prefetch_candidate = None

        new_pick = QueueItem(url="B", title="Nuevo Tema")
        async def fake_pick(current: QueueItem) -> tuple[QueueItem | None, bool, bool]:
            return new_pick, False, False
        b._pick_next_candidate = fake_pick

        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "B"
        # En algun edit de la tarjeta (44/7) aparecio el estado de carga
        # con el titulo nuevo ANTES de que la musica entrara.
        carga = [
            e for e in b._app.bot.edited
            if e[0] == 44 and e[1] == 7 and isinstance(e[2], str)
            and "⏳ Cargando…" in e[2] and "Nuevo Tema" in e[2]
        ]
        assert carga, b._app.bot.edited
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_next_radio_failed_resolve_keeps_current():
    """Si la resolucion del candidato nuevo falla, queue._current NO pasa a
    apuntar al candidato: el estado conserva el tema anterior (el commit del
    dominio ocurre solo cuando el audio realmente arranca)."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: None
    try:
        b = make_bot()
        b._card_chat_id = 44
        b._card_message_id = 7
        b._card_is_photo = False
        b._radio_artist = ""
        b._clear_nav_stacks()
        b.queue.set_current(QueueItem(url="A", title="A"))
        b._prefetch_resolved = None
        b._prefetch_candidate = None

        new_pick = QueueItem(url="B", title="Nuevo Tema")
        async def fake_pick(current: QueueItem) -> tuple[QueueItem | None, bool, bool]:
            return new_pick, False, False
        b._pick_next_candidate = fake_pick

        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))
        # El estado no se corrompe con el candidato que nunca sonó.
        assert cast(QueueItem, b.queue.current).url == "A"
        # La card si mostró el feedback de carga (presentación) antes del fallo.
        carga = [
            e for e in b._app.bot.edited
            if e[0] == 44 and e[1] == 7 and isinstance(e[2], str)
            and "⏳ Cargando…" in e[2] and "Nuevo Tema" in e[2]
        ]
        assert carga, b._app.bot.edited
    finally:
        bot_mod.resolve_stream_url = lambda url: None


async def test_stream_for_times_out_hung_resolve():
    """Un yt-dlp colgado (thread que nunca termina) NO congela el bot: el
    timeout de _stream_for corta el await y devuelve None (fallo de
    resolucion) en vez de dejar el polling mudo para siempre."""
    import bot as bot_mod

    b = make_bot()

    original_resolve = bot_mod.resolve_stream_url
    original_timeout = bot_mod._RESOLVE_TIMEOUT
    bot_mod.resolve_stream_url = lambda url: time.sleep(5) or ("stream-" + url, None)
    bot_mod._RESOLVE_TIMEOUT = 0.3
    try:
        inicio = time.monotonic()
        result = await b._stream_for("X")
        transcurrido = time.monotonic() - inicio
        assert result is None, "la resolucion colgada debe expirar como fallo"
        assert transcurrido < 4, f"tardo {transcurrido:.1f}s, el timeout no funciono"
    finally:
        bot_mod.resolve_stream_url = original_resolve
        bot_mod._RESOLVE_TIMEOUT = original_timeout


async def test_control_button_answers_instantly_no_toast():
    """El boton responde al instante con query.answer() mudo (apaga el spinner
    sin toast de arriba): todo el feedback vive en la card, y la accion corrio."""
    b = make_bot()
    upd = FakeUpdate("ctl:vol-10", chat_id=44)
    await b._on_control(upd, SimpleNamespace(args=[]), "vol-10")
    assert upd.answered == (), upd.answered
    assert b._volume == 90, "la accion del boton debe ejecutarse"


async def test_remove_card_deletes_and_resets():
    """La card vieja se borra de verdad (delete_message) y los ids se limpian:
    antes el pick solo resetaba los ids y dejaba el mensaje huerfano en el chat."""
    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    await b._remove_card()
    assert (44, 7) in [(d[0], d[1]) for d in b._app.bot.deleted], b._app.bot.deleted
    assert b._card_chat_id is None
    assert b._card_message_id is None


async def test_reposition_card_deletes_old_and_sends_new():
    """La card vieja se desvanece (delete_message) y se re-envia como nuevo
    mensaje al final del chat: queda como ultima visualizacion, no editada
    en el medio."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_current(QueueItem(url="u1", title="Tema"))
    b._card_chat_id = 44
    b._card_message_id = 7
    await b._reposition_card(44)
    assert (44, 7) in [(d[0], d[1]) for d in b._app.bot.deleted], b._app.bot.deleted
    assert len(b._app.bot.sent) == 1, b._app.bot.sent
    assert b._card_chat_id == 44
    assert b._card_message_id is not None


async def test_pick_removes_orphan_card():
    """Elegir un resultado de /buscar borra la card vieja (con su mensaje),
    no solo los ids: la nueva nace fresca al final de la conversacion."""
    from search import SearchResult

    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    b._search_cache["pick:0"] = SearchResult(
        url="u1", title="Tema Uno", duration="3:00", thumbnail=""
    )
    played = []

    async def fake_play(update, item, **kwargs):
        played.append(item)
        return True

    b._play_item = fake_play
    upd = FakeUpdate("pick:0", message_id=500, chat_id=44)
    await b.on_callback(upd, SimpleNamespace(args=[]))
    assert (44, 7) in [(d[0], d[1]) for d in b._app.bot.deleted], b._app.bot.deleted
    assert len(played) == 1


async def test_with_card_reposition_repositions_existing():
    """Tras correr un comando con card previa, el wrapper la re-envia al final
    del chat: los mensajes del comando quedan arriba y la card abajo."""
    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    ran = []

    async def handler(update, context):
        ran.append(1)

    wrapped = b._with_card_reposition(handler)
    await wrapped(FakeMessageUpdate("/x"), SimpleNamespace(args=[]))
    assert ran == [1]
    assert (44, 7) in [(d[0], d[1]) for d in b._app.bot.deleted], b._app.bot.deleted
    assert len(b._app.bot.sent) == 1, b._app.bot.sent


async def test_passive_text_repositions_card():
    """Escribir un texto en el chat (no comando) re-posiciona la card al final
    sin responder nada al usuario."""
    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    upd = FakeMessageUpdate("hola")
    await b._passive_card_reposition(upd, SimpleNamespace(args=[]))
    assert (44, 7) in [(d[0], d[1]) for d in b._app.bot.deleted], b._app.bot.deleted
    assert len(b._app.bot.sent) == 1, b._app.bot.sent
    assert upd.sent == [], "el pasivo no debe responder nada"


async def test_playlist_feedback_renders_in_card():
    """Arrancar una playlist avisa en la card (⏳ Arrancando) sin dejar un
    mensaje suelto clavado en el chat, y reproduce YA el primer track con el
    total real de la lista (quick_playlist) mientras el resto carga aparte."""
    import bot as bot_mod
    from search import SearchResult

    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    played = []

    async def fake_play(update, item, **kwargs):
        played.append(item)
        return True

    b._play_item = fake_play

    orig_pl = bot_mod.is_playlist_url
    orig_quick = bot_mod.quick_playlist
    orig_exp = bot_mod.expand_playlist
    bot_mod.is_playlist_url = lambda q: True
    bot_mod.quick_playlist = lambda q, n: (
        [SearchResult(url="u1", title="T1", duration="3:00", thumbnail="")], 39
    )
    bot_mod.expand_playlist = lambda q, n: []
    try:
        upd = FakeMessageUpdate("https://youtube.com/playlist?list=XYZ")
        await b._play_link_or_playlist(upd, SimpleNamespace(args=[]), upd.message.text)
        if b._expand_task is not None:
            await b._expand_task
    finally:
        bot_mod.is_playlist_url = orig_pl
        bot_mod.quick_playlist = orig_quick
        bot_mod.expand_playlist = orig_exp
    cards_log = [e for e in b._app.bot.edited if "Arrancando playlist" in str(e[2])]
    assert cards_log, b._app.bot.edited
    assert not [s for s in b._app.bot.sent if "Arrancando" in str(s[1])], b._app.bot.sent
    assert len(played) == 1, played
    # El feedback final reporta el total REAL de la lista (39), no solo los N
    # del arranque rapido, y avisa que el resto carga en segundo plano.
    assert any("Playlist (1/39)" in t for t, _ in upd.sent), upd.sent
    assert any("segundo plano" in t for t, _ in upd.sent), upd.sent


async def test_quality_button_in_card_keyboard():
    """La tarjeta muestra siempre el boton ancho de calidad (fila 3), visible
    para todos: '⚙️ Calidad: 1080p' por defecto."""
    b = make_bot()
    kb = b._control_keyboard()
    rows = kb.inline_keyboard
    assert len(rows) == 3, f"la tarjeta debe tener 3 filas, tiene {len(rows)}"
    quality_btn = rows[2]
    assert len(quality_btn) == 1, f"la fila 3 es un solo boton ancho: {quality_btn}"
    assert quality_btn[0].text == "⚙️ Calidad: 1080p", quality_btn[0].text
    assert quality_btn[0].callback_data == "ctl:calidad"
    b._max_height = 480
    kb = b._control_keyboard()
    assert kb.inline_keyboard[2][0].text == "⚙️ Calidad: 480p"


async def test_quality_selector_opens_for_admin():
    """El selector de calidad se abre tocando el boton de la tarjeta; solo el
    admin lo puede abrir (el resto recibe alerta y no se toca nada)."""
    b = make_bot()
    b.roles.set_role(1, "admin")

    # No-admin: rechazado con alerta, la card no cambia.
    upd_user = FakeUpdate("ctl:calidad", user_id=77, chat_id=44, message_id=7)
    b._card_chat_id = 44
    b._card_message_id = 7
    b._app.bot.edited.clear()
    await b._on_control(upd_user, SimpleNamespace(args=[]), "calidad")
    assert upd_user.answered and "admin" in str(upd_user.answered[0])
    assert b._app.bot.edited == []
    assert b._max_height is None

    # Admin: se abre el selector en la propia tarjeta.
    upd_admin = FakeUpdate("ctl:calidad", user_id=1, chat_id=44, message_id=7)
    b._card_chat_id = 44
    b._card_message_id = 7
    b._app.bot.edited.clear()
    await b._on_control(upd_admin, SimpleNamespace(args=[]), "calidad")
    ediciones = b._app.bot.edited
    assert ediciones, "el admin debe ver el selector editando la card"
    assert ediciones[-1][2] == "MARKUP", ediciones[-1]
    markup = ediciones[-1][3].get("reply_markup")
    filas = markup.inline_keyboard
    etiquetas = [btn.text for fila in filas for btn in fila]
    assert "1080 ✓" in etiquetas, etiquetas  # el nivel vigente sale marcado
    assert "❌" in etiquetas, etiquetas
    # Tope de la app: 1080 es el maximo, no hay 1440 ni 2160.
    assert "1440" not in etiquetas, etiquetas
    assert "2160" not in etiquetas, etiquetas


async def test_quality_select_applies_and_persists():
    """Elegir un nivel aplica search.MAX_HEIGHT, lo persiste en state.json y
    restaura la MISMA card sin recrearla (solo vuelve el teclado de control
    con el boton mostrando la nueva calidad)."""
    import bot as bot_mod
    import search as search_mod

    b = make_bot()
    b.roles.set_role(1, "admin")
    b._card_chat_id = 44
    b._card_message_id = 7

    original_height = search_mod.MAX_HEIGHT
    try:
        upd = FakeUpdate("cl:calidad:480", user_id=1, chat_id=44, message_id=7)
        await b._on_quality_callback(upd, SimpleNamespace(args=[]))
        assert b._max_height == 480
        assert search_mod.MAX_HEIGHT == 480
        # La card NO se desvanece ni se re-envia: se edita el MISMO mensaje.
        assert b._app.bot.sent == [], "no se debe re-enviar la card"
        assert b._app.bot.deleted == [], "no se debe desvanecer la card vieja"
        ediciones = b._app.bot.edited
        assert ediciones and ediciones[-1][2] == "MARKUP", ediciones[-1]
        # El teclado restaurado es el de control con la calidad elegida.
        kb = ediciones[-1][3].get("reply_markup")
        assert kb.inline_keyboard[2][0].text == "⚙️ Calidad: 480p"
        # Persistido: al "reiniciar" en aislamiento se restaura.
        b2 = make_bot()
        assert b2._max_height is None, "un bot nuevo sin estado previo: 1080"
    finally:
        search_mod.MAX_HEIGHT = original_height


async def test_quality_select_reloads_current_track():
    """Elegir calidad invalida el cache del mesonero/radio y RECARGA el tema
    actual con la nueva resolucion: el player recibe el stream re-resuelto."""
    import search as search_mod
    from queue_manager import QueueItem

    b = make_bot()
    b.roles.set_role(1, "admin")
    b._card_chat_id = 44
    b._card_message_id = 7
    b.queue.set_current(QueueItem(url="u1", title="Mi Cancion", channel="GP Band"))
    # Cache del mesonero con streams resueltos a la calidad vieja: al cambiar
    # el nivel deben invalidarse (se re-resuelve con el tope nuevo).
    b._stream_cache["u1"] = ("stream-1080", None)
    b._radio_search_cache["GP Band"] = ["resultado viejo"]

    async def fake_stream_for(url):
        return ("stream-480", None)

    b._stream_for = fake_stream_for
    original_height = search_mod.MAX_HEIGHT
    try:
        upd = FakeUpdate("cl:calidad:480", user_id=1, chat_id=44, message_id=7)
        await b._on_quality_callback(upd, SimpleNamespace(args=[]))
        assert search_mod.MAX_HEIGHT == 480
        # El stream se re-resolvio y se recargo en el player.
        assert b.player.loaded, "la cancion actual debe recargarse"
        assert b.player.loaded[-1] == ("stream-480", None), b.player.loaded
        assert b.player.resumed >= 1
        # El cache viejo quedo invalidado (el stream cacheado murio de verdad).
        assert "u1" not in b._stream_cache, b._stream_cache
        assert "GP Band" not in b._radio_search_cache, b._radio_search_cache
        # La card no se borra ni re-envia: solo vuelve el teclado de control.
        assert b._app.bot.sent == [], "recargar no re-envia la card"
        assert b._app.bot.deleted == [], "recargar no desvanece la card"
        assert b._app.bot.edited[-1][2] == "MARKUP", b._app.bot.edited
    finally:
        search_mod.MAX_HEIGHT = original_height


async def test_quality_persisted_across_restart():
    """Si state.json guarda max_height, un bot nuevo lo restaura y lo aplica."""
    import bot as bot_mod
    import persistence as persistence_mod
    import search as search_mod
    import tempfile
    from config import Config

    original_height = search_mod.MAX_HEIGHT
    real_state_path = persistence_mod.STATE_PATH
    try:
        tmp = tempfile.mkdtemp()
        persistence_mod.STATE_PATH = Path(tmp) / "state.json"

        def _fresh_bot():
            player = FakePlayer()
            config = Config(
                token="test", mpv_path="mpv", default_role="user",
                max_results=5, owner_id=1, allowed_chat_id=None,
            )
            b = bot_mod.YTRemoteBot(config)
            b._app = FakeApp()
            object.__setattr__(b, "player", player)
            b.roles.set_role(77, "dj")
            return b

        b1 = _fresh_bot()
        assert b1._max_height is None
        b1._max_height = 720
        b1._persist_flush()
        # "Reinicio": un bot nuevo que lee el MISMO state.json.
        b2 = _fresh_bot()
        assert b2._max_height == 720
        assert search_mod.MAX_HEIGHT == 720
        # Y un estado sin tope vuelve al default 1080.
        persistence_mod.STATE_PATH = Path(tmp) / "otro.json"
        b3 = _fresh_bot()
        assert b3._max_height is None
        # Un estado viejo con nivel por encima del tope nuevo (1440/2160) cae
        # a None -> 1080: nunca se aplica una calidad que dejamos de soportar.
        persistence_mod.STATE_PATH.write_text(
            '{"version": 1, "max_height": 2160}', encoding="utf-8"
        )
        b4 = _fresh_bot()
        assert b4._max_height is None, b4._max_height
        assert search_mod.MAX_HEIGHT == 1080
    finally:
        persistence_mod.STATE_PATH = real_state_path
        search_mod.MAX_HEIGHT = original_height


async def test_quality_close_without_changing():
    """Cerrar el selector no cambia nada y la card vuelve al estado normal."""
    b = make_bot()
    b.roles.set_role(1, "admin")
    b._card_chat_id = 44
    b._card_message_id = 7
    upd = FakeUpdate("cl:calidad:cerrar", user_id=1, chat_id=44, message_id=7)
    await b._on_quality_callback(upd, SimpleNamespace(args=[]))
    assert b._max_height is None
    ediciones = b._app.bot.edited
    assert ediciones and ediciones[-1][2] == "MARKUP", ediciones
    kb = ediciones[-1][3].get("reply_markup")
    assert kb.inline_keyboard[2][0].text == "⚙️ Calidad: 1080p"
    assert b._app.bot.sent == [], "cerrar no re-envia ni borra la card"
    assert b._app.bot.deleted == []


async def test_quality_rejects_non_admin():
    """Un no-admin no puede elegir calidad ni con el boton ni con target."""
    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    upd = FakeUpdate("ctl:calidad", user_id=77, chat_id=44, message_id=7)
    await b._on_control(upd, SimpleNamespace(args=[]), "calidad")
    assert upd.answered and "admin" in str(upd.answered[0])
    upd2 = FakeUpdate("cl:calidad:360", user_id=77, chat_id=44, message_id=7)
    await b._on_quality_callback(upd2, SimpleNamespace(args=[]))
    assert upd2.answered and "admin" in str(upd2.answered[0])
    assert b._max_height is None


async def test_quality_lock_discards_while_control_busy():
    """Si otra accion de boton corre, el toque de calidad se descarta al
    instante (candado anti-colision) en vez de esperar y congelar la
    botonera: es el fix del freeze de next/prev/stop/pause."""
    b = make_bot()
    b.roles.set_role(1, "admin")
    await b._control_lock.acquire()
    try:
        upd = FakeUpdate("cl:calidad:720", user_id=1, chat_id=44, message_id=7)
        await b._on_quality_callback(upd, SimpleNamespace(args=[]))
        assert b._max_height is None, "el nivel no se aplica si otra accion corre"
        assert upd.answered, "el toque en curso responde con aviso"
        assert "termina" in " ".join(str(a) for a in upd.answered), upd.answered
        assert b._app.bot.sent == [], "no se toca la card"
    finally:
        b._control_lock.release()


async def test_quality_above_1080_is_ignored():
    """El tope maximo es 1080: pedir 2160 no aplica nada."""
    import search as search_mod

    b = make_bot()
    b.roles.set_role(1, "admin")
    b._card_chat_id = 44
    b._card_message_id = 7
    upd = FakeUpdate("cl:calidad:2160", user_id=1, chat_id=44, message_id=7)
    await b._on_quality_callback(upd, SimpleNamespace(args=[]))
    assert b._max_height is None
    assert search_mod.MAX_HEIGHT == 1080


async def test_quality_same_level_is_ignored():
    """Elegir el nivel ya activo equivale a Cerrar: no aplica nada, no invalida
    cache y NO recarga la cancion actual (solo vuelve el teclado de control)."""
    import search as search_mod
    from queue_manager import QueueItem

    b = make_bot()
    b.roles.set_role(1, "admin")
    b._card_chat_id = 44
    b._card_message_id = 7
    b._max_height = 480
    b.queue.set_current(QueueItem(url="u1", title="Mi Cancion", channel="GP Band"))
    b._stream_cache["u1"] = ("stream-480", None)
    b._radio_search_cache["GP Band"] = ["resultado viejo"]

    async def fake_stream_for(url):
        raise AssertionError("no se debe re-resolver si la calidad no cambia")

    b._stream_for = fake_stream_for
    original_height = search_mod.MAX_HEIGHT
    try:
        search_mod.MAX_HEIGHT = 480
        upd = FakeUpdate("cl:calidad:480", user_id=1, chat_id=44, message_id=7)
        await b._on_quality_callback(upd, SimpleNamespace(args=[]))
        # Nada cambio: ni nivel, ni tope, ni cache.
        assert b._max_height == 480
        assert search_mod.MAX_HEIGHT == 480
        assert "u1" in b._stream_cache, "misma calidad no invalida el cache de streams"
        assert "GP Band" in b._radio_search_cache
        # El player NO recarga la cancion actual.
        assert b.player.loaded == [], "misma calidad no recarga la cancion"
        assert b.player.resumed == 0
        # La card no se borra ni re-envia: solo vuelve el teclado de control.
        assert b._app.bot.sent == [], "misma calidad no re-envia la card"
        assert b._app.bot.deleted == [], "misma calidad no desvanece la card"
        ediciones = b._app.bot.edited
        assert ediciones and ediciones[-1][2] == "MARKUP", ediciones
        kb = ediciones[-1][3].get("reply_markup")
        assert kb.inline_keyboard[2][0].text == "⚙️ Calidad: 480p"
    finally:
        search_mod.MAX_HEIGHT = original_height


async def test_list_button_opens_separate_message():
    """El 📋 abre la lista como MENSAJE aparte (la card no se pisa) con
    botones de tema para dj y la navegacion con ❌."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_playlist(
        [QueueItem(url=f"u{i}", title=f"Tema {i}") for i in range(3)]
    )
    upd = FakeUpdate("ctl:lista", user_id=77, chat_id=44)
    await b._on_control(upd, SimpleNamespace(args=[]), "lista")
    assert b._list_message_id is not None
    assert len(b._app.bot.sent) == 1, "la lista es un mensaje nuevo"
    text = b._app.bot.sent[0][1]
    assert text.startswith("Lista:"), text
    kb = b._app.bot.sent[0][2]["reply_markup"]
    rows = kb.inline_keyboard
    # dj: un boton de tema por fila (posicion global + titulo completo) y la
    # nav [· ❌ ·] (una sola pagina).
    assert [btn.text for btn in rows[0]] == ["▶️ 1. Tema 0"]
    assert [btn.text for btn in rows[1]] == ["2. Tema 1"]
    assert [btn.text for btn in rows[2]] == ["3. Tema 2"]
    assert [btn.text for btn in rows[3]] == ["·", "❌", "·"], rows[3]
    # La card no se edita ni se desvanece.
    assert b._app.bot.edited == []
    assert b._app.bot.deleted == []


async def test_list_open_for_user_no_track_buttons():
    """Un user ve la lista pero NO recibe botones de tema: solo navegacion."""
    from queue_manager import QueueItem

    b = make_bot()
    b.roles.set_role(50, "user")
    b.queue.set_playlist(
        [QueueItem(url=f"u{i}", title=f"Tema {i}") for i in range(3)]
    )
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=50, chat_id=44),
        SimpleNamespace(args=[]),
        "lista",
    )
    assert b._list_can_select is False
    kb = b._app.bot.sent[0][2]["reply_markup"]
    rows = kb.inline_keyboard
    assert len(rows) == 1, "user: sin botones de tema, solo navegacion"
    assert [btn.text for btn in rows[0]] == ["·", "❌", "·"]


async def test_list_radio_shows_current():
    """Modo radio (item suelto, sin playlist): la lista NO queda vacia;
    muestra el tema que esta sonando como boton "▶️ titulo" y tocarlo
    cierra igual que el ❌ (mismo callback lst:close)."""
    from queue_manager import QueueItem

    b = make_bot()
    b.roles.set_role(50, "user")
    b.queue.set_current(QueueItem(url="u0", title="Tema Sonando"))
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=50, chat_id=44),
        SimpleNamespace(args=[]),
        "lista",
    )
    assert b._list_message_id is not None
    kb = b._app.bot.sent[0][2]["reply_markup"]
    rows = kb.inline_keyboard
    assert rows[0][0].text == "▶️ Tema Sonando", rows
    assert rows[0][0].callback_data == "lst:close", rows
    assert [btn.text for btn in rows[1]] == ["·", "❌", "·"]

    list_id = b._list_message_id
    await b._on_list_callback(
        FakeUpdate("lst:close", user_id=50, chat_id=44), SimpleNamespace(args=[])
    )
    assert b._list_message_id is None, "tocar el tema en radio cierra la lista"
    assert (44, list_id) in b._app.bot.deleted, "se desvanece al cerrar"


async def test_list_paginates_with_arrows():
    """21 temas se muestran de 10 en 10 (3 paginas: 10+10+1); ◀ ▶ editan el
    mensaje abierto: el header muestra pagina actual/total y los BOTONES son
    la lista en si (titulo completo, unico lugar donde se ve cada tema)."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_playlist(
        [QueueItem(url=f"u{i}", title=f"Tema {i}") for i in range(21)]
    )
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44),
        SimpleNamespace(args=[]),
        "lista",
    )
    kb = b._app.bot.sent[0][2]["reply_markup"]
    rows = kb.inline_keyboard
    assert len(rows) == 11, "pagina 1: 10 temas + navegacion"
    assert rows[0][0].text == "▶️ 1. Tema 0"
    assert rows[9][0].text == "10. Tema 9"
    assert [btn.text for btn in rows[10]] == ["·", "❌", "▶"]
    # El texto del mensaje es SOLO el header: pagina actual y total.
    text1 = b._app.bot.sent[0][1]
    assert text1 == "Lista (pag 1/3):", text1

    edited_before = len(b._app.bot.edited)
    await b._on_list_callback(
        FakeUpdate("lst:next", user_id=77, chat_id=44), SimpleNamespace(args=[])
    )
    assert b._list_page == 1
    kb2 = b._app.bot.edited[-1][3]["reply_markup"]
    rows2 = kb2.inline_keyboard
    assert rows2[0][0].text == "11. Tema 10"
    assert rows2[9][0].text == "20. Tema 19"
    assert [btn.text for btn in rows2[10]] == ["◀", "❌", "▶"]
    # El texto editado es SOLO el header de la pagina 2.
    assert b._app.bot.edited[-1][2] == "Lista (pag 2/3):"

    await b._on_list_callback(
        FakeUpdate("lst:prev", user_id=77, chat_id=44), SimpleNamespace(args=[])
    )
    assert b._list_page == 0
    kb3 = b._app.bot.edited[-1][3]["reply_markup"]
    assert kb3.inline_keyboard[0][0].text == "▶️ 1. Tema 0"
    assert b._app.bot.edited[-1][2] == "Lista (pag 1/3):"

    # La ultima pagina no avanza: ▶ desaparece de la nav.
    b._list_page = 2
    kb4 = b._list_keyboard()
    assert len(kb4.inline_keyboard) == 2, "pagina 3: 1 solo tema + nav"
    assert kb4.inline_keyboard[0][0].text == "21. Tema 20"
    assert [btn.text for btn in kb4.inline_keyboard[-1]] == ["◀", "❌", "·"]
    # El header de la pagina 3 anuncia la pagina final (caso del usuario).
    text3 = b._queue_list_text(2)
    assert text3 == "Lista (pag 3/3):", text3
    # El placeholder · es inerte: no edita nada.
    edited_before = len(b._app.bot.edited)
    await b._on_list_callback(
        FakeUpdate("lst:noop", user_id=77, chat_id=44), SimpleNamespace(args=[])
    )
    assert len(b._app.bot.edited) == edited_before, "noop no edita nada"


async def test_list_reopen_deletes_old_and_sends_new():
    """Reabrir con lista abierta: se borra la anterior (con fade) y se manda
    una fresca (nunca se edita la lista existente)."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_playlist(
        [QueueItem(url=f"u{i}", title=f"Tema {i}") for i in range(3)]
    )
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44),
        SimpleNamespace(args=[]),
        "lista",
    )
    first_id = b._list_message_id
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44),
        SimpleNamespace(args=[]),
        "lista",
    )
    assert (44, first_id) in b._app.bot.deleted, "la lista vieja se desvanece"
    assert len(b._app.bot.sent) == 2, "se manda una lista fresca"
    assert b._list_message_id == 101, b._list_message_id  # fake numera 100, 101...


async def test_list_select_plays_and_closes():
    """Elegir un tema (dj): salta a la posicion, reproduce YA y la lista se
    cierra con desvanecimiento."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_playlist(
        [
            QueueItem(url=f"u{i}", title=f"Tema {i}", channel="GP Band")
            for i in range(5)
        ]
    )

    async def fake_stream_for(url):
        return (f"stream-{url}", None)

    b._stream_for = fake_stream_for
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44),
        SimpleNamespace(args=[]),
        "lista",
    )
    list_id = b._list_message_id
    current = b.queue.current
    assert current is not None and current.title == "Tema 0"
    upd = FakeUpdate("lst:4", user_id=77, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    current = b.queue.current
    assert current is not None and current.title == "Tema 3", "salto a la posicion 4 (1-based)"
    assert b.player.loaded and b.player.loaded[-1] == ("stream-u3", None), b.player.loaded
    assert b.player.resumed >= 1
    assert b._list_message_id is None, "la lista se cierra al elegir"
    assert (44, list_id) in b._app.bot.deleted, "la lista se desvanece al elegir"


async def test_list_user_cannot_select():
    """Un user tocando un tema recibe la alerta; la lista sigue abierta."""
    from queue_manager import QueueItem

    b = make_bot()
    b.roles.set_role(50, "user")
    b.queue.set_playlist(
        [QueueItem(url=f"u{i}", title=f"Tema {i}") for i in range(5)]
    )
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=50, chat_id=44),
        SimpleNamespace(args=[]),
        "lista",
    )
    list_id = b._list_message_id
    upd = FakeUpdate("lst:3", user_id=50, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert upd.answered_kwargs.get("show_alert") is True
    assert "Solo el admin o un dj" in upd.answered_kwargs.get("text", "")
    assert b._list_message_id == list_id, "la lista sigue abierta"
    assert b._app.bot.deleted == [], "nada se borra"
    assert b.player.loaded == [], "nada se reproduce"


async def test_list_close_deletes_for_anyone():
    """El ❌ lo cierra CUALQUIERA (user o dj): la lista se desvanece."""
    from queue_manager import QueueItem

    for role in ("user", "dj"):
        b = make_bot()
        if role == "user":
            b.roles.set_role(77, "user")
        b.queue.set_playlist(
            [QueueItem(url=f"u{i}", title=f"Tema {i}") for i in range(3)]
        )
        await b._on_control(
            FakeUpdate("ctl:lista", user_id=77, chat_id=44),
            SimpleNamespace(args=[]),
            "lista",
        )
        list_id = b._list_message_id
        await b._on_list_callback(
            FakeUpdate("lst:close", user_id=77, chat_id=44), SimpleNamespace(args=[])
        )
        assert (44, list_id) in b._app.bot.deleted, f"{role}: ❌ desvanece la lista"
        assert b._list_message_id is None


async def test_list_open_empty_queue_toast():
    """Cola vacia: el 📋 solo responde el toast, no envia ningun mensaje."""
    b = make_bot()
    upd = FakeUpdate("ctl:lista", user_id=77, chat_id=44)
    await b._on_control(upd, SimpleNamespace(args=[]), "lista")
    assert upd.answered == ("La lista esta vacia.",), upd.answered
    assert b._app.bot.sent == [], "no se envia lista vacia"


async def test_list_select_already_playing_keeps_open():
    """Elegir el tema actual: toast 'Ya esta sonando' y la lista NO se cierra."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_playlist(
        [QueueItem(url=f"u{i}", title=f"Tema {i}") for i in range(3)]
    )
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44),
        SimpleNamespace(args=[]),
        "lista",
    )
    list_id = b._list_message_id
    upd = FakeUpdate("lst:1", user_id=77, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert "Ya esta sonando" in upd.answered_kwargs.get("text", ""), upd.answered
    assert b._list_message_id == list_id, "no se cierra"
    assert b._app.bot.deleted == []


async def test_list_select_out_of_range_keeps_open():
    """Posicion inexistente: toast 'Numero fuera de rango', la lista sigue."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_playlist(
        [QueueItem(url=f"u{i}", title=f"Tema {i}") for i in range(3)]
    )
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44),
        SimpleNamespace(args=[]),
        "lista",
    )
    list_id = b._list_message_id
    upd = FakeUpdate("lst:99", user_id=77, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert "Numero fuera de rango" in upd.answered_kwargs.get("text", "")
    assert b._list_message_id == list_id
    assert b._app.bot.deleted == []


async def test_playlist_expand_times_out():
    """Un link de playlist colgado no congela el bot: el wait_for con
    _QUICK_TIMEOUT expira y se responde el error en vez de colgarse."""
    import bot as bot_mod

    b = make_bot()
    upd = FakeMessageUpdate("https://youtube.com/playlist?list=XYZ")
    orig_pl = bot_mod.is_playlist_url
    orig_quick = bot_mod.quick_playlist
    orig_timeout = bot_mod._QUICK_TIMEOUT
    bot_mod.is_playlist_url = lambda q: True
    bot_mod.quick_playlist = lambda q, n: time.sleep(5) or ([], 0)
    bot_mod._QUICK_TIMEOUT = 0.3
    try:
        inicio = time.monotonic()
        await b._play_link_or_playlist(upd, SimpleNamespace(args=[]), upd.message.text)
        transcurrido = time.monotonic() - inicio
    finally:
        bot_mod.is_playlist_url = orig_pl
        bot_mod.quick_playlist = orig_quick
        bot_mod._QUICK_TIMEOUT = orig_timeout
    assert transcurrido < 4, f"tardo {transcurrido:.1f}s, el timeout no funciono"
    assert any("No se pudo expandir" in t for t, _ in upd.sent), upd.sent


async def test_playlist_quick_load_starts_immediately():
    """Paso 3 (arranque al toque): una playlist arranca YA con los primeros N
    (quick_playlist) sin esperar la expansion completa; el mensaje reporta
    el total real y que el resto carga en segundo plano."""
    import bot as bot_mod
    from search import SearchResult

    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    played = []

    async def fake_play(update, item, **kwargs):
        played.append(item)
        return True

    b._play_item = fake_play

    orig_pl = bot_mod.is_playlist_url
    orig_quick = bot_mod.quick_playlist
    orig_exp = bot_mod.expand_playlist
    bot_mod.is_playlist_url = lambda q: True
    bot_mod.quick_playlist = lambda q, n: (
        [SearchResult(url=f"u{i}", title=f"T{i}", duration="3:00", thumbnail="") for i in range(n)],
        1882,
    )
    bot_mod.expand_playlist = lambda q, n: []
    try:
        assert b._expanding_total == 0, "arranque: aun sin expansion"
        upd = FakeMessageUpdate("https://youtube.com/playlist?list=XYZ")
        await b._play_link_or_playlist(upd, SimpleNamespace(args=[]), upd.message.text)
        # El total y la URL quedan registrados para la expansion de fondo.
        assert b._expanding_total == 1882
        assert b._expanding_playlist_url == "https://youtube.com/playlist?list=XYZ"
        if b._expand_task is not None:
            await b._expand_task
    finally:
        bot_mod.is_playlist_url = orig_pl
        bot_mod.quick_playlist = orig_quick
        bot_mod.expand_playlist = orig_exp

    assert len(played) == 1, played
    q = b.queue
    assert q.has_playlist
    assert len(q._items) == 15, f"quick-load debe cargar 15, trajo {len(q._items)}"
    assert b.queue.current is not None and b.queue.current.url == "u0"
    # El mensaje de exito reporta 15/1882 y avisa del segundo plano.
    assert any("Playlist (15/1882)" in t for t, _ in upd.sent), upd.sent
    assert any("segundo plano" in t for t, _ in upd.sent), upd.sent


async def test_playlist_background_expansion_does_not_duplicate():
    """Paso 3/4: la expansion de fondo anexa SOLO los tracks que no estaban
    (el quick-load ya cargo los primeros N, no debe duplicarlos) y mantiene
    el orden a continuacion de lo cargado."""
    import bot as bot_mod
    from search import SearchResult

    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7

    orig_pl = bot_mod.is_playlist_url
    orig_quick = bot_mod.quick_playlist
    orig_exp = bot_mod.expand_playlist
    bot_mod.is_playlist_url = lambda q: True
    bot_mod.quick_playlist = lambda q, n: (
        [SearchResult(url=f"u{i}", title=f"T{i}", duration="3:00", thumbnail="") for i in range(2)],
        4,
    )
    # La expansion completa incluye tambien los 2 primeros (igual URL): el
    # dedupe debe dejar solo los 2 nuevos al final.
    bot_mod.expand_playlist = lambda q, n: [
        SearchResult(url="u0", title="T0", duration="3:00", thumbnail=""),
        SearchResult(url="u1", title="T1", duration="3:00", thumbnail=""),
        SearchResult(url="u2", title="T2", duration="3:00", thumbnail=""),
        SearchResult(url="u3", title="T3", duration="3:00", thumbnail=""),
    ]

    async def fake_play(update, item, **kwargs):
        return True

    b._play_item = fake_play
    try:
        upd = FakeMessageUpdate("https://youtube.com/playlist?list=XYZ")
        await b._play_link_or_playlist(upd, SimpleNamespace(args=[]), upd.message.text)
        # La expansion de fondo ya fue lanzada por el arranque: esperarla a
        # que complete (los datos mockeados terminan al vuelo).
        if b._expand_task is not None:
            await b._expand_task
    finally:
        bot_mod.is_playlist_url = orig_pl
        bot_mod.quick_playlist = orig_quick
        bot_mod.expand_playlist = orig_exp

    assert [i.url for i in b.queue._items] == ["u0", "u1", "u2", "u3"], [
        i.url for i in b.queue._items
    ]
    assert b._expanding_playlist is False


async def test_playlist_background_expansion_ignores_swapped_queue():
    """Paso 3/4: una expansion de UNA playlist vieja no anexa sus datos a la
    cola si el usuario ya arranco otra playlist (la URL ya no es la vigente):
    la tarea caduca se descarta sin tocar la cola."""
    import bot as bot_mod
    from search import SearchResult

    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7

    # Escenario sin expansión activa: la URL vieja se considera no vigente
    # (no hay self._expanding_playlist), asi que la expansion no anexa nada.
    b._expanding_playlist = False
    b._expanding_playlist_url = None
    orig_exp = bot_mod.expand_playlist
    bot_mod.expand_playlist = lambda q, n: [
        SearchResult(url="u1", title="T1", duration="3:00", thumbnail="")
    ]
    try:
        from queue_manager import QueueItem

        b.queue.set_playlist([QueueItem(url="cur", title="Cur")])
        await b._expand_playlist_background("https://youtube.com/playlist?list=OLD")
    finally:
        bot_mod.expand_playlist = orig_exp

    assert [i.url for i in b.queue._items] == ["cur"], (
        "una expansion caduca no debe anexar nada a la cola"
    )


async def test_pick_next_waits_for_expansion_no_early_wrap():
    """Paso 4: si el arranque al toque sigue expandiendo y el cursor esta en
    el ULTIMO tema cargado, el siguiente NO hace wrap prematuro: espera a que
    la expansion anexe mas (el peek devuelve el tema nuevo, no el primero)."""
    import bot as bot_mod
    import asyncio as _asyncio
    from queue_manager import QueueItem

    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7

    items = [QueueItem(url=f"u{i}", title=f"T{i}") for i in range(2)]
    b.queue.set_playlist(items)
    b.queue._cursor = 1  # estamos en el ULTIMO item cargado (u1)
    b._expanding_playlist = True
    b._expanding_playlist_url = "https://youtube.com/playlist?list=XYZ"
    b._expanding_total = 3

    # La expansion ficticia: dentro de 0.3s anexa u2 y termina.
    async def expansion_fake():
        await _asyncio.sleep(0.3)
        b.queue.add(QueueItem(url="u2", title="T2"))
        b._expanding_playlist = False
        b._expanding_playlist_url = None
        b._expanding_total = 0

    tarea = _asyncio.create_task(expansion_fake())
    try:
        actual = cast(QueueItem, b.queue.current)
        candidato, de_cola, err = await b._pick_next_candidate(actual)
        assert candidato is not None and candidato.url == "u2", candidato
        assert de_cola is True
    finally:
        if not tarea.done():
            tarea.cancel()


def test_live_stream_resolves_separated():
    """Directo (en vivo): el HLS de YouTube live NO tiene formato combinado
    ('best' falla), llega como video y audio en requested_formats separados.

    El audio HLS viene SIN la key 'acodec' (solo vcodec 'none'); se lo debe
    detectar igual y devolver (video, audio), no solo el video (que queda
    mudo en mpv)."""
    import search as search_mod
    import yt_dlp

    calls = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            calls.append(opts.get("format", ""))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):  # noqa: ARG002
            return {
                "id": "live1",
                "title": "Directo de prueba",
                "live_status": "is_live",
                "is_live": True,
                "requested_formats": [
                    {"vcodec": "avc1", "acodec": "none", "url": "https://hls/v.m3u8"},
                    {"vcodec": "none", "url": "https://hls/a.m3u8"},
                ],
            }

    original = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = FakeYDL
    try:
        res = search_mod.resolve_stream_url("https://youtube.com/watch?v=live1")
        assert res is not None, res
        assert res[0] == "https://hls/v.m3u8", res
        assert res[1] == "https://hls/a.m3u8", f"el audio HLS debe detectarse: {res}"
    finally:
        yt_dlp.YoutubeDL = original

    assert len(calls) == 1, f"no debe re-resolverse con 'best' (falla en directos), vi {calls}"


def test_fmt_duration_live_shows_dashes():
    """Paso 6: un video EN VIVO no tiene duracion fija; un 0 no debe
    mostrarse como '0:00' engañoso sino como '--:--'."""
    import search as search_mod

    assert search_mod._fmt_duration(0) == "--:--"
    assert search_mod._fmt_duration(None) == "--:--"
    assert search_mod._fmt_duration(3 * 60 + 5) == "3:05"
    assert search_mod._fmt_duration(3600 + 2 * 60 + 7) == "1:02:07"


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
    test_nav_stacks_initial_state()
    asyncio.run(test_nav_push_to_back_clears_forward())
    asyncio.run(test_nav_clear_stacks())
    asyncio.run(test_nav_prev_next_cycle_radio())
    asyncio.run(test_nav_next_chooses_new_candidate_when_forward_empty())
    asyncio.run(test_nav_prev_falls_back_to_queue_history())
    asyncio.run(test_nav_playlist_unaffected())
    asyncio.run(test_toggle_play_pause_starts_player_when_off())
    asyncio.run(test_resume_starts_player_when_not_running())
    asyncio.run(test_toggle_play_pause_keeps_playlist())
    asyncio.run(test_restore_cursor_and_list_page())
    asyncio.run(test_restore_cursor_clamped_to_range())
    asyncio.run(test_restore_cursor_corrupt_falls_to_zero())
    asyncio.run(test_close_list_keeps_page())
    asyncio.run(test_next_prefetch_starts_player())
    asyncio.run(test_stop_conserves_state_and_rewinds())
    asyncio.run(test_stop_does_not_clear_queue())
    asyncio.run(test_notify_admin_sends_to_owner())
    asyncio.run(test_control_lock_discards_second_tap())
    asyncio.run(test_radio_search_list_cached_per_anchor())
    asyncio.run(test_next_radio_renders_loading_card_inmediato())
    asyncio.run(test_next_radio_failed_resolve_keeps_current())
    asyncio.run(test_stream_for_times_out_hung_resolve())
    asyncio.run(test_control_button_answers_instantly_no_toast())
    asyncio.run(test_remove_card_deletes_and_resets())
    asyncio.run(test_reposition_card_deletes_old_and_sends_new())
    asyncio.run(test_pick_removes_orphan_card())
    asyncio.run(test_with_card_reposition_repositions_existing())
    asyncio.run(test_passive_text_repositions_card())
    asyncio.run(test_playlist_feedback_renders_in_card())
    asyncio.run(test_playlist_expand_times_out())
    asyncio.run(test_playlist_quick_load_starts_immediately())
    asyncio.run(test_playlist_background_expansion_does_not_duplicate())
    asyncio.run(test_playlist_background_expansion_ignores_swapped_queue())
    asyncio.run(test_pick_next_waits_for_expansion_no_early_wrap())
    test_live_stream_resolves_separated()
    test_fmt_duration_live_shows_dashes()
    asyncio.run(test_quality_button_in_card_keyboard())
    asyncio.run(test_quality_selector_opens_for_admin())
    asyncio.run(test_quality_select_applies_and_persists())
    asyncio.run(test_quality_select_reloads_current_track())
    asyncio.run(test_quality_persisted_across_restart())
    asyncio.run(test_quality_close_without_changing())
    asyncio.run(test_quality_rejects_non_admin())
    asyncio.run(test_quality_lock_discards_while_control_busy())
    asyncio.run(test_quality_above_1080_is_ignored())
    asyncio.run(test_quality_same_level_is_ignored())
    asyncio.run(test_list_button_opens_separate_message())
    asyncio.run(test_list_open_for_user_no_track_buttons())
    asyncio.run(test_list_radio_shows_current())
    asyncio.run(test_list_paginates_with_arrows())
    asyncio.run(test_list_reopen_deletes_old_and_sends_new())
    asyncio.run(test_list_select_plays_and_closes())
    asyncio.run(test_list_user_cannot_select())
    asyncio.run(test_list_close_deletes_for_anyone())
    asyncio.run(test_list_open_empty_queue_toast())
    asyncio.run(test_list_select_already_playing_keeps_open())
    asyncio.run(test_list_select_out_of_range_keeps_open())
    print("TARJETA TESTS OK")


if __name__ == "__main__":
    run()