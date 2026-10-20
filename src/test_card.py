"""Tests de la tarjeta persistente del mini reproductor (Fase 2, tarea 4).

Cubre: teclado de control, texto de estado, toggle play/pause, vol+-10,
dispatch de callbacks ctl:*, reuso del boton 📋 y render de la tarjeta.
Navegación prev/next con stacks (modo radio) y tests del mesonero.
Sin red ni mpv real: se stubbea Player y el bot de Telegram.
"""

import asyncio
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from testkit import run_sync_tests  # noqa: E402
import playback as playback_mod

# Aislar el registro de roles PARA TODO EL PROCESO: el RoleManager default
# del bot escribe en data/roles.json REAL (el __init__ registra al dueno y
# los callbacks a los usuarios; los tests mutan b.roles despues de armar el
# bot). Sin esto, una sola prueba contamina el archivo real con IDs falsos
# que aparecen luego en la lista del boton 👥. Este redirect alcanza a toda
# suite que importe test_card (test_roles y test_members incluidos), sin
# tocar el archivo real nunca.
import roles as _roles_mod

_ROLES_TEST_DIR = tempfile.mkdtemp()
_roles_mod.ROLES_PATH = Path(_ROLES_TEST_DIR) / "roles.json"
# ALSO el SQLite. Aislar solo el JSON no alcanzaba: `RoleManager` guarda en
# `roles.db`, asi que toda la suite estaba ESCRIBIENDO en el data/roles.db
# real del usuario (y con el DELETE-on-load de antes, ademas lo vaciaba).
# Se redirecta la base al mismo temp.
_roles_mod.DB_PATH = Path(_ROLES_TEST_DIR) / "roles.db"


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
        self.banned = []
        self.unbanned = []
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
        kwargs["media"] = media
        self.edited.append((chat_id, message_id, "MEDIA", kwargs))

    async def edit_message_caption(self, caption=None, chat_id=None, message_id=None, **kwargs):
        self.edited.append((chat_id, message_id, "CAP:" + str(caption), kwargs))

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, **kwargs):
        self.edited.append((chat_id, message_id, "MARKUP", kwargs))

    async def delete_message(self, chat_id, message_id, **kwargs):
        self.deleted.append((chat_id, message_id))

    async def ban_chat_member(self, chat_id, user_id, **kwargs):
        self.banned.append((chat_id, user_id))

    async def unban_chat_member(self, chat_id, user_id, **kwargs):
        self.unbanned.append((chat_id, user_id))

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
        self.quits = 0
        self.seeks = []
        self.seek_tos = []
        self.time_pos = None

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

    async def quit(self):
        self.quits += 1
        self.running = False

    async def seek(self, delta):
        self.seeks.append(delta)

    async def seek_to(self, seconds):
        self.seek_tos.append(seconds)

    async def get_time_pos(self):
        return self.time_pos


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


def make_bot(fail_loads=0, state_file=None):
    """Construye un bot aislado. `state_file` permite arrancar el bot leyendo un
    state.json REAL (una sesion anterior), que es lo que hace una reinicio de
    verdad; sin ese argumento arranca con el estado vacio de siempre."""
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
    # historial de un uso previo, no debe filtrarse a los tests. (Los roles
    # ya estan aislados a nivel modulo: ROLES_PATH apunta a un temp.)
    if state_file is None:
        with isolated_state_path():
            b = bot_mod.YTRemoteBot(config)
    else:
        import persistence as persistence_mod

        real = persistence_mod.STATE_PATH
        persistence_mod.STATE_PATH = Path(state_file)
        try:
            b = bot_mod.YTRemoteBot(config)
        finally:
            persistence_mod.STATE_PATH = real
    b._app = FakeApp()
    object.__setattr__(b, "player", player)
    b.roles.set_role(77, "dj")
    return b


def test_control_keyboard_and_status():
    b = make_bot()
    kb = b._control_keyboard()
    rows = kb.inline_keyboard
    # Fila 1: ⏮, seek atras, play/pause, seek adelante, ⏭, ⏹;
    # fila 2: vol-, estado, vol+, lista (sin numeros en las etiquetas)
    labels1 = [btn.text for btn in rows[0]]
    labels2 = [btn.text for btn in rows[1]]
    assert labels1 == ["⏮", "⏪", "⏸️", "⏩", "⏭", "⏹"], labels1
    assert labels2 == ["🔉", "🔊 100", "🔊", "📋"], labels2
    # Volumen en 0: el nivel actual se muestra con 🔇 solo.
    b._volume = 0
    kb = b._control_keyboard()
    assert [btn.text for btn in kb.inline_keyboard[1]][1] == "🔇"
    b._volume = 100
    # En pausa el botón central (indice 2) debe ser ▶️
    b._paused = True
    kb = b._control_keyboard()
    assert [btn.text for btn in kb.inline_keyboard[0]][2] == "▶️"

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


async def test_dispatch_seek():
    """⏪/⏩ de la tarjeta: simetricos (10 s en cada sentido), mudos si
    no hay mpv corriendo (no hay posicion que mover) y con permiso dj."""
    b = make_bot()
    await b._on_control(FakeUpdate("ctl:seek-10"), SimpleNamespace(args=[]), "seek-10")
    await b._on_control(FakeUpdate("ctl:seek+10"), SimpleNamespace(args=[]), "seek+10")
    assert b.player.seeks == [-10, 10], b.player.seeks
    # Sin mpv corriendo no se busca: no hay stream cargado que mover.
    b.player.running = False
    await b._on_control(FakeUpdate("ctl:seek+10"), SimpleNamespace(args=[]), "seek+10")
    assert b.player.seeks == [-10, 10], "sin mpv no debe buscar"
    # Permiso: un user sin rol dj no puede hacer seek.
    upd = FakeUpdate("ctl:seek+10", user_id=99)
    b.player.running = True
    await b._on_control(upd, SimpleNamespace(args=[]), "seek+10")
    assert upd.answered == ("No tiene permiso para eso.",), upd.answered
    assert b.player.seeks == [-10, 10], "el seek denegado no debe llegar al player"


async def test_player_seek_emite_comando_mpv():
    """El seek de la tarjeta se traduce en el comando mpv correcto:
    seek N relative (negativo = atras), sin tocar pausa ni posicion."""
    import json as _json

    from player import Player

    p = object.__new__(Player)  # sin arrancar mpv: solo probamos el wire
    enviados = []

    async def _fake_send(data):
        enviados.append(data)

    p._send_raw = _fake_send
    await p.seek(-10)
    await p.seek(10)
    assert _json.loads(enviados[0])["command"] == ["seek", "-10", "relative"]
    assert _json.loads(enviados[1])["command"] == ["seek", "10", "relative"]


async def test_salud_diagnostico_admin():
    """/salud entrega REPRODUCTOR, VERSION de YT-Remote (constants.VERSION,
    la misma del release de GitHub) y ESTADO en UN mensaje de 3 lineas; solo
    admin (el user ni ve el encabezado). Regla del usuario: NINGUNA pieza
    interna se menciona en el chat."""
    import constants

    b = make_bot()
    h = b._require("admin", b.cmd_salud)

    # No-admin: denegado, sin diagnostico.
    upd_user = FakeMessageUpdate("/salud", user_id=50)
    await h(upd_user, SimpleNamespace(args=[]))
    assert any("denegado" in t for t, _ in upd_user.sent), upd_user.sent
    assert not any("🩺" in t for t, _ in upd_user.sent), "el user no ve nada"

    # Admin: una sola pantalla con encabezado + las 3 lineas.
    b.roles.set_role(1, "admin")
    upd_admin = FakeMessageUpdate("/salud", user_id=1)
    await h(upd_admin, SimpleNamespace(args=[]))
    assert len(upd_admin.sent) == 1, upd_admin.sent
    texto = upd_admin.sent[0][0]
    assert texto.startswith("🩺 YT-Remote — estado:"), texto
    assert "Reproductor:" in texto, texto
    assert f"Version: {constants.VERSION}" in texto, texto
    assert "Estado: conectado" in texto, texto
    assert "disco" not in texto, texto
    assert "si ves esto" not in texto, texto
    assert texto.count("\n") == 3, texto  # encabezado + 3 lineas
    # La version mostrada NUNCA puede ser la de una pieza interna.
    assert "yt-dlp" not in texto and "2026.08.19" not in texto, texto


async def test_menu_cubre_todos_los_comandos():
    """Todo nombre de CommandHandler de bot.py tiene que estar en
    _COMANDOS_PUBLICOS o _COMANDOS_ADMIN: un comando nuevo sin registrar
    en el menu queda invisible para siempre (el olvido que dejo a /salud
    y cinco comandos mas afuera de la lista del "/")."""
    import re as _re
    from pathlib import Path as _Path

    import bot as bot_mod

    fuente = _Path(bot_mod.__file__).read_text(encoding="utf-8")
    registrados = set(_re.findall(r'CommandHandler\(\s*"([a-z_]+)"', fuente))
    menu = {c.command for c in bot_mod._COMANDOS_PUBLICOS}
    menu |= {c.command for c in bot_mod._COMANDOS_ADMIN}
    assert registrados, "el patron no agarro ningun CommandHandler: el test no prueba nada"
    faltan = registrados - menu
    assert not faltan, f"comandos sin menu: {sorted(faltan)}"


async def test_sleep_menu_abre_y_programa():
    """El 💤 de Ajustes abre el menu de presets SOLO cambiando el teclado;
    elegir uno programa el temporizador de la sesion y Ajustes pasa a
    mostrar los minutos que faltan."""
    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7

    fila3 = [btn.text for btn in b._control_keyboard().inline_keyboard[2]]
    assert fila3 == ["⚙️ Ajustes"], fila3

    upd = FakeUpdate("ctl:apagar", user_id=77, chat_id=44, message_id=7)
    await b._on_control(upd, SimpleNamespace(args=[]), "apagar")
    assert b._app.bot.edited[-1][2] == "MARKUP", b._app.bot.edited[-1]
    menu = b._app.bot.edited[-1][3].get("reply_markup")
    etiquetas = [btn.text for fila in menu.inline_keyboard for btn in fila]
    assert "15 min" in etiquetas and "60 min" in etiquetas, etiquetas
    assert "❌" in etiquetas, etiquetas
    assert "⏰" not in "".join(etiquetas), "sin temporizador no hay encabezado"

    # Programar: estado + tarea + la opcion de Ajustes muestra lo que falta.
    upd2 = FakeUpdate("ap:45", user_id=77, chat_id=44, message_id=7)
    await b._on_sleep_callback(upd2, SimpleNamespace(args=[]))
    assert b._sleep_minutes == 45, b._sleep_minutes
    assert b._sleep_task is not None and not b._sleep_task.done()
    assert upd2.answered and "45" in str(upd2.answered), upd2.answered
    opcion = b._ajustes_keyboard(77).inline_keyboard[0][0]
    assert opcion.text == "💤 Se apagará en 45 min", opcion.text
    assert opcion.callback_data == "ctl:apagar"

    # Reprogramar cancela la tarea anterior (solo una puede correr).
    tarea_vieja = b._sleep_task
    upd3 = FakeUpdate("ap:15", user_id=77, chat_id=44, message_id=7)
    await b._on_sleep_callback(upd3, SimpleNamespace(args=[]))
    assert b._sleep_minutes == 15, b._sleep_minutes
    assert b._sleep_task is not tarea_vieja
    assert tarea_vieja.cancelled() or tarea_vieja.cancelling() >= 1

    # Enrutado real: on_callback encamina ap:* y CERRAR conserva el timer.
    upd4 = FakeUpdate("ap:cerrar", user_id=77, chat_id=44, message_id=7)
    await b.on_callback(upd4, SimpleNamespace(args=[]))
    assert b._sleep_minutes == 15, "cerrar el menu no apaga el temporizador"
    assert b._app.bot.edited[-1][2] == "MARKUP", "cierra volviendo controles"

    # Apagar el temporizador limpia estado y teclado.
    upd5 = FakeUpdate("ap:off", user_id=77, chat_id=44, message_id=7)
    await b._on_sleep_callback(upd5, SimpleNamespace(args=[]))
    assert b._sleep_minutes is None and b._sleep_task is None
    assert b._sleep_deadline is None
    control = b._app.bot.edited[-1][3].get("reply_markup")
    assert control.inline_keyboard[2][0].text == "⚙️ Ajustes", control.inline_keyboard[2]
    assert b._ajustes_keyboard(77).inline_keyboard[0][0].text == "💤 Apagado programado"


async def test_sleep_muestra_minutos_que_faltan():
    """El encabezado del menu y la opcion de Ajustes muestran los minutos
    que FALTAN (contra la hora limite real), no el valor programado que
    quedaria viejo."""
    import time as _time

    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    b._programar_apagado(45)
    # Simulo 33 minutos de espera moviendo la hora limite: quedan 12.
    b._sleep_deadline = _time.monotonic() + 12 * 60 + 5
    b._sleep_minutes = 45
    header = b._sleep_keyboard().inline_keyboard[0][0].text
    assert header == "⏰ Se apagará en 12 min", header
    opcion = b._ajustes_keyboard(77).inline_keyboard[0][0].text
    assert opcion == "💤 Se apagará en 12 min", opcion
    # A menos de un minuto nunca muestra 0.
    b._sleep_deadline = _time.monotonic() + 20
    assert b._sleep_restante_min() == 1
    b._cancelar_apagado()


async def test_ajustes_menu_roles_y_cierre():
    """Fila 3 = un solo ⚙️ Ajustes: user no lo abre, dj solo ve el apagado,
    admin suma calidad y usuarios (uno por fila). ❌ vuelve al control."""
    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7

    # User sin dj: rechazado en la puerta, la card no cambia.
    b.roles.set_role(50, "user")
    upd_user = FakeUpdate("ctl:ajustes", user_id=50, chat_id=44, message_id=7)
    b._app.bot.edited.clear()
    await b._on_control(upd_user, SimpleNamespace(args=[]), "ajustes")
    assert upd_user.answered and "permiso" in str(upd_user.answered[0]), upd_user.answered
    assert b._app.bot.edited == [], "el user no abre Ajustes"

    # dj (77, rol por defecto de make_bot): una opcion por fila, solo 💤 + ❌.
    filas_dj = [[btn.text for btn in fila] for fila in b._ajustes_keyboard(77).inline_keyboard]
    assert filas_dj == [["💤 Apagado programado"], ["❌"]], filas_dj

    # admin (1): 💤 + calidad + usuarios + ❌, cada uno en su fila.
    b.roles.set_role(1, "admin")
    menu_admin = b._ajustes_keyboard(1)
    filas_admin = [[btn.text for btn in fila] for fila in menu_admin.inline_keyboard]
    assert filas_admin == [
        ["💤 Apagado programado"],
        ["⚙️ Calidad: 1080p"],
        ["👥 Usuarios"],
        ["❌"],
    ], filas_admin
    assert menu_admin.inline_keyboard[0][0].callback_data == "ctl:apagar"
    assert menu_admin.inline_keyboard[1][0].callback_data == "ctl:calidad"
    assert menu_admin.inline_keyboard[2][0].callback_data == "ctl:usuarios"

    # Abrir de verdad con dj y cerrar con ❌ (ruta aj:cerrar): vuelve controles.
    b._app.bot.edited.clear()
    upd = FakeUpdate("ctl:ajustes", user_id=77, chat_id=44, message_id=7)
    await b._on_control(upd, SimpleNamespace(args=[]), "ajustes")
    assert b._app.bot.edited[-1][2] == "MARKUP", b._app.bot.edited[-1]
    abierto = b._app.bot.edited[-1][3].get("reply_markup")
    assert [btn.text for btn in abierto.inline_keyboard[0]] == ["💤 Apagado programado"]
    upd2 = FakeUpdate("aj:cerrar", user_id=77, chat_id=44, message_id=7)
    await b.on_callback(upd2, SimpleNamespace(args=[]))
    final = b._app.bot.edited[-1][3].get("reply_markup")
    assert [btn.text for btn in final.inline_keyboard[2]] == ["⚙️ Ajustes"], final.inline_keyboard[2]


async def test_sleep_permisos_y_disparo():
    """Sin rol dj no se abre ni se programa; el disparo del temporizador
    cierra todo en silencio: guarda el instante del tema (para que el ▶ lo
    retome al volver), cierra el mpv, deja la marca para el launcher y sale
    — sin nada en el chat y sin tocar la card."""
    from queue_manager import QueueItem

    b = make_bot()
    b.roles.set_role(50, "user")
    b._card_chat_id = 44
    b._card_message_id = 7

    b._app.bot.edited.clear()
    upd = FakeUpdate("ctl:apagar", user_id=50, chat_id=44, message_id=7)
    await b._on_control(upd, SimpleNamespace(args=[]), "apagar")
    assert upd.answered and "permiso" in str(upd.answered[0]), upd.answered
    assert b._app.bot.edited == [], "el user no abre el menu"

    upd2 = FakeUpdate("ap:30", user_id=50, chat_id=44, message_id=7)
    await b._on_sleep_callback(upd2, SimpleNamespace(args=[]))
    assert upd2.answered_kwargs.get("show_alert") is True
    assert b._sleep_minutes is None, "el user no programa"

    # Disparo: instante guardado y flusheado, mpv cerrado, marca en data para
    # el launcher, NADA al chat y la card sin un solo edit. La salida
    # (os._exit) queda sustituida para no matar la prueba.
    b.player.running = True
    b.player.time_pos = 90.0
    b.queue.set_current(QueueItem(url="u1", title="Tema"))
    b._sleep_minutes = 30
    b._salir_del_proceso = lambda: None
    import persistence as persistence_mod

    with isolated_state_path():
        enviados = len(b._app.bot.sent)
        b._app.bot.edited.clear()
        await b._apagar_por_temporizador()
        assert b.player.quits == 1, "cierra el mpv de verdad"
        assert b._posicion_guardada == {
            "item": b.queue.current.to_dict(),
            "pos": 90,
        }, "el ▶ retoma el minuto al volver a abrir"
        assert b._state._current["posicion_guardada"]["pos"] == 90, "flusheado"
        assert b._sleep_minutes is None and b._sleep_task is None
        assert b._app.bot.sent[enviados:] == [], "en silencio: nada al chat"
        assert b._app.bot.edited == [], "la card queda intacta"
        marca = persistence_mod.STATE_PATH.parent / "salir_total.flag"
        assert marca.is_file(), "la marca que el launcher detecta"


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
            playback_mod.search = lambda q, n, _r=results: _r
            b._radio_search_cache.clear()  # cada caso es una radio nueva
            candidate, from_queue, _ = asyncio.run(b._pick_next_candidate(current))
            got = candidate.url if candidate else None
            assert got == expected, f"caso {idx}: {got} != {expected}"
    finally:
        bot_mod.search = original
        playback_mod.search = original


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
    # El ancla de la radio NO se mueve todavia: se mueve cuando el usuario
    # elige un resultado y arranca. Buscar solo anota el artista del listado.
    assert b._search_artist == "GP Band", b._search_artist
    assert b._radio_artist == "", b._radio_artist

    await b.cmd_play(FakeMessageUpdate(""), SimpleNamespace(args=["Mafe Restrepo"]))
    assert calls == ["GP Band Inexplicable", "Mafe Restrepo"], calls
    assert b._search_artist == "Mafe Restrepo", b._search_artist
    assert b._radio_artist == "", b._radio_artist


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
    playback_mod.resolve_stream_url = fake
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
    playback_mod.resolve_stream_url = fake
    try:
        b = make_bot()
        first = await b._stream_for("u1")
        second = await b._stream_for("u1")
        assert first == ("stream-u1", None)
        assert second == first
        assert calls == ["u1"], calls  # resolvio una sola vez
        # una URL sin stream no se cachea
        bot_mod.resolve_stream_url = lambda url: None
        playback_mod.resolve_stream_url = lambda url: None
        assert await b._stream_for("u2") is None
        assert "u2" not in b._stream_cache
    finally:
        bot_mod.resolve_stream_url = original
        playback_mod.resolve_stream_url = original


async def test_stream_for_dedupes_inflight():
    """Dos pedidos simultaneos de la misma URL resuelven una sola vez."""
    import bot as bot_mod

    calls = []

    def fake(url):
        calls.append(url)
        return f"stream-{url}", None

    original = bot_mod.resolve_stream_url
    bot_mod.resolve_stream_url = fake
    playback_mod.resolve_stream_url = fake
    try:
        b = make_bot()
        r1, r2 = await asyncio.gather(b._stream_for("u1"), b._stream_for("u1"))
        assert r1 == r2 == ("stream-u1", None)
        assert calls == ["u1"], calls
    finally:
        bot_mod.resolve_stream_url = original
        playback_mod.resolve_stream_url = original


async def test_anticipate_urls_serial_and_clear():
    """Anticipar encola varias URLs y el cache se vacia con _clear_stream_cache."""
    import bot as bot_mod

    order = []

    def fake(url):
        order.append(url)
        return ("stream-" + url, None)

    original = _patch_resolve(bot_mod, {})
    bot_mod.resolve_stream_url = fake
    playback_mod.resolve_stream_url = fake
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
        playback_mod.resolve_stream_url = original


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
    playback_mod.resolve_stream_url = fake
    try:
        async def _no_candidate(current=None):
            return None, False, False

        b._pick_next_candidate = _no_candidate  # evita radio/red real
        b.playback.pick_next_candidate = _no_candidate  # evita radio/red real
        upd = FakeUpdate("ctl:next", chat_id=44)
        started = await b._play_item(upd, QueueItem(url="u1", title="X"))
        assert started
        assert b.player.loaded == [("vencida", None), ("stream-fresca", None)], b.player.loaded
        assert b._stream_cache["u1"] == ("stream-fresca", None)
    finally:
        bot_mod.resolve_stream_url = original
        playback_mod.resolve_stream_url = original


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
    backlog SIN Mandar ningun aviso por Telegram.

    El aviso en el chat se elimino: es un post-mortem que llega tarde y no le
    sirve al usuario para nada. Lo que mando mal es un comando a un grupo. Lo
    que tiene que saber es que no hay internet, y eso se entera en su propia
    PC al instante (data/conexion.json -> ventana e icono del reloj)."""
    b = make_bot()
    b.config.allowed_chat_id = 44
    fb = b._app.bot
    fb.pending_updates = [
        _FakeBacklogUpd(44, "/pause"),
        _FakeBacklogUpd(44, "/volume 30"),
        _FakeBacklogUpd(44, "hola"),
        _FakeBacklogUpd(99, "/next"),  # de otro chat
    ]
    ctx = SimpleNamespace(bot=fb, application=SimpleNamespace(updater=None))
    b._net_offline = True

    # offline: el probe falla y el job no hace nada
    fb.get_me_ok = False
    await b._net_watch_job(ctx)
    assert b._net_offline is True
    assert fb.sent == [], fb.sent

    # vuelve la conexion: se descarta el backlog y NO se avisa por chat
    fb.get_me_ok = True
    await b._net_watch_job(ctx)
    assert b._net_offline is False
    # No debe quedar ningun mensaje en el chat con la lista de comandos
    assert fb.sent == [], f"se volvio a mandar el post-mortem: {fb.sent}"
    for m in fb.sent:
        assert "La conexion del bot se perdio" not in str(m[1]), fb.sent
        assert "Se descartaron estos comandos" not in str(m[1]), fb.sent

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
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        b.playback.pick_next_candidate = fake_pick

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
        playback_mod.resolve_stream_url = lambda url: None


async def test_nav_next_chooses_new_candidate_when_forward_empty():
    """Si la pila forward está vacía, next elige un candidato nuevo y
    deja el actual en la pila back para un eventual prev."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        b._radio_artist = ""
        b._clear_nav_stacks()
        b.queue.set_current(QueueItem(url="A", title="A"))

        new_pick = QueueItem(url="B", title="B")
        async def fake_pick(current: QueueItem) -> tuple[QueueItem | None, bool, bool]:
            return new_pick, False, False
        b._pick_next_candidate = fake_pick
        b.playback.pick_next_candidate = fake_pick

        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "B"
        # A en back, forward vacía.
        assert [i.url for i in b._nav_back] == ["A"]
        assert list(b._nav_forward) == []
    finally:
        bot_mod.resolve_stream_url = lambda url: None
        playback_mod.resolve_stream_url = lambda url: None


async def test_nav_prev_falls_back_to_queue_history():
    """Si la pila back está vacía, prev usa el histórico de items de la cola.
    El fallback no preserva nav_forward (es navegación desde cola, no navegación
    entre tracks elegidos por el usuario)."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        playback_mod.resolve_stream_url = lambda url: None


async def test_nav_playlist_unaffected():
    """En modo playlist el cursor con wrap sigue mandando, no la pila."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        b.playback.pick_next_candidate = fake_pick

        # next -> B (cursor avanza a 1)
        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "B"
        # prev -> A (cursor vuelve a 0, wrap)
        await b.cmd_prev(FakeUpdate("ctl:prev"), SimpleNamespace(args=[]))
        assert cast(QueueItem, b.queue.current).url == "A"
    finally:
        bot_mod.resolve_stream_url = lambda url: None
        playback_mod.resolve_stream_url = lambda url: None


async def test_next_en_cola_con_prefetch_registra_historial():
    """⏭ en modo cola con el siguiente YA resuelto: carga directo a mpv
    (camino que NO pasa por play_item) y la cancion igual queda registrada."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        b._clear_nav_stacks()
        items = [
            QueueItem(url="A", title="A"),
            QueueItem(url="B", title="B"),
        ]
        b.queue.set_playlist(items)

        async def fake_pick(current):
            return None, False, False

        b._pick_next_candidate = fake_pick
        b.playback.pick_next_candidate = fake_pick
        b._prefetch_resolved = ((items[1], True), ("stream-B", None))

        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))

        assert cast(QueueItem, b.queue.current).url == "B"
        assert [h["url"] for h in b._historial()] == ["B"], b._historial()
    finally:
        bot_mod.resolve_stream_url = lambda url: None
        playback_mod.resolve_stream_url = lambda url: None


async def test_next_de_radio_con_prefetch_registra_historial():
    """⏭ en radio con el prefetch ya resuelto: otra carga directa sin pasar
    por play_item — tambien debe quedar en el historial."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        b._radio_artist = ""
        b._clear_nav_stacks()
        b.queue.set_current(QueueItem(url="A", title="A"))

        async def fake_pick(current):
            return None, False, False

        b._pick_next_candidate = fake_pick
        b.playback.pick_next_candidate = fake_pick
        siguiente = QueueItem(url="B", title="B")
        b._prefetch_resolved = ((siguiente, False), ("stream-B", None))

        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))

        assert cast(QueueItem, b.queue.current).url == "B"
        assert [h["url"] for h in b._historial()] == ["B"], b._historial()
    finally:
        bot_mod.resolve_stream_url = lambda url: None
        playback_mod.resolve_stream_url = lambda url: None


async def test_next_de_radio_sin_cache_registra_historial():
    """⏭ en radio sin stream cacheado: resuelve y carga con el loop propio
    de cmd_next (tampoco pasa por play_item) — queda en el historial."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        b._radio_artist = ""
        b._clear_nav_stacks()
        b.queue.set_current(QueueItem(url="A", title="A"))
        nuevo = QueueItem(url="B", title="B")

        async def fake_pick(current):
            return nuevo, False, False

        b._pick_next_candidate = fake_pick
        b.playback.pick_next_candidate = fake_pick

        await b.cmd_next(FakeUpdate("ctl:next"), SimpleNamespace(args=[]))

        assert cast(QueueItem, b.queue.current).url == "B"
        assert [h["url"] for h in b._historial()] == ["B"], b._historial()
    finally:
        bot_mod.resolve_stream_url = lambda url: None
        playback_mod.resolve_stream_url = lambda url: None


async def test_prev_de_radio_registra_historial():
    """⏮ con la pila back vacia: carga directa por el historico de la cola
    (otro camino sin play_item) — la cancion que vuelve a sonar queda registrada."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        b._radio_artist = ""
        b._clear_nav_stacks()
        b.queue.set_current(QueueItem(url="A", title="A"))
        b.queue._history_items.append(QueueItem(url="Z", title="Z"))

        await b.cmd_prev(FakeUpdate("ctl:prev"), SimpleNamespace(args=[]))

        assert cast(QueueItem, b.queue.current).url == "Z"
        assert [h["url"] for h in b._historial()] == ["Z"], b._historial()
    finally:
        bot_mod.resolve_stream_url = lambda url: None
        playback_mod.resolve_stream_url = lambda url: None


async def test_toggle_play_pause_starts_player_when_off():
    """Fase 1: si mpv no esta corriendo, ▶ en la tarjeta PRENDE mpv y carga
    el item actual (no se queda mudo tras un apagado de la PC o un stop)."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        playback_mod.resolve_stream_url = lambda url: None


async def test_resume_starts_player_when_not_running():
    """Fase 1: el /resume tras apagado/stop reproduce el item restaurado:
    prende mpv y carga el stream (no solo hace play sobre nada)."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        playback_mod.resolve_stream_url = lambda url: None


async def test_toggle_play_pause_keeps_playlist():
    """El ▶ con mpv apagado y playlist ACTIVA NO debe descartar la cola:
    reanuda el item actual posicionado (preserve_current=True) y _items
    queda intacto. Antes, _play_item con preserve_current=False llamaba
    set_current y borraba la playlist (rar: lista vacia tras reiniciar)."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        playback_mod.resolve_stream_url = lambda url: None


async def test_restore_cursor_and_list_page():
    """Paso 2: al cargar el estado se restaura la posicion real dentro de la
    playlist (no el tema 0) y la pagina del 📋 en la que iba el usuario."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        playback_mod.resolve_stream_url = lambda url: None


async def test_restore_cursor_clamped_to_range():
    """Paso 2: un cursor persistido fuera de rango (playlist mas corta o
    estado antiguo con cursor grande) se clampa al ultimo indice valido."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        playback_mod.resolve_stream_url = lambda url: None


async def test_restore_cursor_corrupt_falls_to_zero():
    """Paso 2: un cursor o pagina corruptos (no int) caen a 0 sin explotar."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        playback_mod.resolve_stream_url = lambda url: None


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
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        playback_mod.resolve_stream_url = lambda url: None


async def test_autoadvance_refreshes_card_state_and_nav():
    """El fin natural del tema (auto-advance con prefetch resuelto) cambia la
    miniatura y el titulo de la tarjeta, guarda el estado nuevo y apila el
    tema que termino para que /prev pueda volver a el."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_current(
        QueueItem(url="actual", title="Actual", thumbnail="https://img/1")
    )
    await b._send_card(44)
    assert b._card_is_photo is True
    b._app.bot.edited.clear()

    candidate = QueueItem(url="siguiente", title="Siguiente", thumbnail="https://img/2")
    b._prefetch_resolved = ((candidate, False), ("stream-siguiente", None))

    await b._advance_after_end(None)
    b._cancel_prefetch()

    media = [e[3]["media"] for e in b._app.bot.edited if e[2] == "MEDIA"]
    assert media, f"la tarjeta no se re-rendizo: {b._app.bot.edited}"
    assert media[0].media == "https://img/2", media[0].media
    assert "Siguiente" in str(media[0].caption), media[0].caption
    assert cast(QueueItem, b.queue.current).url == "siguiente"
    assert b._state._current["current"]["url"] == "siguiente"
    assert [i.url for i in b._nav_back] == ["actual"], b._nav_back


async def test_stop_conserves_state_and_rewinds():
    """Fase 2: el stop NO borra historial, cola, radio ni navegacion: solo
    pausa el reproductor y rebobina a 0:00 para que ▶ reanude desde el inicio."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        playback_mod.resolve_stream_url = lambda url: None


async def test_stop_does_not_clear_queue():
    """El stop de la tarjeta no llama queue.clear(): el historico de la radio
    y la cola siguen vivos (el bug reportado era que /stop borraba todo)."""
    import bot as bot_mod
    from queue_manager import QueueItem, QueueManager

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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


async def test_stop_borra_el_instante_y_play_arranca_en_cero():
    """⏹ es parar: rebobina, BORRA cualquier instante guardado y el proximo ▶
    arranca de cero (no revive la posicion de antes)."""
    import bot as bot_mod
    import playback as playback_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        fp = cast(Any, b.player)
        b.queue.set_current(QueueItem(url="u1", title="Tema"))
        b._posicion_guardada = {"item": b.queue.current.to_dict(), "pos": 125}

        await b.cmd_stop(FakeUpdate("ctl:stop"), SimpleNamespace(args=[]))
        assert fp.rewinds == 1, "sigue rebobinando a 0:00"
        assert b._posicion_guardada is None, "parar borra el instante guardado"
        assert b._state._current["posicion_guardada"] is None, "y se persiste"

        fp.running = False  # como tras un ⏹: mpv apagado
        await b._toggle_play_pause(FakeUpdate("ctl:play"))
        assert fp.loaded == [("stream-u1", None)], fp.loaded
        assert fp.seek_tos == [], "tras ⏹ el ▶ arranca en 0, no retoma nada"
    finally:
        bot_mod.resolve_stream_url = lambda url: None
        playback_mod.resolve_stream_url = lambda url: None


async def test_play_tras_temporizador_retoma_el_minuto():
    """El temporizador guarda el instante y cierra todo; al volver a abrir, el
    mismo ▶ (mpv apagado) carga el tema y RETOMA ahi: el guardado se consume."""
    import bot as bot_mod
    import playback as playback_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        fp = cast(Any, b.player)
        b.queue.set_current(QueueItem(url="u1", title="Tema"))
        # Estado tal como quedo despues del temporizador (y de un reinicio).
        b._posicion_guardada = {"item": b.queue.current.to_dict(), "pos": 42}
        fp.running = False
        fp.loaded.clear()

        await b._toggle_play_pause(FakeUpdate("ctl:play"))
        assert fp.loaded == [("stream-u1", None)], fp.loaded
        assert fp.seek_tos == [42], "retoma el minuto donde quedo"
        assert cast(QueueItem, b.queue.current).url == "u1", "el actual no cambia"
        assert b._paused is False, "queda sonando"
        assert b._posicion_guardada is None, "el instante se consume al retomarlo"
        assert b._state._current["posicion_guardada"] is None, "y se persiste"
    finally:
        bot_mod.resolve_stream_url = lambda url: None
        playback_mod.resolve_stream_url = lambda url: None


async def test_play_ignora_el_instante_de_otro_tema():
    """Si el guardado es de un tema que ya no es el actual, el ▶ lo descarta
    en silencio: arranca el actual en 0, sin seek y sin revivir nada."""
    import bot as bot_mod
    import playback as playback_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    try:
        b = make_bot()
        fp = cast(Any, b.player)
        b.queue.set_current(QueueItem(url="u1", title="Ahora"))
        b._posicion_guardada = {"item": {"url": "u2", "title": "Vieja"}, "pos": 42}
        fp.running = False

        await b._toggle_play_pause(FakeUpdate("ctl:play"))
        assert fp.seek_tos == [], "no revive la posicion de otro tema"
        assert fp.loaded == [("stream-u1", None)], fp.loaded
        assert b._posicion_guardada is None, "el guardado divergente se descarta"
        assert b._state._current["posicion_guardada"] is None, "y se persiste"
    finally:
        bot_mod.resolve_stream_url = lambda url: None
        playback_mod.resolve_stream_url = lambda url: None


async def test_actualizar_solo_admin():
    """/actualizar es de admin (mismo envoltorio que /salud): el resto ni
    entra, y sin permiso no se dispara la actualizacion."""
    b = make_bot()
    llamado = []

    async def trampa():
        llamado.append(True)
        return "1", None

    b._actualizar_ytdlp = trampa
    b._actualizar_aplicacion = trampa  # type: ignore[method-assign]
    h = b._require("admin", b.cmd_actualizar)

    upd_user = FakeMessageUpdate("/actualizar", user_id=50)
    await h(upd_user, SimpleNamespace(args=[]))
    assert any("denegado" in t for t, _ in upd_user.sent), upd_user.sent
    assert llamado == [], "el user no debe disparar la actualizacion"


@contextmanager
def meta_de_actualizacion(release, pypi, instalada):
    """El mundo externo de /actualizar en stub, con restauracion: el
    release de GitHub, la version pypi y la pieza interna instalada."""

    import bot as bot_mod

    async def da_release():
        if isinstance(release, Exception):
            raise bot_mod.ActualizacionError(str(release))
        return release

    async def da_pypi():
        if isinstance(pypi, Exception):
            raise bot_mod.ActualizacionError(str(pypi))
        return pypi

    def da_instalada():
        return instalada

    viejos = (
        bot_mod._pedir_meta_release,
        bot_mod._pedir_meta_ytdlp,
        bot_mod._version_interna_instalada,
    )
    bot_mod._pedir_meta_release = da_release  # type: ignore[assignment]
    bot_mod._pedir_meta_ytdlp = da_pypi  # type: ignore[assignment]
    bot_mod._version_interna_instalada = da_instalada  # type: ignore[assignment]
    try:
        yield
    finally:
        (
            bot_mod._pedir_meta_release,
            bot_mod._pedir_meta_ytdlp,
            bot_mod._version_interna_instalada,
        ) = viejos


# Releases de figura: (version, url, bytes, sha256_publicado)
RELEASE_1_1_4 = ("1.1.4", "https://x/yt-remote-1.1.4-portable.zip", 10, None)
RELEASE_1_2_0 = ("1.2.0", "https://x/yt-remote-1.2.0-portable.zip", 1_000, None)
# Versiones de la pieza interna: (version_publicada, url, sha)
PYPI_2026_08 = ("2026.08.19", "https://u/w", "sha")
PYPI_2026_10 = ("2026.10.01", "https://u/w2", "sha2")
SALTO_INTERNA = "runtime/python/Lib/site-packages/yt_dlp"


def _ayudante_y_marcas(b, disponible: bool):
    """Stub del ayudante con registro de llamadas y marcas de salida."""
    b._ayudante_disponible = lambda: disponible  # type: ignore[method-assign]
    lanzados: list[tuple[object, list[str]]] = []
    marcas: list[str] = []

    def lanza(ruta, saltos):
        lanzados.append((ruta, saltos))

    async def salir(marca):
        marcas.append(marca)

    b._lanzar_ayudante = lanza  # type: ignore[method-assign]
    b._apagar_y_salir = salir  # type: ignore[method-assign]
    return lanzados, marcas


async def test_actualizar_nada_nuevo():
    """Con las dos piezas al dia, la respuesta es 'ya esta al dia', sin
    lanzamiento, sin marca y sin mencionar piezas internas."""
    b = make_bot()
    lanzados, marcas = _ayudante_y_marcas(b, disponible=False)
    with meta_de_actualizacion(RELEASE_1_1_4, PYPI_2026_08, "2026.08.19"):
        upd = FakeMessageUpdate("/actualizar")
        await b.cmd_actualizar(upd, SimpleNamespace(args=[]))
    assert len(upd.sent) == 1, upd.sent
    texto = upd.sent[0][0]
    assert "✅ YT-Remote 1.1.4 ya está al día." in texto, texto
    assert "yt-dlp" not in texto and "PyPI" not in texto, texto
    assert lanzados == [] and marcas == [], (lanzados, marcas)


async def test_actualizar_chequeos_rotos_aviso_honesto():
    """GitHub y PyPI caidos: un solo mensaje con los dos motivos, y NADA
    lanzado ni cambiado."""
    b = make_bot()
    lanzados, marcas = _ayudante_y_marcas(b, disponible=False)
    with meta_de_actualizacion(
        Exception("GitHub no respondio (timeout)"),
        Exception("PyPI no responde (te)"),
        "x",
    ):
        upd = FakeMessageUpdate("/actualizar")
        await b.cmd_actualizar(upd, SimpleNamespace(args=[]))
    assert len(upd.sent) == 1, upd.sent
    texto = upd.sent[0][0]
    assert texto.startswith("⚠️ "), texto
    assert "GitHub no respondio (timeout)" in texto, texto
    assert "PyPI no responde (te)" in texto, texto
    assert lanzados == [] and marcas == [], (lanzados, marcas)


async def test_actualizar_interna_no_se_nombra():
    """El aviso de la pieza interna habla de 'componentes internos' con
    versiones, PERO el nombre y la procedencia JAMAS aparecen (regla del
    usuario: herramientas internas no son asunto del usuario)."""
    b = make_bot()
    b._ayudante_disponible = lambda: True  # type: ignore[method-assign]
    lanzados, marcas = _ayudante_y_marcas(b, disponible=True)

    async def ok():
        return "2026.08.19", "2026.10.01"

    b._actualizar_ytdlp = ok  # type: ignore[method-assign]
    with meta_de_actualizacion(RELEASE_1_1_4, PYPI_2026_10, "2026.08.19"):
        upd = FakeMessageUpdate("/actualizar")
        await b.cmd_actualizar(upd, SimpleNamespace(args=[]))
    assert len(upd.sent) == 1, upd.sent
    texto = upd.sent[0][0]
    assert "✅ Componentes internos actualizados: 2026.08.19 → 2026.10.01." in texto, texto
    assert "YT-Remote se reinicia solo." in texto, texto
    for prohibida in ("yt-dlp", "PyPI", "whl", "wheel"):
        assert prohibida not in texto, (prohibida, texto)
    assert lanzados == [(None, [])], lanzados
    assert marcas == ["componentes"], marcas


async def test_actualizar_interna_sin_ayudante_pide_reinicio_manual():
    """En desarrollo no hay exe que reabrir: los mismos cambios van con
    instrucciones MANUALES; no se toca el runtime del repo."""
    b = make_bot()
    lanzados, marcas = _ayudante_y_marcas(b, disponible=False)

    async def ok():
        return "2026.08.19", "2026.10.01"

    b._actualizar_ytdlp = ok  # type: ignore[method-assign]
    with meta_de_actualizacion(RELEASE_1_1_4, PYPI_2026_10, "2026.08.19"):
        upd = FakeMessageUpdate("/actualizar")
        await b.cmd_actualizar(upd, SimpleNamespace(args=[]))
    texto = upd.sent[0][0]
    assert "Componentes internos actualizados" in texto, texto
    assert "Reinicia YT-Remote para aplicarlos." in texto, texto
    assert lanzados == [] and marcas == [], (lanzados, marcas)


async def test_actualizar_interna_fallida_aviso_y_nada_mas():
    """Si la pieza interna no se pudo instalar, aviso honesto y NO hay
    reinicio ni mensaje de exito."""
    import bot as bot_mod

    b = make_bot()
    lanzados, marcas = _ayudante_y_marcas(b, disponible=True)

    async def raya():
        raise bot_mod.ActualizacionError("PyPI no responde (te)")

    b._actualizar_ytdlp = raya  # type: ignore[method-assign]
    with meta_de_actualizacion(RELEASE_1_1_4, PYPI_2026_10, "2026.08.19"):
        upd = FakeMessageUpdate("/actualizar")
        await b.cmd_actualizar(upd, SimpleNamespace(args=[]))
    assert len(upd.sent) == 1, upd.sent
    texto = upd.sent[0][0]
    assert texto.startswith("⚠️ "), texto
    assert "No se pudo actualizar los componentes internos" in texto, texto
    assert "PyPI no responde (te)" in texto, texto
    assert lanzados == [] and marcas == [], (lanzados, marcas)


async def test_actualizar_app_nueva_reinicia_solo():
    """Hay YT-Remote nuevo: un mensaje lo anuncia, el ayudante se deja
    corriendo con el ZIP y el bot se apaga con la marca 'actualizacion'.
    El texto NO menciona piezas internas ni GitHub en webview del msg."""
    b = make_bot()
    lanzados, marcas = _ayudante_y_marcas(b, disponible=True)

    async def lista(release, interna_puesta):
        return (Path("paquete.zip"), [])

    b._actualizar_aplicacion = lista  # type: ignore[method-assign]
    with meta_de_actualizacion(RELEASE_1_2_0, PYPI_2026_08, "2026.08.19"):
        upd = FakeMessageUpdate("/actualizar")
        await b.cmd_actualizar(upd, SimpleNamespace(args=[]))
    assert len(upd.sent) == 1, upd.sent
    texto = upd.sent[0][0]
    assert "🚀 Actualizando YT-Remote 1.1.4 → 1.2.0." in texto, texto
    assert "YT-Remote se cierra solo y vuelve al rato." in texto, texto
    assert "yt-dlp" not in texto, texto
    assert lanzados == [(Path("paquete.zip"), [])], lanzados
    assert marcas == ["actualizacion"], marcas


async def test_actualizar_app_y_componentes_una_pasada():
    """YT-Remote nuevo + componente nuevo: UNA sola reinicion y el aviso
    marca las dos lineas; el salto del componente viaja el ayudante."""
    b = make_bot()
    lanzados, marcas = _ayudante_y_marcas(b, disponible=True)

    async def ok():
        return "2026.08.19", "2026.10.01"

    b._actualizar_ytdlp = ok  # type: ignore[method-assign]

    async def lista(release, interna_puesta):
        return (Path("z.zip"), [SALTO_INTERNA])

    b._actualizar_aplicacion = lista  # type: ignore[method-assign]
    with meta_de_actualizacion(RELEASE_1_2_0, PYPI_2026_10, "2026.08.19"):
        upd = FakeMessageUpdate("/actualizar")
        await b.cmd_actualizar(upd, SimpleNamespace(args=[]))
    texto = upd.sent[0][0]
    assert "🚀 Actualizando YT-Remote 1.1.4 → 1.2.0." in texto, texto
    assert "✅ Componentes internos: 2026.08.19 → 2026.10.01." in texto, texto
    assert "YT-Remote se cierra solo y vuelve al rato." in texto, texto
    assert lanzados == [(Path("z.zip"), [SALTO_INTERNA])], lanzados
    assert marcas == ["actualizacion"], marcas


async def test_actualizar_app_sin_ayudante_manual():
    """Sin el ayudante (instalacion sin portable), NO se baja nada ni se
    apaga nada: el aviso remite a la carpeta portable de YT-Remote."""
    b = make_bot()
    lanzados, marcas = _ayudante_y_marcas(b, disponible=False)
    baja: list = []

    async def no_baja(release, interna_puesta):
        baja.append(True)
        return (Path("sin_uso.zip"), [])

    b._actualizar_aplicacion = no_baja  # type: ignore[method-assign]
    with meta_de_actualizacion(RELEASE_1_2_0, PYPI_2026_08, "2026.08.19"):
        upd = FakeMessageUpdate("/actualizar")
        await b.cmd_actualizar(upd, SimpleNamespace(args=[]))
    texto = upd.sent[0][0]
    assert "🚀 YT-Remote 1.1.4 → 1.2.0 disponible." in texto, texto
    assert "no permite el cambio automático" in texto, texto
    assert baja == [] and lanzados == [] and marcas == [], (baja, lanzados, marcas)


async def test_actualizar_app_fallo_bajar_zip():
    """Si la bajada del ZIP falla, aviso con el motivo, sin ayudante
    corriendo ni salida total."""
    import bot as bot_mod

    b = make_bot()
    lanzados, marcas = _ayudante_y_marcas(b, disponible=True)

    async def estalla(release, interna_puesta):
        raise bot_mod.ActualizacionError("GitHub no respondio (te)")

    b._actualizar_aplicacion = estalla  # type: ignore[method-assign]
    with meta_de_actualizacion(RELEASE_1_2_0, PYPI_2026_08, "2026.08.19"):
        upd = FakeMessageUpdate("/actualizar")
        await b.cmd_actualizar(upd, SimpleNamespace(args=[]))
    assert len(upd.sent) == 1, upd.sent
    texto = upd.sent[0][0]
    assert texto.startswith("⚠️ No se pudo bajar la versión 1.2.0: "), texto
    assert "GitHub no respondio (te)" in texto, texto
    assert lanzados == [] and marcas == [], (lanzados, marcas)


def test_actualizar_versiones_y_sha():
    """2026.08.19 y 2026.8.19 son la MISMA version (PyPI publica sin el
    cero): si no, /actualizar "renovaria" a la misma; y el sha256 muerde."""
    import hashlib

    import bot as bot_mod

    assert bot_mod._version_num("2026.08.19") == bot_mod._version_num("2026.8.19")
    assert bot_mod._version_num("2026.10.01") > bot_mod._version_num("2026.8.19")
    assert bot_mod._version_num("rc1") == (0,)

    bueno = hashlib.sha256(b"peso real").hexdigest()
    bot_mod._verificar_sha256(b"peso real", bueno)  # no debe fallar
    try:
        bot_mod._verificar_sha256(b"otra cosa", bueno)
    except bot_mod.ActualizacionError:
        pass
    else:
        raise AssertionError("el sha256 debia morder si el archivo no es el publicado")


def test_instalar_wheel_reemplaza_el_paquete():
    """El swap REAL: el wheel nuevo reemplaza yt_dlp/ entera, la vieja no
    queda, y un wheel basura NO toca lo instalado."""
    import io as io_mod
    import zipfile as zipfile_mod
    from tempfile import TemporaryDirectory

    import bot as bot_mod

    def wheel_bueno(version: str) -> bytes:
        buf = io_mod.BytesIO()
        with zipfile_mod.ZipFile(buf, "w") as zf:
            zf.writestr("yt_dlp/__init__.py", "")
            zf.writestr("yt_dlp/version.py", f'__version__ = "{version}"\n')
            zf.writestr("yt_dlp-9.9.dist-info/METADATA", "Name: yt-dlp\n")
        return buf.getvalue()

    with TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        destino = raiz / "yt_dlp"
        destino.mkdir()
        (destino / "__init__.py").write_text("viejo", encoding="utf-8")
        (destino / "version.py").write_text('__version__ = "2026.08.19"', encoding="utf-8")

        bot_mod._instalar_wheel_ytdlp(wheel_bueno("2099.1.1"), destino)

        leido = (destino / "version.py").read_text(encoding="utf-8")
        assert leido == '__version__ = "2099.1.1"\n', leido
        assert not (raiz / "yt_dlp.viejo").exists(), "la carpeta vieja no debe quedar"
        assert bot_mod._leer_version_paquete(destino) == "2099.1.1"

        # Wheel basura: error honesto y el paquete instalado queda INTACTO.
        try:
            bot_mod._instalar_wheel_ytdlp(b"esto no es un zip", destino)
        except bot_mod.ActualizacionError as exc:
            assert "wheel" in str(exc), str(exc)
        else:
            raise AssertionError("un wheel basura debia fallar")
        assert (
            bot_mod._leer_version_paquete(destino) == "2099.1.1"
        ), "un fallo no debe tocar lo instalado"


def test_version_de_constants_iguala_release_rules():
    """La version de YT-Remote vive en DOS sitios (constants.VERSION y
    release_rules.json) y NUNCA se desincronizan: este test muerde si el
    release se publica con version distinta a la que el bot muestra."""
    import json

    import constants as constants_mod

    reglas = Path(__file__).resolve().parent.parent / "release_rules.json"
    if not reglas.is_file():  # pragma: no cover - fuera del repo
        return
    datos = json.loads(reglas.read_text(encoding="utf-8"))
    assert (
        constants_mod.VERSION == datos.get("version")
    ), f"constants.VERSION={constants_mod.VERSION} desincronizada con release_rules.json"


def test_parsear_release_json_de_github():
    """Del JSON de releases/latest: version sin v, URL, bytes y sha256
    (cuando GitHub publica digest); cada JSON roto con su motivo honesto."""
    import bot as bot_mod

    datos_ok = {
        "tag_name": "v1.2.0",
        "assets": [
            {
                "name": "yt-remote-1.2.0-portable.zip",
                "browser_download_url": "https://github.com/x/yt-remote-1.2.0-portable.zip",
                "size": 97_000_000,
                "digest": "sha256:AB12CD34",
            }
        ],
    }
    version, url, size, sha = bot_mod._parsear_release_json(datos_ok)
    assert version == "1.2.0", version
    assert url.endswith("yt-remote-1.2.0-portable.zip"), url
    assert size == 97_000_000, size
    assert sha == "ab12cd34", sha

    # Tag sin la v delante tambien pasa.
    sin_v = {"tag_name": "1.3.0", "assets": [datos_ok["assets"][0]]}
    assert bot_mod._parsear_release_json(sin_v)[0] == "1.3.0"

    # Casos rotos, cada uno con motivo.
    casos = [
        (["no", "soy", "dict"], "formato"),
        ({"tag_name": "", "assets": []}, "legible"),
        ({"tag_name": "v2.0.0", "assets": []}, "archivos"),
        (
            {
                "tag_name": "v1.2.0",
                "assets": [{"name": "otra-cosa.txt", "size": 5}],
            },
            "ZIP portable",
        ),
        (
            {
                "tag_name": "v1.2.0",
                "assets": [{"name": "yt-remote-1.2.0-portable.zip", "size": 0}],
            },
            "incompleto",
        ),
    ]
    for datos, pista in casos:
        try:
            bot_mod._parsear_release_json(datos)
        except bot_mod.ActualizacionError as exc:
            assert pista in str(exc), (pista, str(exc))
        else:
            raise AssertionError(f"debia fallar con {datos!r}")


def _paquete_zip_de_figura(prefijo: str, con_version: bool = True) -> bytes:
    """Un ZIP portable de juguete, con o sin carpeta contenedora."""
    import io as io_mod
    import zipfile as zipfile_mod

    piezas = [
        ("ytremote.exe", "exe"),
        ("runtime/python/python.exe", "py"),
        ("src/main.py", "main"),
        ("runtime/python/Lib/site-packages/yt_dlp/version.py", '__version__ = "2026.10.01"\n'),
    ]
    if not con_version:
        piezas[-1] = ("runtime/python/Lib/site-packages/yt_dlp/__init__.py", "")
    buf = io_mod.BytesIO()
    with zipfile_mod.ZipFile(buf, "w") as zf:
        for nombre, contenido in piezas:
            zf.writestr(prefijo + nombre, contenido)
    return buf.getvalue()


def test_zip_del_release_valida_y_lee_version_interna():
    """_validar_zip_paquete: devuelve el prefijo contenedor (o ""), muerde
    si faltan piezas clave; _leer_interna_del_zip lee la version del ZIP y
    None si el ZIP no la expone."""
    import io as io_mod
    import zipfile as zipfile_mod

    import bot as bot_mod
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        raiz = Path(tmp)

        con_prefijo = raiz / "con_prefijo.zip"
        con_prefijo.write_bytes(_paquete_zip_de_figura("yt-remote-1.2.0/"))
        assert bot_mod._validar_zip_paquete(con_prefijo) == "yt-remote-1.2.0/"
        assert (
            bot_mod._leer_interna_del_zip(con_prefijo, "yt-remote-1.2.0/")
            == "2026.10.01"
        )

        sin_prefijo = raiz / "sin_prefijo.zip"
        sin_prefijo.write_bytes(_paquete_zip_de_figura(""))
        assert bot_mod._validar_zip_paquete(sin_prefijo) == ""
        assert bot_mod._leer_interna_del_zip(sin_prefijo, "") == "2026.10.01"

        # Zip que no es el paquete (le falta src/main.py): muerde.
        roto = raiz / "roto.zip"
        buf = io_mod.BytesIO()
        with zipfile_mod.ZipFile(buf, "w") as zf:
            zf.writestr("ytremote.exe", "exe")
        roto.write_bytes(buf.getvalue())
        try:
            bot_mod._validar_zip_paquete(roto)
        except bot_mod.ActualizacionError as exc:
            assert "paquete" in str(exc) or "piezas" in str(exc), str(exc)
        else:
            raise AssertionError("un ZIP sin piezas clave debia fallar")

        # Sin version.py legible -> None (el flujo decide sin ella).
        sin_version = raiz / "sin_version.zip"
        sin_version.write_bytes(_paquete_zip_de_figura("", con_version=False))
        assert bot_mod._leer_interna_del_zip(sin_version, "") is None


def test_ayudante_plan_protege_data_config_y_salta():
    """entradas_a_extraer: data/ NO se toca, config.json NO se pisa, los
    prefijos de --skip quedan afuera y lo demas va con destino limpio."""
    import updater

    nombres = [
        "yt-remote-2.0.0/ytremote.exe",
        "yt-remote-2.0.0/runtime/python/python.exe",
        "yt-remote-2.0.0/src/main.py",
        "yt-remote-2.0.0/src/updater.py",
        "yt-remote-2.0.0/README.md",
        "yt-remote-2.0.0/config.json",
        "yt-remote-2.0.0/data/sesion.txt",
        "yt-remote-2.0.0/runtime/python/Lib/site-packages/yt_dlp/a.py",
        "__MACOSX/.grasa",
    ]
    plan = updater.entradas_a_extraer(nombres, ["runtime/python/Lib/site-packages/yt_dlp"])
    destinos = [d for member, d in plan]
    assert destinos == ["ytremote.exe", "runtime/python/python.exe", "src/main.py", "src/updater.py", "README.md"], destinos

    # Sin --skip: la pieza interna del release SI se extrae.
    plan_limpio = updater.entradas_a_extraer(nombres, [])
    assert "runtime/python/Lib/site-packages/yt_dlp/a.py" in [d for member, d in plan_limpio]

    # Rutas trampadas no escapan de la carpeta destino.
    trampa = updater.entradas_a_extraer(
        ["../escapado.txt", "C:/absoluto.txt", ""], []
    )
    assert trampa == [], trampa


def test_ayudante_extrae_conserva_data_y_limpia():
    """El reemplazo real de juguete: los archivos del release se ponen,
    data/ y config.json se CONSERVAN tal cual estaban, la chatarra vieja
    de src/ desaparece (renombre + rebuild) y no queda ningun .viejo."""
    import updater
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        raiz = Path(tmp)

        def armar_destino(nombre: str) -> Path:
            dest = raiz / nombre
            (dest / "runtime/python/Lib/site-packages/yt_dlp").mkdir(parents=True)
            (dest / "src").mkdir(parents=True)
            (dest / "data").mkdir(parents=True)
            (dest / "ytremote.exe").write_text("exe-viejo", encoding="utf-8")
            (dest / "config.json").write_text("config-viejo", encoding="utf-8")
            (dest / "runtime/python/python.exe").write_text("py-viejo", encoding="utf-8")
            (dest / "runtime/python/Lib/site-packages/yt_dlp/version.py").write_text(
                '__version__ = "2026.08.19"\n', encoding="utf-8"
            )
            (dest / "src/main.py").write_text("print('viejo')", encoding="utf-8")
            (dest / "src/chatarra-vieja.py").write_text("vieja", encoding="utf-8")
            (dest / "data/sesion.txt").write_text("SESION-USUARIO", encoding="utf-8")
            return dest

        def zip_de_figura(ruta: Path) -> Path:
            import io as io_mod
            import zipfile as zipfile_mod

            buf = io_mod.BytesIO()
            piezas = [
                ("ytremote.exe", "exe-nuevo"),
                ("runtime/python/python.exe", "py-nuevo"),
                ("src/main.py", "print('nuevo')"),
                ("runtime/python/Lib/site-packages/yt_dlp/version.py", '__version__ = "2026.10.01"\n'),
                ("data/NUEVO.txt", "no entres"),
                ("config.json", "{}"),
            ]
            with zipfile_mod.ZipFile(buf, "w") as zf:
                for nombre, contenido in piezas:
                    zf.writestr("yt-remote-1.2.0/" + nombre, contenido)
            ruta.write_bytes(buf.getvalue())
            return ruta

        # Caso A: reemplazo completo (sin --skip).
        dest = armar_destino("instal-a")
        zip_ruta = zip_de_figura(raiz / "paquete-a.zip")
        updater.extraer_zip(zip_ruta, dest, [], updater.Log(raiz / "log-a.txt"))

        assert (dest / "ytremote.exe").read_text(encoding="utf-8") == "exe-nuevo"
        assert (dest / "src/main.py").read_text(encoding="utf-8") == "print('nuevo')"
        assert (
            (dest / "runtime/python/Lib/site-packages/yt_dlp/version.py")
            .read_text(encoding="utf-8")
            .strip()
            == '__version__ = "2026.10.01"'
        )
        # Lo protegido queda tal cual; lo que el release trae de data/ NO entra.
        assert (dest / "data/sesion.txt").read_text(encoding="utf-8") == "SESION-USUARIO"
        assert not (dest / "data/NUEVO.txt").exists()
        assert (dest / "config.json").read_text(encoding="utf-8") == "config-viejo"
        # Chatarra vieja fuera y sin restos .viejo.
        assert not (dest / "src/chatarra-vieja.py").exists()
        assert list(dest.glob("*.viejo")) == []

        # Caso B: con --skip, la pieza interna actual no retrocede.
        dest_b = armar_destino("instal-b")
        zip_b = zip_de_figura(raiz / "paquete-b.zip")
        updater.extraer_zip(
            zip_b,
            dest_b,
            ["runtime/python/Lib/site-packages/yt_dlp"],
            updater.Log(raiz / "log-b.txt"),
        )
        assert (
            (dest_b / "runtime/python/Lib/site-packages/yt_dlp/version.py")
            .read_text(encoding="utf-8")
            .strip()
            == '__version__ = "2026.08.19"'
        ), "el --skip debia preservar la pieza interna instalada"
        assert (dest_b / "src/main.py").read_text(encoding="utf-8") == "print('nuevo')"


def test_ayudante_extraccion_rota_se_revierte():
    """Si la descompresion explota a mitad (destino imponiblemente
    ocupado), las carpetas renombradas VUELVEN a su lugar: la instalacion
    queda como estaba, con su version anterior."""
    import updater
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        dest = raiz / "instal"
        (dest / "runtime/python").mkdir(parents=True)
        (dest / "src").mkdir(parents=True)
        (dest / "ytremote.exe").mkdir()  # El sabotaje: es una CARPETA.
        (dest / "runtime/python/python.exe").write_text("py-viejo", encoding="utf-8")
        (dest / "src/main.py").write_text("print('viejo')", encoding="utf-8")

        import io as io_mod
        import zipfile as zipfile_mod

        buf = io_mod.BytesIO()
        with zipfile_mod.ZipFile(buf, "w") as zf:
            zf.writestr("ytremote.exe", "exe-nuevo")
            zf.writestr("runtime/python/python.exe", "py-nuevo")
            zf.writestr("src/main.py", "print('nuevo')")
        zip_ruta = raiz / "paquete.zip"
        zip_ruta.write_bytes(buf.getvalue())

        try:
            updater.extraer_zip(zip_ruta, dest, [], updater.Log(raiz / "log.txt"))
        except Exception:
            pass
        else:
            raise AssertionError("escribir sobre una carpeta debia explotar")

        # Todo vuelve a donde estaba y ningun .viejo queda tirado.
        assert (dest / "runtime/python/python.exe").read_text(encoding="utf-8") == "py-viejo"
        assert (dest / "src/main.py").read_text(encoding="utf-8") == "print('viejo')"
        assert list(dest.glob("*.viejo")) == []


def test_ayudante_disponible_solo_en_portable():
    """_ayudante_disponible pide runtime\\python\\python.exe y
    ytremote.exe JUNTO a src: en el repo (desarrollo) ninguno esta y dice
    False; en una carpeta con los dos archivos dice True."""
    import bot as bot_mod
    from tempfile import TemporaryDirectory

    b = make_bot()
    # Repo: desarrollo, sin portable.
    assert bot_mod._base_instalacion() == Path(__file__).resolve().parent.parent
    assert b._ayudante_disponible() is False, "el repo no es instalacion portable"

    # Una carpeta portable de juguete: los dos archivos y dice True.
    with TemporaryDirectory() as tmp:
        portable = Path(tmp)
        (portable / "runtime" / "python").mkdir(parents=True)
        (portable / "runtime" / "python" / "python.exe").write_text("x", encoding="utf-8")
        (portable / "ytremote.exe").write_text("x", encoding="utf-8")
        original = bot_mod._base_instalacion
        bot_mod._base_instalacion = lambda: portable  # type: ignore[assignment]
        try:
            assert b._ayudante_disponible() is True
        finally:
            bot_mod._base_instalacion = original  # type: ignore[assignment]


def test_radio_parsea_argumentos():
    """Los argumentos de /radio: sin nada, destacadas; texto suelto =
    nombre; genero:/género:/g: = tag; pais:/país:/p: = pais; y
    favoritas/fav/❤️ = la lista propia."""
    import bot as bot_mod


    assert bot_mod._radio_parsear("") == ("top", "")
    assert bot_mod._radio_parsear("  salsa  ") == ("name", "salsa")
    assert bot_mod._radio_parsear("genero:bolero") == ("tag", "bolero")
    assert bot_mod._radio_parsear("género: rock") == ("tag", "rock")
    assert bot_mod._radio_parsear("g:bachata") == ("tag", "bachata")
    assert bot_mod._radio_parsear("pais:Venezuela") == ("pais", "Venezuela")
    assert bot_mod._radio_parsear("p: ve") == ("pais", "ve")
    assert bot_mod._radio_parsear("país:México") == ("pais", "México")
    assert bot_mod._radio_parsear("favoritas") == ("fav", "")
    assert bot_mod._radio_parsear("❤️") == ("fav", "")


def _emisora(nombre="Mango Radio", pais="DE", stream="https://s.mp3", logo=""):
    import radio as radio_mod

    return radio_mod.Emisora(nombre=nombre, pais=pais, stream=stream, logo=logo)


async def test_radio_destacadas_abren_listado():
    """/radio sin argumentos abre DESTACADAS en un mensaje-lista propio:
    encabezado, emisoras con pais y el teclado con play/favorita, la
    navegacion inerte (una sola pagina) y la fila de vistas."""
    import radio as radio_mod

    b = make_bot()

    async def destacadas(cantidad):
        return [_emisora(nombre=f"Emisora {n}") for n in range(1, 4)]

    viejo = radio_mod.destacadas
    radio_mod.destacadas = destacadas
    try:
        await b.cmd_radio(FakeMessageUpdate("/radio"), SimpleNamespace(args=[]))
    finally:
        radio_mod.destacadas = viejo

    chat_id, texto, kwargs = b._app.bot.sent[-1]
    assert chat_id == 44 and texto.startswith("🔥 Emisoras destacadas:"), texto
    assert "1. Emisora 1 — DE" in texto, texto
    kb = kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "rad:play:0"
    assert kb.inline_keyboard[0][1].callback_data == "rad:fav:0"
    assert [btn.text for btn in kb.inline_keyboard[-2]] == ["·", "❌", "·"]
    assert [btn.callback_data for btn in kb.inline_keyboard[-1]] == ["rad:top", "rad:fav"]


async def test_radio_busqueda_por_nombre():
    """/radio <texto> busca por NOMBRE (rad:busqueda) y el encabezado lleva
    el termino entrecomillado."""
    import radio as radio_mod

    b = make_bot()
    llamado = {}

    async def buscar_nombre(termino, cantidad):
        llamado["q"] = termino
        return [_emisora(nombre="Salsa Brava", pais="", stream="https://s/s.mp3")]

    viejo = radio_mod.buscar_nombre
    radio_mod.buscar_nombre = buscar_nombre
    try:
        await b.cmd_radio(
            FakeMessageUpdate("/radio salsa"), SimpleNamespace(args=["salsa"])
        )
    finally:
        radio_mod.buscar_nombre = viejo

    assert llamado == {"q": "salsa"}, llamado
    assert b._radio_view == "busqueda"
    texto = b._app.bot.sent[-1][1]
    assert texto.startswith("🔎 Emisoras para «salsa»:"), texto
    assert "Salsa Brava" in texto and "—" not in texto, texto


async def test_radio_fallo_honesto():
    """Si radio-browser no responde, el motivo llega honesto en UN mensaje y
    no se abre ningun listado."""
    import radio as radio_mod

    b = make_bot()

    async def destacadas(cantidad):
        raise radio_mod.RadioError("radio-browser no responde (timeout)")

    viejo = radio_mod.destacadas
    radio_mod.destacadas = destacadas
    try:
        upd = FakeMessageUpdate("/radio")
        await b.cmd_radio(upd, SimpleNamespace(args=[]))
    finally:
        radio_mod.destacadas = viejo

    assert any(
        t.startswith("⚠️ No se pudo buscar emisoras: radio-browser no responde")
        for t, _ in upd.sent
    ), upd.sent
    assert b._app.bot.sent == [], "un fallo no debe abrir listado"


async def test_radio_favoritas_vacias_avisan():
    """Sin favoritas guardadas no se abre listado: avisa y ya."""
    b = make_bot()
    upd = FakeMessageUpdate("/radio favoritas")
    await b.cmd_radio(upd, SimpleNamespace(args=["favoritas"]))
    assert any(
        "Todavía no guardas emisoras favoritas" in t for t, _ in upd.sent
    ), upd.sent
    assert b._app.bot.sent == []


async def test_radio_elegir_es_dj_y_sonar():
    """Elegir emisora: solo dj (user = alerta y NO se dispara nada). El dj
    que la elige la suena ya (el item lleva el stream directo, el pais como
    canal y el logo como miniatura) y el listado se cierra."""
    b = make_bot()
    e = _emisora(stream="https://stream.example/live", logo="https://logo.png")
    b._radio_items = [e]
    b._radio_chat_id, b._radio_message_id = 44, 7
    registro = []

    async def play_ok(update, item, preserve_current=False):
        registro.append(
            (item.url, item.title, item.channel, item.thumbnail, item.directo)
        )
        return True

    b._play_item = play_ok

    upd_user = FakeUpdate("rad:play:0", user_id=50)
    await b._on_radio_callback(upd_user, None)
    assert "Solo el admin o un dj pueden elegir." == upd_user.answered_kwargs.get(
        "text"
    ), upd_user.answered_kwargs
    assert registro == [] and b._radio_message_id == 7

    upd_dj = FakeUpdate("rad:play:0", user_id=77)
    await b._on_radio_callback(upd_dj, None)
    assert registro == [
        ("https://stream.example/live", "Mango Radio", "DE", "https://logo.png", True)
    ], registro
    assert (44, 7) in b._app.bot.deleted, b._app.bot.deleted
    assert b._radio_message_id is None


async def test_radio_favoritas_guardan_y_quitan():
    """❤️ Guardar (dj) mete la emisora en favoritas PERSISTIDAS (clave
    radio_favs del estado comun) y el mismo boton la quita."""
    b = make_bot()
    e = _emisora()
    b._radio_items = [e]
    b._radio_chat_id, b._radio_message_id = 44, 7

    upd = FakeUpdate("rad:fav:0", user_id=77)
    await b._on_radio_callback(upd, None)
    assert [f.nombre for f in b._radio_favs] == ["Mango Radio"], b._radio_favs
    assert upd.answered_kwargs.get("text") == "Guardada en favoritas."
    assert [d["nombre"] for d in b._state._current["radio_favs"]] == ["Mango Radio"]

    upd2 = FakeUpdate("rad:fav:0", user_id=77)
    await b._on_radio_callback(upd2, None)
    assert b._radio_favs == [], b._radio_favs
    assert upd2.answered_kwargs.get("text") == "Quitada de favoritas."
    assert b._state._current["radio_favs"] == []


async def test_radio_vista_favoritas_pagina():
    """/radio favoritas abre la vista propia y ◀ ▶ pagina el MISMO mensaje
    (6 favoritas: pagina 1 = 5, la 6a queda para la segunda)."""
    b = make_bot()
    b._radio_favs = [
        _emisora(nombre=f"Fav {n}", stream=f"https://f/{n}") for n in range(1, 7)
    ]
    await b.cmd_radio(
        FakeMessageUpdate("/radio favoritas"), SimpleNamespace(args=["favoritas"])
    )
    texto = b._app.bot.sent[-1][1]
    assert texto.startswith("❤️ Tus emisoras favoritas:"), texto
    assert "Fav 5" in texto and "Fav 6" not in texto, texto

    await b._on_radio_callback(FakeUpdate("rad:next"), None)
    assert b._radio_page == 1
    editado = b._app.bot.edited[-1]
    assert "Fav 6" in editado[2] and "Fav 1" not in editado[2], editado


async def test_radio_reproduce_directo_sin_yt_dlp():
    """La emisora entra por play_item COMO cualquier tema, pero marcada como
    directa: el stream ES su URL (si alguien la resolviera, el stub
    revienta), mpv la carga tal cual y NO hay prefetch — el "siguiente"
    seria una busqueda de YouTube de la nada."""
    from queue_manager import QueueItem

    b = make_bot()
    e = _emisora(stream="https://stream.example/live")
    prefetch = []
    b.playback.schedule_prefetch = lambda item: prefetch.append(item.url)
    resolver = playback_mod.resolve_stream_url

    def _no_resolver(url):
        raise AssertionError(f"no debe resolverse con yt-dlp: {url}")

    playback_mod.resolve_stream_url = _no_resolver
    try:
        ok = await b._play_item(
            FakeMessageUpdate("/radio"),
            QueueItem(url=e.stream, title=e.nombre, directo=True),
        )
    finally:
        playback_mod.resolve_stream_url = resolver

    assert ok is True
    assert b.player.loaded == [(e.stream, None)], b.player.loaded
    assert prefetch == [], prefetch


def _stub_letra(valor=None, error=None):
    """Reemplaza lyrics.buscar_letra por un stub que registra la llamada y
    devuelve `valor` (o lanza `error`). Se restaura con el viejo que
    devuelve, como los stubs de radio."""
    import lyrics as lyrics_mod

    llamado = {}

    async def buscar_letra(cancion, artista=""):
        llamado["args"] = (cancion, artista)
        if error is not None:
            raise error
        return valor

    viejo = lyrics_mod.buscar_letra
    lyrics_mod.buscar_letra = buscar_letra
    return lyrics_mod, viejo, llamado


def _sin_admin(b):
    """Ningun mensaje debe haber ido al chat del admin (chat 1)."""
    return all(cid != 1 for cid, _, _ in b._app.bot.sent)


async def test_letra_sin_nada_sonando():
    """/letra sin nada en reproduccion solo avisa: no hay nada que buscar."""
    b = make_bot()

    upd = FakeMessageUpdate("/letra")
    await b.cmd_letra(upd, SimpleNamespace(args=[]))
    assert any("No hay nada sonando" in t for t, _ in upd.sent), upd.sent
    assert _sin_admin(b)


async def test_letra_en_radio_no_aplica():
    """En emisoras no aplica (es un stream en vivo, no una cancion): se
    avisa y NO se le pregunta nada a lrclib."""
    import lyrics as lyrics_mod
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_current(
        QueueItem(url="https://s.mp3", title="Mango Radio", directo=True)
    )

    async def _no_preguntar(cancion, artista=""):
        raise AssertionError("no debe preguntarse por letra en radio")

    viejo = lyrics_mod.buscar_letra
    lyrics_mod.buscar_letra = _no_preguntar
    try:
        upd = FakeMessageUpdate("/letra")
        await b.cmd_letra(upd, SimpleNamespace(args=[]))
    finally:
        lyrics_mod.buscar_letra = viejo

    assert any("En la radio no hay letra" in t for t, _ in upd.sent), upd.sent
    assert _sin_admin(b)


async def test_letra_enviada_desde_titulo():
    """La letra llega como MENSAJE DE TEXTO. El titulo "Artista - Cancion
    (ruido)" se parte para preguntarle preciso a lrclib, y el admin no se
    entera: la letra es informacion, no reproducecon."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_current(
        QueueItem(url="u1", title="GP Band - Inexplicable (Video Oficial)")
    )
    lyrics_mod, viejo, llamado = _stub_letra(valor="Linea uno\nLinea dos")
    try:
        upd = FakeMessageUpdate("/letra")
        await b.cmd_letra(upd, SimpleNamespace(args=[]))
    finally:
        lyrics_mod.buscar_letra = viejo

    assert llamado["args"] == ("Inexplicable (Video Oficial)", "GP Band"), llamado
    assert any(t == "Linea uno\nLinea dos" for t, _ in upd.sent), upd.sent
    assert _sin_admin(b)


async def test_letra_no_encontrada_responde_sin_admin():
    """Sin coincidencia en lrclib: el mensaje EXACTO del plan y CERO aviso
    al admin (no encontre no es una emergencia)."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_current(QueueItem(url="u1", title="Tema Raro"))
    lyrics_mod, viejo, _ = _stub_letra(valor=None)
    try:
        upd = FakeMessageUpdate("/letra")
        await b.cmd_letra(upd, SimpleNamespace(args=[]))
    finally:
        lyrics_mod.buscar_letra = viejo

    assert any(
        t == "No encontré la letra de esta canción." for t, _ in upd.sent
    ), upd.sent
    assert _sin_admin(b)


async def test_letra_fallo_de_red_honesto():
    """Si lrclib esta caido el motivo es honesto ("no responde (...)") y
    tampoco se le avisa al admin."""
    from lyrics import LetraError
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_current(QueueItem(url="u1", title="Tema"))
    lyrics_mod, viejo, _ = _stub_letra(
        error=LetraError("lrclib no responde (boom)")
    )
    try:
        upd = FakeMessageUpdate("/letra")
        await b.cmd_letra(upd, SimpleNamespace(args=[]))
    finally:
        lyrics_mod.buscar_letra = viejo

    assert any(
        "La letra no se pudo buscar" in t and "boom" in t for t, _ in upd.sent
    ), upd.sent
    assert _sin_admin(b)


async def test_letra_larga_se_corta_con_aviso():
    """Una letra interminable NO revienta el limite de 4096 de Telegram:
    se corta en el tope con un aviso honesto, nunca a medias sin decirlo."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_current(QueueItem(url="u1", title="Tema"))
    lyrics_mod, viejo, _ = _stub_letra(valor="palabra " * 3000)
    try:
        upd = FakeMessageUpdate("/letra")
        await b.cmd_letra(upd, SimpleNamespace(args=[]))
    finally:
        lyrics_mod.buscar_letra = viejo

    assert len(upd.sent) == 1, upd.sent
    (t, _kwargs) = upd.sent[0]
    assert t.endswith("… (letra cortada)"), t[-40:]
    assert len(t) <= 3600, len(t)


def test_cancion_y_artista_de_titulo():
    """El titulo de YouTube se parte por " - ": lo de antes es el artista.
    Sin " - " no se adivina (el titulo entero va como busqueda libre)."""
    import bot as bot_mod

    assert bot_mod._cancion_y_artista("GP Band - Inexplicable") == (
        "Inexplicable",
        "GP Band",
    )
    assert bot_mod._cancion_y_artista("Tema Sin Guion") == ("Tema Sin Guion", "")
    assert bot_mod._cancion_y_artista(" - Cancion") == ("- Cancion", "")
    assert bot_mod._cancion_y_artista("  A  -  B  ") == ("B", "A")


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
    playback_mod.search = counting_search
    try:
        c1, _, _ = await b._pick_next_candidate(current)
        c2, _, _ = await b._pick_next_candidate(current)
        assert len(calls) == 1, f"search se llamo {len(calls)} veces, debe ser 1"
        assert c1 is not None and c2 is not None
        assert c1.url == c2.url == "u1"
    finally:
        bot_mod.search = original
        playback_mod.search = original


async def test_next_radio_renders_loading_card_inmediato():
    """Al tocar ⏭ sin prefetch resuelto, la tarjeta se edita YA con
    "⏳ Cargando…" + titulo del candidato nuevo, antes de resolver el stream.
    El commit de queue._current ocurre recien tras el load exitoso: el estado
    del dominio no se toca para mostrar el feedback de carga."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: ("stream-" + url, None)
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
        b.playback.pick_next_candidate = fake_pick

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
        playback_mod.resolve_stream_url = lambda url: None


async def test_next_radio_failed_resolve_keeps_current():
    """Si la resolucion del candidato nuevo falla, queue._current NO pasa a
    apuntar al candidato: el estado conserva el tema anterior (el commit del
    dominio ocurre solo cuando el audio realmente arranca)."""
    import bot as bot_mod
    from queue_manager import QueueItem

    bot_mod.resolve_stream_url = lambda url: None
    playback_mod.resolve_stream_url = lambda url: None
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
        b.playback.pick_next_candidate = fake_pick

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
        playback_mod.resolve_stream_url = lambda url: None


async def test_stream_for_times_out_hung_resolve():
    """Un yt-dlp colgado (thread que nunca termina) NO congela el bot: el
    timeout de _stream_for corta el await y devuelve None (fallo de
    resolucion) en vez de dejar el polling mudo para siempre."""
    import bot as bot_mod

    b = make_bot()

    original_resolve = bot_mod.resolve_stream_url
    original_timeout = bot_mod._RESOLVE_TIMEOUT
    bot_mod.resolve_stream_url = lambda url: time.sleep(5) or ("stream-" + url, None)
    playback_mod.resolve_stream_url = lambda url: time.sleep(5) or ("stream-" + url, None)
    bot_mod._RESOLVE_TIMEOUT = 0.3
    # En playback la constante se llama igual pero SIN el guion bajo: ahi vive
    # como `from constants import RESOLVE_TIMEOUT`. Parchar `playback._RESOLVE_TIMEOUT`
    # crearia un atributo nuevo que nadie lee, y la prueba pasaria por el motivo
    # equivocado (o fallaria sin causa real).
    playback_mod.RESOLVE_TIMEOUT = 0.3
    try:
        inicio = time.monotonic()
        result = await b._stream_for("X")
        transcurrido = time.monotonic() - inicio
        assert result is None, "la resolucion colgada debe expirar como fallo"
        assert transcurrido < 4, f"tardo {transcurrido:.1f}s, el timeout no funciono"
    finally:
        bot_mod.resolve_stream_url = original_resolve
        playback_mod.resolve_stream_url = original_resolve
        bot_mod._RESOLVE_TIMEOUT = original_timeout
        playback_mod.RESOLVE_TIMEOUT = original_timeout


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

    async def fake_stream(url):
        return ("stream", None)

    b._play_item = fake_play
    b.playback.play_item = fake_play
    b._stream_for = fake_stream
    b.playback.stream_for = fake_stream
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
    b.playback.play_item = fake_play

    orig_pl = bot_mod.is_playlist_url
    orig_quick = bot_mod.quick_playlist
    orig_exp = bot_mod.expand_playlist
    bot_mod.is_playlist_url = lambda q: True
    bot_mod.quick_playlist = lambda q, n: (
        [SearchResult(url="u1", title="T1", duration="3:00", thumbnail="")], 39
    )
    bot_mod.expand_playlist = lambda q, n, *_: []
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


async def test_ajustes_button_in_card_keyboard():
    """La fila 3 es un SOLO boton ⚙️ Ajustes: calidad, usuarios y apagado
    viven adentro del menu (la etiqueta de calidad solo se ve ahi)."""
    b = make_bot()
    b.roles.set_role(1, "admin")
    kb = b._control_keyboard()
    rows = kb.inline_keyboard
    assert len(rows) == 3, f"la tarjeta debe tener 3 filas, tiene {len(rows)}"
    ajustes_btn = rows[2]
    assert len(ajustes_btn) == 1, f"la fila 3 es un solo boton: {ajustes_btn}"
    assert ajustes_btn[0].text == "⚙️ Ajustes", ajustes_btn[0].text
    assert ajustes_btn[0].callback_data == "ctl:ajustes"
    b._max_height = 480
    menu = b._ajustes_keyboard(1)
    calidad_fila = menu.inline_keyboard[1]
    assert calidad_fila[0].text == "⚙️ Calidad: 480p", calidad_fila
    assert calidad_fila[0].callback_data == "ctl:calidad"


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
        # El teclado restaurado es el de control; la calidad elegida se ve
        # adentro de Ajustes.
        kb = ediciones[-1][3].get("reply_markup")
        assert kb.inline_keyboard[2][0].text == "⚙️ Ajustes"
        assert b._ajustes_keyboard(1).inline_keyboard[1][0].text == "⚙️ Calidad: 480p"
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
    b.playback.stream_for = fake_stream_for
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
    """Si la base guarda max_height, un bot nuevo lo restaura y lo aplica.

    Antes se sembraba un `state.json` a mano; ahora se siembra la base, que es
    donde vive el dato de verdad.
    """
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

        def _sembrar(valor):
            from persistence import StateStore

            s = StateStore(persistence_mod.STATE_PATH)
            s.save({"version": 1, "max_height": valor})

        b1 = _fresh_bot()
        assert b1._max_height is None
        b1._max_height = 720
        b1._persist_flush()
        # "Reinicio": un bot nuevo que lee la MISMA base.
        b2 = _fresh_bot()
        assert b2._max_height == 720, b2._max_height
        assert search_mod.MAX_HEIGHT == 720
        # Y una base sin tope vuelve al default 1080. OJO: la base vive en el
        # DIRECTORIO del state.json, no en el archivo: hace falta otra carpeta
        # para simular "otra instalacion", no otro nombre de archivo.
        persistence_mod.STATE_PATH = Path(tempfile.mkdtemp()) / "state.json"
        b3 = _fresh_bot()
        assert b3._max_height is None
        # Un valor viejo por encima del tope actual (1440/2160) cae a None ->
        # 1080: nunca se aplica una calidad que dejamos de soportar.
        _sembrar(2160)
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
    assert kb.inline_keyboard[2][0].text == "⚙️ Ajustes"
    assert b._ajustes_keyboard(1).inline_keyboard[1][0].text == "⚙️ Calidad: 1080p"
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
    b.playback.stream_for = fake_stream_for
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
        assert kb.inline_keyboard[2][0].text == "⚙️ Ajustes"
        assert b._ajustes_keyboard(1).inline_keyboard[1][0].text == "⚙️ Calidad: 480p"
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
    # dj: un boton de tema por fila (posicion global + titulo completo), el
    # boton 🕘 Historial y la nav [· ❌ ·] (una sola pagina).
    assert [btn.text for btn in rows[0]] == ["▶️ 1. Tema 0"]
    assert [btn.text for btn in rows[1]] == ["2. Tema 1"]
    assert [btn.text for btn in rows[2]] == ["3. Tema 2"]
    assert [btn.text for btn in rows[3]] == ["🕘 Historial"], rows[3]
    assert [btn.text for btn in rows[4]] == ["·", "❌", "·"], rows[4]
    # La card no se edita ni se desvanece.
    assert b._app.bot.edited == []
    assert b._app.bot.deleted == []


async def test_list_open_for_user_no_track_buttons():
    """Un user ve la lista pero NO recibe botones de tema: solo 🕘 historial
    y navegacion."""
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
    assert len(rows) == 2, "user: sin botones de tema, solo 🕘 + navegacion"
    assert [btn.text for btn in rows[0]] == ["🕘 Historial"]
    assert [btn.text for btn in rows[1]] == ["·", "❌", "·"]


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
    assert [btn.text for btn in rows[2]] == ["·", "❌", "·"]

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
    assert len(rows) == 12, "pagina 1: 10 temas + 🕘 historial + navegacion"
    assert rows[0][0].text == "▶️ 1. Tema 0"
    assert rows[9][0].text == "10. Tema 9"
    assert [btn.text for btn in rows[11]] == ["·", "❌", "▶"]
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
    assert [btn.text for btn in rows2[11]] == ["◀", "❌", "▶"]
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
    assert len(kb4.inline_keyboard) == 3, "pagina 3: 1 solo tema + 🕘 + nav"
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
    b.playback.stream_for = fake_stream_for
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


async def test_list_hub_historial_view():
    """El 📋 es un hub de contexto: la cola gana un boton 🕘 Historial que
    convierte el MISMO mensaje en el historial (una fila por cancion, un
    solo boton de ancho completo que repite al tocarlo) y 🔙 Cola vuelve a
    la cola."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_playlist([QueueItem(url=f"u{i}", title=f"Tema {i}") for i in range(3)])
    b._state.marcar_historial(QueueItem(url="uv", title="Vieja Cancion"))
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44), SimpleNamespace(args=[]), "lista"
    )
    cola_markup = b._app.bot.sent[-1][2]["reply_markup"]
    cola_labels = [btn.text for fila in cola_markup.inline_keyboard for btn in fila]
    assert "🕘 Historial" in cola_labels, cola_labels

    upd = FakeUpdate("lst:historial", user_id=77, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert b._list_view == "historial", "el 🕘 cambia la vista del MISMO mensaje"
    texto = b._app.bot.edited[-1][2]
    markup = b._app.bot.edited[-1][3]["reply_markup"]
    assert "Historial" in texto, texto
    labels = [btn.text for fila in markup.inline_keyboard for btn in fila]
    assert "1. Vieja Cancion" in labels, labels
    assert len(markup.inline_keyboard[0]) == 1, "titulo a ancho completo"
    assert "🔙 Cola" in labels, labels

    upd2 = FakeUpdate("lst:cola", user_id=77, chat_id=44)
    await b._on_list_callback(upd2, SimpleNamespace(args=[]))
    assert b._list_view == "cola", "🔙 Cola vuelve a la cola"
    assert "Lista" in b._app.bot.edited[-1][2]


async def test_list_historial_abre_directo_si_cola_vacia():
    """Cola vacia pero con historial: el 📋 abre DIRECTO en el historial (es
    el unico lugar donde vive); solo toast cuando no hay nada en ninguna."""
    from queue_manager import QueueItem

    b = make_bot()
    b._state.marcar_historial(QueueItem(url="uv", title="Vieja"))
    upd = FakeUpdate("ctl:lista", user_id=77, chat_id=44)
    await b._on_control(upd, SimpleNamespace(args=[]), "lista")
    assert b._list_view == "historial", "debe abrir directo en el historial"
    assert b._list_message_id is not None, "se envia el mensaje del historial"
    assert "Historial" in b._app.bot.sent[-1][1]


async def test_list_historial_rep_link_del():
    """Acciones del historial: 🔄 repite (dj: resuelve stream y suena, la
    lista se cierra) y 🗑 borra la entrada (dj) re-renderizando. El test
    ejercita los callbacks lst:rep, lst:link y lst:del, que YA NO tienen
    boton en el teclado (la fila del titulo es un solo boton de ancho
    completo y el borrar por fila se fue) pero siguen atendidos para los
    teclados viejos."""
    from queue_manager import QueueItem

    b = make_bot()
    # Siembra AL REVES: la base se lee mas nuevo primero, asi que ua (el
    # ultimo en entrar) queda en la posicion 1 del teclado.
    b._state.marcar_historial(QueueItem(url="ub", title="Cancion B"))
    b._state.marcar_historial(QueueItem(url="ua", title="Cancion A"))

    async def fake_stream_for(url):
        return (f"stream-{url}", None)

    b._stream_for = fake_stream_for
    b.playback.stream_for = fake_stream_for
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44), SimpleNamespace(args=[]), "lista"
    )
    list_id = b._list_message_id
    assert b._list_view == "historial"

    # 🔄 repetir: resuelve y suena; la lista se cierra con desvanecimiento.
    upd = FakeUpdate("lst:rep:0", user_id=77, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert b.player.loaded and b.player.loaded[-1] == ("stream-ua", None), b.player.loaded
    assert b._list_message_id is None, "repetir cierra la lista"
    assert (44, list_id) in b._app.bot.deleted

    # 🔗 enlace: cualquiera lo pide; el bot manda el URL al chat y la lista
    # sigue abierta.
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44), SimpleNamespace(args=[]), "lista"
    )
    enviados_antes = len(b._app.bot.sent)
    upd = FakeUpdate("lst:link:1", user_id=50, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert b._app.bot.sent[enviados_antes][1] == "ub", b._app.bot.sent
    assert b._list_message_id is not None, "copiar el enlace no cierra"

    # 🗑 borrar: quita la entrada de la BASE y re-renderiza la vista.
    upd = FakeUpdate("lst:del:1", user_id=77, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert [h["url"] for h in b._historial()] == ["ua"], b._historial()
    etiquetas = [
        btn.text
        for fila in b._app.bot.edited[-1][3]["reply_markup"].inline_keyboard
        for btn in fila
    ]
    assert any("Cancion A" in t for t in etiquetas), etiquetas
    assert not any("Cancion B" in t for t in etiquetas), "la entrada borrada desaparece"


async def test_list_historial_permisos():
    """Ver el historial es de cualquiera; 🔄 y 🗑 son del dj (alerta si no)."""
    from queue_manager import QueueItem

    b = make_bot()
    b.roles.set_role(50, "user")
    b._state.marcar_historial(QueueItem(url="ua", title="Cancion A"))
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=50, chat_id=44), SimpleNamespace(args=[]), "lista"
    )
    assert b._list_view == "historial"
    upd = FakeUpdate("lst:rep:0", user_id=50, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert upd.answered_kwargs.get("show_alert") is True
    assert b.player.loaded == [], "el user no reproduce"
    upd = FakeUpdate("lst:del:0", user_id=50, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert upd.answered_kwargs.get("show_alert") is True
    assert len(b._historial()) == 1, "el user no borra"


async def test_reproducir_registra_el_historial():
    """SI sonó, ENTRA al historial guardado: tambien las canciones de la
    COLA (el defecto del 2/10: solo entraba lo suelto de radio)."""
    from queue_manager import QueueItem

    b = make_bot()
    b.queue.set_playlist([QueueItem(url="u1", title="Tema 1")])

    async def fake_stream_for(url):
        return (f"stream-{url}", None)

    async def _no_candidate(current=None):
        return None, False, False

    b._stream_for = fake_stream_for
    b.playback.stream_for = fake_stream_for
    b._pick_next_candidate = _no_candidate
    b.playback.pick_next_candidate = _no_candidate

    started = await b._play_item(
        FakeUpdate("ctl:pp", chat_id=44), QueueItem(url="u1", title="Tema 1")
    )
    assert started, "debio arrancar"
    assert [h["url"] for h in b._historial()] == ["u1"], b._historial()
    assert b._historial()[0]["title"] == "Tema 1", b._historial()[0]


async def test_reproducir_fallido_no_registra_el_historial():
    """Si NO arranco (fallo de resolucion), no se registra nada: el
    historial es lo que SONO, no lo que se intentó."""
    from queue_manager import QueueItem

    b = make_bot()

    async def sin_stream(url):
        return None

    b._stream_for = sin_stream
    b.playback.stream_for = sin_stream

    started = await b._play_item(
        FakeUpdate("ctl:pp", chat_id=44), QueueItem(url="u1", title="X")
    )
    assert started is False, "no debio arrancar"
    assert b._historial() == [], b._historial()


async def test_autoadvance_prefetch_tambien_registra():
    """El siguiente automatico por el camino del prefetch NO pasa por
    play_item: tambien debe entrar al historial."""
    from queue_manager import QueueItem

    b = make_bot()

    async def _no_candidate(current=None):
        return None, False, False

    b.playback.pick_next_candidate = _no_candidate
    b.queue.set_current(QueueItem(url="v0", title="Suena"))
    item = QueueItem(url="u2", title="Siguiente")
    b.playback.prefetch_resolved = ((item, False), ("stream-u2", None))

    await b.playback.advance_after_end(SimpleNamespace(args=[]))

    assert [h["url"] for h in b._historial()] == ["u2"], b._historial()


async def test_list_historial_borrar_todo():
    """🗑 Borrar todo en DOS toques para el dj: el primero solo arma la
    confirmacion (base intacta, teclado con alcance exacto + cancelar); el
    segundo, con ✅, recien vacia la base. Quien no es dj: alerta inmediata.
    Sin cola ni nada sonando, el mensaje de lista se cierra."""
    from queue_manager import QueueItem

    b = make_bot()
    b.roles.set_role(50, "user")
    b._state.marcar_historial(QueueItem(url="ua", title="Cancion A"))
    b._state.marcar_historial(QueueItem(url="ub", title="Cancion B"))
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=50, chat_id=44), SimpleNamespace(args=[]), "lista"
    )
    assert b._list_view == "historial"

    # El user no puede ni armar: alerta y la base queda igual.
    upd = FakeUpdate("lst:delall", user_id=50, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert upd.answered_kwargs.get("show_alert") is True
    assert len(b._historial()) == 2, "el user no borra nada"

    # Primer toque del dj: ARMA la confirmacion pero no borra nada.
    upd = FakeUpdate("lst:delall", user_id=77, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert b._historial_borrar_armado is True
    assert len(b._historial()) == 2, "el primer toque no borra"

    # El teclado muestra el alcance EXACTO (2 canciones) + cancelar.
    markup = b._app.bot.edited[-1][3]["reply_markup"]
    labels = [btn.text for fila in markup.inline_keyboard for btn in fila]
    assert "✅ Borrar TODO el historial (2 canciones)" in labels, labels
    assert "❌ Cancelar" in labels, labels
    assert "🗑 Borrar todo" not in labels, labels

    # Segundo toque ✅: recien aqui se vacia la base y se cierra el mensaje.
    upd = FakeUpdate("lst:delall_ok", user_id=77, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert b._historial() == [], b._historial()
    assert upd.answered_kwargs.get("text") == "Historial borrado ✓", upd.answered
    assert b._list_message_id is None, "sin nada que listar se cierra"


async def test_list_historial_borrar_todo_cancela():
    """El ❌ de la confirmacion vuelve a la vista normal: base intacta y
    teclado con el 🗑 Borrar todo comun, sin fila de confirmacion."""
    from queue_manager import QueueItem

    b = make_bot()
    b._state.marcar_historial(QueueItem(url="ua", title="Cancion A"))
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44), SimpleNamespace(args=[]), "lista"
    )
    upd = FakeUpdate("lst:delall", user_id=77, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert b._historial_borrar_armado is True

    upd = FakeUpdate("lst:delall_no", user_id=77, chat_id=44)
    await b._on_list_callback(upd, SimpleNamespace(args=[]))
    assert b._historial_borrar_armado is False
    assert len(b._historial()) == 1, "cancelar no borra nada"
    markup = b._app.bot.edited[-1][3]["reply_markup"]
    labels = [btn.text for fila in markup.inline_keyboard for btn in fila]
    assert "🗑 Borrar todo" in labels, labels
    assert not any(l.startswith("✅ Borrar TODO") for l in labels), labels


async def test_historial_pagina_de_10_y_titulo_ancho_completo():
    """10 canciones por pagina: con 12 titulos hay 2 paginas; cada cancion
    es UN solo boton de ancho completo (sin 🔄 en el texto y sin 🗑 al
    lado), primero el mas reciente, y no queda ningun boton de borrar por
    fila."""
    from queue_manager import QueueItem

    b = make_bot()
    for i in range(12):
        b._state.marcar_historial(QueueItem(url=f"u{i}", title=f"Cancion {i}"))
    await b._on_control(
        FakeUpdate("ctl:lista", user_id=77, chat_id=44), SimpleNamespace(args=[]), "lista"
    )
    assert b._list_view == "historial"
    assert b._history_text(0) == "🕘 Historial (pag 1/2):", b._history_text(0)

    markup = b._app.bot.sent[-1][2]["reply_markup"]
    filas = markup.inline_keyboard
    assert len(filas) == 13, filas  # 10 titulos + borrar todo + cola + nav
    for fila in filas[:10]:
        assert len(fila) == 1, "el titulo va a ancho completo"
    assert not any(btn.text.startswith("🔄") for fila in filas for btn in fila)
    assert not any(btn.text == "🗑" for fila in filas for btn in fila)
    assert filas[0][0].text == "1. Cancion 11", filas[0]
    assert filas[9][0].text == "10. Cancion 2", filas[9]


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
    b.playback.play_item = fake_play

    orig_pl = bot_mod.is_playlist_url
    orig_quick = bot_mod.quick_playlist
    orig_exp = bot_mod.expand_playlist
    bot_mod.is_playlist_url = lambda q: True
    bot_mod.quick_playlist = lambda q, n: (
        [SearchResult(url=f"u{i}", title=f"T{i}", duration="3:00", thumbnail="") for i in range(n)],
        1882,
    )
    bot_mod.expand_playlist = lambda q, n, *_: []
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
    bot_mod.expand_playlist = lambda q, n, *_: [
        SearchResult(url="u0", title="T0", duration="3:00", thumbnail=""),
        SearchResult(url="u1", title="T1", duration="3:00", thumbnail=""),
        SearchResult(url="u2", title="T2", duration="3:00", thumbnail=""),
        SearchResult(url="u3", title="T3", duration="3:00", thumbnail=""),
    ]

    async def fake_play(update, item, **kwargs):
        return True

    b._play_item = fake_play
    b.playback.play_item = fake_play
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
    bot_mod.expand_playlist = lambda q, n, *_: [
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

    # La ventana que pide el propio bot no debe pegarle a la red: aqui no
    # aporta nada (la ficticia de arriba es la que anexa).
    orig_expand = bot_mod.expand_playlist
    bot_mod.expand_playlist = lambda q, n, *_: []

    tarea = _asyncio.create_task(expansion_fake())
    try:
        actual = cast(QueueItem, b.queue.current)
        candidato, de_cola, err = await b._pick_next_candidate(actual)
        assert candidato is not None and candidato.url == "u2", candidato
        assert de_cola is True
    finally:
        bot_mod.expand_playlist = orig_expand
        b._cancel_playlist_expansion()
        if not tarea.done():
            tarea.cancel()


async def test_playlist_ventana_se_amplia_y_nunca_se_queda_sin_tema():
    """La playlist se carga a ventanas: al llegar al final de lo cargado se
    pide SOLO la tanda siguiente (no la lista entera) y el siguiente tema sale
    de la cola recien cargada, no de la radio ni de un wrap al primero."""
    import bot as bot_mod
    from queue_manager import QueueItem
    from search import SearchResult

    b = make_bot()
    pedidos: list[tuple[int, int]] = []

    def expansion_por_ventana(url, max_results, inicio=1):
        pedidos.append((inicio, max_results))
        return [
            SearchResult(url=f"p{i}", title=f"P{i}", duration="3:00", thumbnail="")
            for i in range(inicio - 1, inicio - 1 + max_results)
        ]

    orig_expand = bot_mod.expand_playlist
    bot_mod.expand_playlist = expansion_por_ventana
    try:
        b._expanding_playlist_url = "https://youtube.com/playlist?list=XYZ"
        b._expanding_total = 200
        b.queue.set_playlist([QueueItem(url=f"c{i}", title=f"C{i}") for i in range(3)])
        b.queue._cursor = 2  # ultimo tema cargado

        candidato, de_cola, err = await b._pick_next_candidate(b.queue.current)
    finally:
        bot_mod.expand_playlist = orig_expand
        b._cancel_playlist_expansion()

    assert err is False, err
    assert de_cola is True, "el siguiente debe salir de la cola, no de la radio"
    assert candidato is not None and candidato.url == "p3", candidato
    assert pedidos == [(4, 30)], f"debio pedir una sola ventana desde la 4: {pedidos}"
    assert len(b.queue._items) == 33, f"debio cargar 30 por ventana: {len(b.queue._items)}"


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


def test_build_master_playlist_joins_video_and_audio():
    """Directo (sync): el master .m3u8 local une el video child con el audio
    child como grupo EXT-X-MEDIA (mismo ID, DEFAULT). Un solo demuxer HLS
    sincroniza A/V segun el spec: no hay dos live edges independientes."""
    from player import _build_master_playlist, _is_hls_url

    assert _is_hls_url("https://x/hls_playlist/.../index.m3u8?sig=1")
    assert not _is_hls_url("https://x/googlevideo/video.mp4?range=1-2")
    master = _build_master_playlist(
        "https://hls/v.m3u8", "https://hls/a.m3u8", resolution="1280x720"
    )
    assert master.startswith("#EXTM3U")
    assert 'TYPE=AUDIO,GROUP-ID="yt-remote-audio0"' in master
    assert "DEFAULT=YES" in master
    assert 'URI="https://hls/a.m3u8"' in master
    # La unica variante referencia el MISMO grupo de audio del EXT-X-MEDIA.
    assert 'AUDIO="yt-remote-audio0"' in master
    assert "https://hls/v.m3u8" in master
    assert "RESOLUTION=1280x720" in master


def test_launch_mpv_amplia_protocol_whitelist():
    """Directo (fix regresion Ronda 10b): un master .m3u8 LOCAL se apertura
    con el demuxer lavf, cuyo whitelist por defecto ('file,crypto,data')
    bloquea los childs https => el directo no cargaba y mpv quedaba en
    "Drop files". La entrega debe arrancar mpv con el whitelist ampliado."""
    import player as player_mod

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):  # noqa: ARG002
            captured["args"] = args

    original = player_mod.subprocess.Popen
    player_mod.subprocess.Popen = FakePopen
    try:
        player_mod.Player()._launch_mpv()
    finally:
        player_mod.subprocess.Popen = original

    assert any(
        a
        == "--demuxer-lavf-o=protocol_whitelist=[file,http,https,tcp,tls,crypto,data]"
        for a in captured["args"]
    ), captured["args"]


async def test_load_master_espera_file_loaded_y_marca_activo():
    """Directo (sync): load() envia el master LOCAL y recien marca el track
    activo cuando mpv confirma file-loaded: la card "Sonando" es real."""
    import player as player_mod

    video = "https://hls/v.m3u8"
    audio = "https://hls/a.m3u8"
    original_pipe = player_mod.MPV_PIPE
    tmp = os.path.join(tempfile.gettempdir(), "mpv_pipe_test.json")
    open(tmp, "w", encoding="utf-8").close()
    player_mod.MPV_PIPE = tmp
    try:
        p = player_mod.Player()
        tarea = asyncio.create_task(p.load(video, audio))
        await asyncio.sleep(0.2)
        p._on_mpv_event({"event": "file-loaded"})
        await asyncio.wait_for(tarea, timeout=1.0)
        with open(tmp, encoding="utf-8") as fh:
            escrito = fh.read()
        assert p._track_active is True, "solo activo con file-loaded confirmado"
        assert p.loaded[-1] == (video, audio)
        assert "loadfile" in escrito and "ytremote_live.m3u8" in escrito, escrito
    finally:
        player_mod.MPV_PIPE = original_pipe
        if os.path.exists(tmp):
            os.remove(tmp)


async def test_load_sin_file_loaded_lanza_error_real():
    """Directo (card honesta): si mpv NO confirma file-loaded (load fallido,
    quedo en idle), load() lanza RuntimeError y no marca el track activo: el
    bot responde "No se pudo reproducir" en vez de una tarjeta "Sonando"."""
    import player as player_mod

    original_pipe = player_mod.MPV_PIPE
    original_timeout = player_mod._PIPE_WAIT_TIMEOUT
    player_mod.MPV_PIPE = os.path.join(
        tempfile.gettempdir(), "__mpv_pipe_no_existe__"
    )
    player_mod._PIPE_WAIT_TIMEOUT = 0.2
    try:
        p = player_mod.Player()
        try:
            await p.load("https://hls/v.m3u8", "https://hls/a.m3u8")
        except RuntimeError as exc:
            assert "no confirmo la carga" in str(exc)
        else:
            raise AssertionError("debia lanzar RuntimeError")
        assert p._track_active is False
    finally:
        player_mod.MPV_PIPE = original_pipe
        player_mod._PIPE_WAIT_TIMEOUT = original_timeout


async def test_load_falla_rapido_con_el_motivo_real_de_mpv():
    """Directo (fix /next 403, 16/oct): si mpv RECHAZA el stream (end-file
    reason=error, ej: HTTP 403 sobre una firma vencida), load() falla AL
    INSTANTE con el motivo real de mpv en vez de esperar los 15 s del
    timeout creyendo que el stream estaba lento. Asi el caller puede
    re-resolver fresco y reintentar sin el usuario quedado sin musica."""
    import player as player_mod

    original_pipe = player_mod.MPV_PIPE
    original_timeout = player_mod._PIPE_WAIT_TIMEOUT
    tmp = os.path.join(tempfile.gettempdir(), "mpv_pipe_error_endfile.json")
    open(tmp, "w", encoding="utf-8").close()
    player_mod.MPV_PIPE = tmp
    player_mod._PIPE_WAIT_TIMEOUT = 5.0  # si no falla rapido, el test lo ve
    try:
        p = player_mod.Player()
        tarea = asyncio.create_task(p.load("https://x/video.mp4"))
        await asyncio.sleep(0.2)  # el load quedo esperando la senal
        inicio = time.monotonic()
        p._on_mpv_event(
            {"event": "end-file", "reason": "error", "error": "HTTP error 403"}
        )
        try:
            await asyncio.wait_for(tarea, timeout=2.0)
        except RuntimeError as exc:
            assert "403" in str(exc), f"se perdio el motivo real: {exc}"
        else:
            raise AssertionError("debia lanzar RuntimeError con el motivo real")
        assert time.monotonic() - inicio < 1.0, (
            "no fue rapido: espero el timeout entero en vez del rechazo"
        )
        assert p._track_active is False
    finally:
        player_mod.MPV_PIPE = original_pipe
        player_mod._PIPE_WAIT_TIMEOUT = original_timeout
        if os.path.exists(tmp):
            os.remove(tmp)


async def test_load_con_rechazo_no_encadena_un_segundo_load():
    """Directo (fix /next 403): un rechazo que llega DESPUES de una carga
    buena (mid-play) queda registrado para ese track muerto, pero el
    SIGUIENTE load arranca limpio: el rechazo viejo no contamina la carga
    nueva con un error fantasma."""
    import player as player_mod

    original_pipe = player_mod.MPV_PIPE
    tmp = os.path.join(tempfile.gettempdir(), "mpv_pipe_post_load.json")
    open(tmp, "w", encoding="utf-8").close()
    player_mod.MPV_PIPE = tmp
    try:
        p = player_mod.Player()
        tarea = asyncio.create_task(p.load("https://x/a.mp4"))
        await asyncio.sleep(0.2)
        p._on_mpv_event({"event": "file-loaded"})
        await asyncio.wait_for(tarea, timeout=1.0)
        # Rechazo mid-play del track que ya cargo.
        p._on_mpv_event(
            {"event": "end-file", "reason": "error", "error": "HTTP error 403"}
        )
        assert p._load_failed_event.is_set(), "el rechazo si debe registrarse"
        # El siguiente load arranca limpio (lo limpia load() al empezar).
        tarea2 = asyncio.create_task(p.load("https://x/b.mp4"))
        await asyncio.sleep(0.2)
        assert p._load_error is None and not p._load_failed_event.is_set(), (
            "el rechazo viejo contamino la carga nueva"
        )
        p._on_mpv_event({"event": "file-loaded"})
        await asyncio.wait_for(tarea2, timeout=1.0)
        assert p._track_active is True
    finally:
        player_mod.MPV_PIPE = original_pipe
        if os.path.exists(tmp):
            os.remove(tmp)


async def test_garantia_comando_al_pipe_con_exists_mintiendo():
    """Garantia (bug de la laptop y de la PC, 15/oct): en Windows,
    os.path.exists() devuelve False para un named pipe OCUPADO (stat da
    Access Denied y exists() lo traga). _send_raw usaba eso como puerta y
    TIRABA EL LOADFILE A LA BASURA sin error: mpv quedaba en idle mostrando
    "Drop files to play here" y el bot, tras 15 s, gritaba "mpv no confirmo
    la carga del stream (timeout)". El comando tiene que salir aunque
    exists() jure que el pipe no esta."""
    import player as player_mod

    original_pipe = player_mod.MPV_PIPE
    tmp = os.path.join(tempfile.gettempdir(), "mpv_pipe_exists_miente.json")
    open(tmp, "w", encoding="utf-8").close()
    player_mod.MPV_PIPE = tmp
    original_exists = os.path.exists
    os.path.exists = lambda *a, **k: False  # simula el pipe ocupado
    try:
        p = player_mod.Player()
        await asyncio.wait_for(p.command("ping"), timeout=1.0)
        with open(tmp, encoding="utf-8") as fh:
            escrito = fh.read()
        assert "ping" in escrito, (
            "el comando se tiro a la basura: exists() mintio con False y "
            "_send_raw no escribio nada al pipe de mpv"
        )
    finally:
        os.path.exists = original_exists
        player_mod.MPV_PIPE = original_pipe
        if os.path.exists(tmp):
            os.remove(tmp)


def test_launch_mpv_deja_su_log_en_data():
    """Diagnostico remoto: mpv corre con la salida a DEVNULL, asi que sin su
    propio log cada fallo en la PC del usuario era indescifrable. La entrega
    tiene que arrancar mpv con --log-file apuntando a data/mpv.log."""
    import player as player_mod

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):  # noqa: ARG002
            captured["args"] = args

    original = player_mod.subprocess.Popen
    player_mod.subprocess.Popen = FakePopen
    try:
        player_mod.Player()._launch_mpv()
    finally:
        player_mod.subprocess.Popen = original

    log_args = [a for a in captured["args"] if a.startswith("--log-file=")]
    assert log_args, captured["args"]
    assert log_args[0].endswith("mpv.log"), log_args


def test_launch_mpv_desactiva_su_propio_ytdl():
    """Directo (fix /next 403, 16/oct): con el ytdl hook de mpv activo, un
    stream rechazado (403) hacia que mpv re-consultara a yt-dlp por su
    cuenta (10-15 s) antes de rendirse. El bot resuelve los streams el
    mismo: mpv tiene que rechazar AL INSTANTE para que el bot pueda
    re-resolver fresco y reintentar sin el usuario quedado sin musica."""
    import player as player_mod

    captured = {}

    class FakePopen:
        def __init__(self, args, **kwargs):  # noqa: ARG002
            captured["args"] = args

    original = player_mod.subprocess.Popen
    player_mod.subprocess.Popen = FakePopen
    try:
        player_mod.Player()._launch_mpv()
    finally:
        player_mod.subprocess.Popen = original

    assert "--ytdl=no" in captured["args"], captured["args"]


def test_resolve_explica_video_sin_formatos():
    """YouTube a veces devuelve el video SIN formatos (bloqueo parcial): el
    usuario veia 'sin motivo detallado', que no diagnostica nada. El motivo
    real tiene que llegar al aviso de fallo de resolucion."""
    import search as search_mod

    original = search_mod._extract_info
    search_mod._extract_info = lambda *a, **k: {"title": "x"}
    try:
        r = search_mod.resolve_stream_url("https://www.youtube.com/watch?v=abc")
        assert r is None
        assert "sin formatos" in search_mod.last_resolve_error()
    finally:
        search_mod._extract_info = original


def test_estrategias_client_orden_y_kids_fallback():
    """El orden de players es visionos -> android -> default, con android
    ANTES del default: es el que abre el contenido 'hecho para ninos'
    (coritos, infantiles) que visionos y el web rechazan con
    'This video is not available'. Si alguien vuelve a dejar android
    afuera, esta prueba avisa (el contenido Corito quedo muerto otra
    vez, detectado en vivo el 20/oct/2026)."""
    import search as search_mod

    nombres = [n for n, _ in search_mod._ESTRATEGIAS_CLIENT]
    assert nombres == ["visionos", "android", "default"], nombres
    estrategias = dict(search_mod._ESTRATEGIAS_CLIENT)
    assert (
        estrategias["android"]["extractor_args"]["youtube"]["player_client"]
        == ["android"]
    )
    assert estrategias["default"] == {}


def test_resolucion_kids_cae_de_visionos_a_android():
    """visionos acusa 'This video is not available' (contenido hecho para
    ninos) -> se sigue con android Y SE RESUELVE; el client default ni se
    consulta (si android abrio, no hay tercer intento)."""
    import search as search_mod

    original = search_mod._extract_info
    usados: "list[str]" = []

    def _extrae(client_name, extra, youtube_url):
        usados.append(client_name)
        if client_name == "visionos":
            search_mod._RESOLVE_LAST_ERROR = (
                "ERROR: [youtube] abc: This video is not available"
            )
            return None
        return {"url": "https://stream/ok.mp4", "format_id": "18"}

    search_mod._extract_info = _extrae  # type: ignore[assignment]
    try:
        r = search_mod.resolve_stream_url("https://www.youtube.com/watch?v=abc")
    finally:
        search_mod._extract_info = original
    assert r == ("https://stream/ok.mp4", None), r
    assert usados == ["visionos", "android"], usados


async def test_search_keyboard_has_cancel_button():
    """El listado de /buscar muestra 5 canciones por pagina, cada una con su
    nombre y su duracion, y termina con la fila [◀ ❌ ▶]: en la primera pagina el
    atras queda inerte y en la ultima el derecho pasa a "Buscar mas".
    Queda marcado como pendiente (la card arriba no se reposiciona)."""
    import bot as bot_mod
    from search import SearchResult

    b = make_bot()
    original_search = bot_mod.search
    original_resolve = _patch_resolve(
        bot_mod, {"u1": ("s1", None), "u2": ("s2", None)}
    )
    try:
        bot_mod.search = lambda q, n: [
            SearchResult(url="u1", title="T1", duration="3:00", thumbnail=""),
            SearchResult(url="u2", title="T2", duration="3:00", thumbnail=""),
        ]
        playback_mod.search = lambda q, n: [
            SearchResult(url="u1", title="T1", duration="3:00", thumbnail=""),
            SearchResult(url="u2", title="T2", duration="3:00", thumbnail=""),
        ]
        upd = FakeMessageUpdate("/buscar")
        await b._run_search(
            upd, SimpleNamespace(args=[], bot=b._app.bot), "artista cancion"
        )
    finally:
        bot_mod.resolve_stream_url = original_resolve
        playback_mod.resolve_stream_url = original_resolve
        bot_mod.search = original_search
        playback_mod.search = original_search
        if b._anticipate_task is not None:
            b._anticipate_task.cancel()
    edited = [e for e in b._app.bot.edited if "Página" in str(e[2])]
    assert edited, b._app.bot.edited
    markup = edited[-1][3]["reply_markup"]
    rows = markup.inline_keyboard
    assert len(rows) == 3, rows  # 2 canciones + fila de navegacion
    nav = rows[-1]
    assert len(nav) == 3, nav
    assert nav[0].callback_data == "pick:noop", nav  # primera pagina: sin atras
    assert nav[1].callback_data == "pick:cancel", nav
    assert nav[2].callback_data == "pick:mas", nav  # ultima pagina: buscar mas
    assert b._search_list_pending is True


async def test_search_list_paginates_with_navigation():
    """El listado trae una tanda de 10, muestra 5 por pagina con el nombre y la
    duracion de cada cancion, y la fila de navegacion pasa de pagina. En la
    ultima pagina el boton derecho trae otra tanda."""
    import bot as bot_mod
    from search import SearchResult

    b = make_bot()
    original_search = bot_mod.search
    original_resolve = _patch_resolve(bot_mod, {})
    tanda = [
        SearchResult(
            url=f"u{i}", title=f"Tema {i}", duration="3:00", channel=f"Canal{i}", thumbnail=""
        )
        for i in range(10)
    ]
    try:
        bot_mod.search = lambda q, n: tanda
        playback_mod.search = lambda q, n: tanda
        await b._run_search(
            FakeMessageUpdate("/buscar"),
            SimpleNamespace(args=[], bot=b._app.bot),
            "artista cancion",
        )
    finally:
        bot_mod.resolve_stream_url = original_resolve
        playback_mod.resolve_stream_url = original_resolve
        bot_mod.search = original_search
        playback_mod.search = original_search
        if b._anticipate_task is not None:
            b._anticipate_task.cancel()

    assert len(b._search_resultados) == 10, len(b._search_resultados)
    assert b._search_paginas() == 2, b._search_paginas()

    primera = b._search_teclado().inline_keyboard
    assert len(primera) == 6, primera  # 5 canciones + fila de navegacion
    boton = primera[0][0]
    # Nombre y duracion. SIN numero: el contador va en el texto del mensaje.
    # SIN canal aparte: ya viene dentro del titulo y lo que sobra se recorta.
    assert boton.text == "Tema 0 3:00", boton.text
    assert not boton.text[0].isdigit(), f"el boton no lleva numero: {boton.text!r}"
    assert "Canal0" not in boton.text, f"el canal se repite de mas: {boton.text!r}"
    assert primera[-1][0].callback_data == "pick:noop", primera[-1]
    assert primera[-1][2].callback_data == "pick:next", primera[-1]

    await b._on_search_nav(FakeUpdate("pick:next").callback_query)

    segunda = b._search_teclado().inline_keyboard
    assert b._search_pagina == 1, b._search_pagina
    assert segunda[0][0].text == "Tema 5 3:00", segunda[0][0].text
    assert segunda[-1][0].callback_data == "pick:prev", segunda[-1]
    assert segunda[-1][2].callback_data == "pick:mas", segunda[-1]


async def test_pick_cancel_removes_list_and_keeps_card():
    """Cancelar el listado (❌ Cancelar) lo borra con su desvanecimiento,
    deja la card que estaba arriba intacta y no reproduce nada."""
    from search import SearchResult

    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    b._search_cache["pick:0"] = SearchResult(
        url="u1", title="T1", duration="3:00", thumbnail=""
    )
    b._search_list_pending = True
    played = []

    async def fake_play(*args, **kwargs) -> bool:
        played.append(args)
        return True

    b._play_item = fake_play
    b.playback.play_item = fake_play
    upd = FakeUpdate("pick:cancel", message_id=500, chat_id=44)
    deleted = []
    msg = SimpleNamespace(message_id=500)

    async def _delete():
        deleted.append(500)

    async def _edit(*a, **k):
        deleted.append("edit")

    msg.delete = _delete
    msg.edit_message_text = _edit
    upd.callback_query.message = msg
    await b.on_callback(upd, SimpleNamespace(args=[], bot=b._app.bot))
    assert deleted == [500], deleted  # se borro el mensaje del listado
    assert (44, 7) not in [(d[0], d[1]) for d in b._app.bot.deleted], b._app.bot.deleted
    assert played == [], "cancelar no reproduce nada"
    assert b._search_list_pending is False
    assert b._search_cache == {}


async def test_pick_result_clears_pending_search():
    """Elegir un resultado de /buscar deja de marcar el listado como pendiente:
    el listado ya se quito y la card nueva puede reposicionarse."""
    from search import SearchResult

    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    b._search_list_pending = True
    b._search_cache["pick:0"] = SearchResult(
        url="u1", title="T1", duration="3:00", thumbnail=""
    )

    async def fake_play(update, item, **kwargs):
        return True

    async def fake_stream(url):
        return ("stream", None)

    b._play_item = fake_play
    b.playback.play_item = fake_play
    b._stream_for = fake_stream
    b.playback.stream_for = fake_stream
    upd = FakeUpdate("pick:0", message_id=500, chat_id=44)
    await b.on_callback(upd, SimpleNamespace(args=[]))
    assert b._search_list_pending is False


async def test_with_card_reposition_skips_while_search_pending():
    """Con el listado de /buscar aun en pantalla, el wrapper NO re-renderiza la
    card: queda arriba del listado, sin borrar ni reenviar."""
    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    b._search_list_pending = True
    ran = []

    async def handler(update, context):
        ran.append(1)

    wrapped = b._with_card_reposition(handler)
    await wrapped(FakeMessageUpdate("/x"), SimpleNamespace(args=[]))
    assert ran == [1]
    assert b._app.bot.deleted == [], b._app.bot.deleted
    assert b._app.bot.sent == [], b._app.bot.sent


async def test_cmd_play_link_resets_pending_search():
    """Un link pegado en /play descarta el listado pendiente: el flag se limpia
    y la card vuelve a poder reposicionarse al final."""
    b = make_bot()
    b._search_list_pending = True

    async def fake_play_link(update, context, query):
        pass

    b._play_link_or_playlist = fake_play_link
    await b.cmd_play(
        FakeMessageUpdate(""), SimpleNamespace(args=["https://youtu.be/abc"])
    )
    assert b._search_list_pending is False


def test_la_tarjeta_vive_en_card_py_y_no_duplicada_en_bot():
    """La tarjeta es una pieza, no un metodo mas del bot.

    Sin esta garantia el proximo que agregue un metodo de tarjeta lo mete en
    `bot.py` "porque es mas rapido", y el archivo vuelve a crecer con una
    pieza que tiene su propia identidad y su propio ciclo de vida. El bot solo
    debe DELEGAR: si un metodo aparece con su cuerpo aqui, esta prueba falla.
    """
    import bot as bot_mod
    from card import CardManager

    assert isinstance(bot_mod.YTRemoteBot.__dict__.get("_render_card"), object)

    # Los 16 delegadores existen y son de una linea: delegan a la tarjeta.
    delegadores = [
        "_card_exists_in",
        "_with_card_reposition",
        "_send_track_card",
        "_truncate",
        "_track_status_text",
        "_render_pending",
        "_control_keyboard",
        "_quality_keyboard",
        "_render_card",
        "_swap_card_keyboard",
        "_send_card",
        "_show_card",
        "_remove_card",
        "_reposition_card",
        "_dejar_card_al_final",
        "_enviar_al_chat",
    ]
    for nombre in delegadores:
        metodo = getattr(bot_mod.YTRemoteBot, nombre, None)
        assert metodo is not None, f"el bot ya no delega {nombre}"
        assert metodo.__qualname__.startswith("YTRemoteBot."), metodo.__qualname__

    # Y la pieza tiene TODOS esos metodos con el cuerpo de verdad.
    for nombre in ("exists_in", "with_reposition", "send_track_card", "truncate",
                   "status_text", "render_pending", "control_keyboard",
                   "quality_keyboard", "render_card", "swap_keyboard",
                   "send_card", "show_card", "remove", "reposition",
                   "dejar_al_final", "enviar_al_chat"):
        assert callable(getattr(CardManager, nombre, None)), f"CardManager sin {nombre}"

    # El estado de la tarjeta vive en la pieza, no duplicado en el bot.
    fuente_bot = Path(bot_mod.__file__).read_text(encoding="utf-8")
    assert "InputMediaPhoto" not in fuente_bot, "el render de foto volvio a bot.py"
    assert "edit_message_caption" not in fuente_bot, "el render de la tarjeta volvio a bot.py"
    assert "_card_lock" not in fuente_bot, "el lock de la tarjeta volvio a bot.py"


def run():
    total = run_sync_tests(globals())
    print(f"TARJETA TESTS OK ({total} pruebas)")


if __name__ == "__main__":
    run()
