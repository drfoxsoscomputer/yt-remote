"""Tests del modelo de roles de YT-Remote.

Valida toda la matriz de permisos:
- _require("dj") bloquea user, permite dj y admin
- _require("admin") bloquea user y dj, permite admin
- _on_control: 7 acciones de control solo para dj/admin
- cmd_solicitar: user puede pedir, dj/admin recibe denegado
- help_for_role: contenido correcto por nivel
- get_all_with_role: filtro exacto por rol

No toca el data/roles.json real: se reemplaza roles por uno en memoria.
Reutiliza los fakes de test_card.py.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from testkit import run_sync_tests  # noqa: E402

from test_card import FakeBot, FakePlayer, FakeApp, FakeMessageUpdate, FakeUpdate  # noqa: E402


class InMemoryRoles:
    """RoleManager en memoria: no toca data/roles.json."""

    VALID_ROLES = {"admin", "dj", "user"}

    def __init__(self):
        self._roles: dict[str, str] = {}
        self._users: dict[str, dict] = {}

    def get_role(self, user_id: int, default: str = "user") -> str:
        return self._roles.get(str(user_id), default)

    def has_role(self, user_id: int, required: str) -> bool:
        rank = {"user": 0, "dj": 1, "admin": 2}
        return rank.get(self.get_role(user_id), 0) >= rank.get(required, 0)

    def set_role(self, user_id: int, role: str, name: str | None = None) -> None:
        if role not in self.VALID_ROLES:
            raise ValueError(f"Rol invalido: {role}")
        self._roles[str(user_id)] = role
        self.register_user(user_id, name)

    def remove_user(self, user_id: int) -> bool:
        key = str(user_id)
        if key in self._roles:
            del self._roles[key]
            return True
        return False

    def get_all_with_role(self, role: str) -> list[int]:
        return [int(uid) for uid, r in self._roles.items() if r == role]

    def register_user(self, user_id: int, name: str | None = None) -> bool:
        key = str(user_id)
        existing = self._users.get(key)
        if existing is not None:
            if name:
                existing["name"] = name
            return False
        self._users[key] = {"name": name or key, "joined_at": int(123)}
        return True

    def get_name(self, user_id: int) -> str | None:
        u = self._users.get(str(user_id))
        return u["name"] if u else None

    def get_joined_at(self, user_id: int) -> int | None:
        u = self._users.get(str(user_id))
        return u["joined_at"] if u else None

    def known_users(self):
        users = []
        for uid, role in self._roles.items():
            u = self._users.get(uid, {})
            users.append(
                {
                    "id": int(uid),
                    "name": u.get("name", uid),
                    "role": role,
                    "joined_at": u.get("joined_at"),
                }
            )
        users.sort(key=lambda x: x["name"].lower())
        return users


def make_bot():
    """Crea un bot con role manager en memoria y player fake.
    _chat_allowed se monkey-patchea para que siempre devuelva True
    (aisla el test de la logica de chat/owner)."""
    import tempfile
    import bot as bot_mod
    from config import Config
    from persistence import StateStore

    config = Config(
        token="test",
        mpv_path="mpv",
        default_role="user",
        max_results=5,
        owner_id=1,
        allowed_chat_id=None,
    )
    # La construccion ya no toca el roles.json real: al importar test_card,
    # este redirige roles.ROLES_PATH a un temp para todo el proceso.
    b = bot_mod.YTRemoteBot(config)
    b._app = FakeApp()
    object.__setattr__(b, "player", FakePlayer())
    object.__setattr__(b, "_volume", 100)
    object.__setattr__(b, "_paused", False)
    object.__setattr__(b, "roles", InMemoryRoles())
    object.__setattr__(b, "_roles", b.roles)
    b._chat_allowed = lambda update: True
    # Aislar la persistencia: nunca tocar el state.json real del proyecto.
    tmp = tempfile.TemporaryDirectory()
    b._state = StateStore(Path(tmp.name) / "state.json")
    return b


def test_has_role_rank():
    """admin pasa como dj (rank 2 >= 1), pero get_all_with_role filtra exacto."""
    b = make_bot()
    b.roles.set_role(50, "admin")
    b.roles.set_role(51, "dj")
    b.roles.set_role(52, "user")
    assert b.roles.has_role(50, "dj") is True
    assert b.roles.has_role(50, "admin") is True
    assert b.roles.has_role(51, "dj") is True
    assert b.roles.has_role(51, "admin") is False
    assert b.roles.has_role(52, "dj") is False
    assert b.roles.has_role(52, "user") is True
    print("  OK  test_has_role_rank")


def test_get_all_with_role_exact_match():
    """get_all_with_role devuelve solo el rol exacto, no los de rank superior."""
    b = make_bot()
    b.roles.set_role(50, "user")
    b.roles.set_role(51, "dj")
    b.roles.set_role(52, "admin")
    b.roles.set_role(53, "dj")
    assert sorted(b.roles.get_all_with_role("dj")) == [51, 53]
    assert b.roles.get_all_with_role("admin") == [52]
    assert b.roles.get_all_with_role("user") == [50]
    assert 52 not in b.roles.get_all_with_role("dj"), "admin no debe aparecer en lista de djs"
    print("  OK  test_get_all_with_role_exact_match")


def test_require_dj_blocks_user():
    """_require("dj") rechaza user con mensaje de denegacion."""
    b = make_bot()
    b.roles.set_role(50, "user")
    upd = FakeMessageUpdate("/buscar GP Band", user_id=50)
    ctx = SimpleNamespace(args=["GP", "Band"])

    async def run():
        wrapped = b._require("dj", b.cmd_play)
        await wrapped(upd, ctx)

    asyncio.run(run())
    assert len(upd.sent) == 1, f"se esperaba 1 reply, got {upd.sent}"
    reply_text = upd.sent[0][0]
    assert "Acceso denegado" in reply_text
    assert "dj" in reply_text
    print("  OK  test_require_dj_blocks_user")


def test_require_dj_allows_dj_and_admin():
    """_require("dj") permite dj Y admin (rank pass-through)."""
    b = make_bot()
    b.roles.set_role(50, "dj")
    b.roles.set_role(51, "admin")
    b.roles.set_role(52, "user")
    search_calls: list = []

    async def fake_run(update, context, query):
        search_calls.append(query)

    b._run_search = fake_run

    async def run():
        for uid in [50, 51, 52]:
            upd = FakeMessageUpdate("/buscar X", user_id=uid)
            ctx = SimpleNamespace(args=["GP", "Band"])
            wrapped = b._require("dj", b.cmd_play)
            await wrapped(upd, ctx)

    asyncio.run(run())
    assert len(search_calls) == 2, f"Se esperaban 2 llamadas, got {len(search_calls)}"
    print("  OK  test_require_dj_allows_dj_and_admin")


def test_require_admin_blocks_user_and_dj():
    """_require("admin") rechaza user y dj, solo admin pasa."""
    b = make_bot()
    b.roles.set_role(50, "user")
    b.roles.set_role(51, "dj")
    b.roles.set_role(52, "admin")
    admin_calls: list = []

    async def fake_adduser(update, context):
        admin_calls.append(update.effective_user.id)

    async def run():
        for uid in [50, 51, 52]:
            upd = FakeMessageUpdate("/adduser", user_id=uid)
            ctx = SimpleNamespace(args=["999", "dj"])
            wrapped = b._require("admin", fake_adduser)
            await wrapped(upd, ctx)

    asyncio.run(run())
    assert len(admin_calls) == 1, f"se esperaba 1 admin pass, got {admin_calls}"
    assert admin_calls[0] == 52
    print("  OK  test_require_admin_blocks_user_and_dj")


async def test_on_control_blocks_user_toast_all_actions():
    """Los 7 botones de control devuelven toast a user; dj los ejecuta."""
    b = make_bot()
    b.roles.set_role(50, "user")
    b.roles.set_role(51, "dj")
    b._volume = 100

    actions_to_test = ["pp", "prev", "next", "stop", "vol-10", "vol+10", "vol-info"]
    for action in actions_to_test:
        upd_user = FakeUpdate(f"ctl:{action}", user_id=50)
        await b._on_control(upd_user, SimpleNamespace(args=[]), action)
        assert upd_user.answered is not None, f"user no recibio toast en '{action}'"
        toast_text = upd_user.answered[0] if upd_user.answered else ""
        assert "No tiene permiso" in toast_text, f"toast incorrecto en '{action}': {toast_text!r}"
        assert b.player.resumed == 0
        assert b.player.paused == 0

    vol_user = FakeUpdate("ctl:vol+10", user_id=50)
    await b._on_control(vol_user, SimpleNamespace(args=[]), "vol+10")
    assert b._volume == 100, "user no deberia haber cambiado el volumen"
    print("  OK  test_on_control_blocks_user_toast_all_actions")


async def test_on_control_allows_dj():
    """dj puede usar todos los botones de control."""
    b = make_bot()
    b.roles.set_role(51, "dj")
    b._volume = 100
    b._paused = False

    from queue_manager import QueueItem
    b.queue.set_current(QueueItem(url="u1", title="X"))

    upd_pp = FakeUpdate("ctl:pp", user_id=51)
    await b._on_control(upd_pp, SimpleNamespace(args=[]), "pp")
    assert b._paused is True, "pp deberia pausar"

    upd_vol = FakeUpdate("ctl:vol-10", user_id=51)
    await b._on_control(upd_vol, SimpleNamespace(args=[]), "vol-10")
    assert b._volume == 90, f"vol deberia ser 90, got {b._volume}"
    print("  OK  test_on_control_allows_dj")


async def test_on_control_lista_works_for_user():
    """El boton 📋 no se bloquea para un user: puede ver la lista, que se
    ENVIA como mensaje aparte (la card no se pisa)."""
    from queue_manager import QueueItem

    b = make_bot()
    b.roles.set_role(50, "user")
    b.queue.set_playlist(
        [QueueItem(url=f"u{i}", title=f"Tema {i}") for i in range(3)]
    )
    upd = FakeUpdate("ctl:lista", user_id=50, chat_id=44)
    await b._on_control(upd, SimpleNamespace(args=[]), "lista")
    # Lista enviada como mensaje nuevo; el listado arranca con "Lista:".
    assert b._list_message_id is not None
    assert b._app.bot.sent, "deberia enviar el mensaje de la lista"
    text = b._app.bot.sent[-1][1]
    assert text.startswith("Lista:"), text
    # El user abre la lista SIN botones de tema (flag fijo en el mensaje).
    assert b._list_can_select is False
    print("  OK  test_on_control_lista_works_for_user")


async def test_cmd_solicitar_blocks_non_user():
    """dj y admin que escriben /solicitar reciben 'Ya tiene un rol'."""
    b = make_bot()
    b.roles.set_role(50, "dj")
    b.roles.set_role(51, "admin")

    for uid in [50, 51]:
        upd = FakeMessageUpdate("/solicitar", user_id=uid)
        ctx = SimpleNamespace(args=[])
        await b.cmd_solicitar(upd, ctx)
        assert len(upd.sent) == 1, f"uid={uid} deberia recibir 1 respuesta, got {upd.sent}"
        assert "Ya tiene un rol" in upd.sent[0][0], f"texto: {upd.sent[0]}"
    print("  OK  test_cmd_solicitar_blocks_non_user")


async def test_cmd_solicitar_notifies_admins_for_user():
    """user que escribe /solicitar: mensaje al admin indicando el boton
    '👥 Usuarios' de la tarjeta (ya no se sugiere /adduser)."""
    b = make_bot()
    b.roles.set_role(50, "user")
    b.roles.set_role(51, "admin")
    app = b._app
    app.bot.sent.clear()

    upd = FakeMessageUpdate("/solicitar", user_id=50)
    upd.effective_user = SimpleNamespace(id=50, full_name="Test User", username="tester")
    ctx = SimpleNamespace(args=[], bot=app.bot)
    await b.cmd_solicitar(upd, ctx)

    confirm = [m for m in upd.sent if "solicitud fue enviada" in m[0]]
    assert confirm, f"no hubo confirmacion al user: {upd.sent}"
    admin_msg = [m for m in app.bot.sent if "Usuarios" in m[1]]
    assert admin_msg, f"no hubo mensaje al admin: {app.bot.sent}"
    assert "👥 Usuarios" in admin_msg[0][1], f"deberia mencionar el boton: {admin_msg[0][1]}"
    assert str(50) in admin_msg[0][1], f"deberia incluir user ID: {admin_msg[0][1]}"
    assert "abra la lista con el botón 👥 Usuarios" in admin_msg[0][1], admin_msg[0][1]
    print("  OK  test_cmd_solicitar_notifies_admins_for_user")


def test_help_for_role_visibility_matrix():
    """user=nada, dj=1 linea, admin=2 lineas (👥 Usuarios y /reglas)."""
    b = make_bot()
    assert b.help_for_role("user") == "", f"user deberia dar vacio, got: {b.help_for_role('user')!r}"

    dj_help = b.help_for_role("dj")
    dj_lines = [l for l in dj_help.splitlines() if l.strip()]
    assert len(dj_lines) == 1, f"dj deberia dar 1 linea, got {len(dj_lines)}: {dj_help!r}"
    assert "/buscar" in dj_help

    admin_help = b.help_for_role("admin")
    admin_lines = [l for l in admin_help.splitlines() if l.strip()]
    assert len(admin_lines) == 3, f"admin deberia dar 3 lineas, got {len(admin_lines)}: {admin_help!r}"
    assert "/buscar" in admin_help
    assert "👥 Usuarios" in admin_help
    assert "/reglas" in admin_help
    assert "/adduser" not in admin_help
    assert "/removeuser" not in admin_help
    print("  OK  test_help_for_role_visibility_matrix")


async def test_buscar_rejected_before_search_for_user():
    """user que escribe /buscar: denegado ANTES de que se invoque search."""
    b = make_bot()
    b.roles.set_role(50, "user")
    search_called: list = []

    async def fake_run(update, context, query):
        search_called.append(query)

    b._run_search = fake_run
    upd = FakeMessageUpdate("/buscar", user_id=50)
    ctx = SimpleNamespace(args=["GP", "Band", "-", "Inexplicable"])
    wrapped = b._require("dj", b.cmd_play)
    await wrapped(upd, ctx)
    assert len(search_called) == 0, f"search no deberia llamarse para user: {search_called}"
    assert len(upd.sent) == 1
    assert "Acceso denegado" in upd.sent[0][0]
    print("  OK  test_buscar_rejected_before_search_for_user")


def test_los_roles_sobreviven_a_reiniciar_el_bot():
    """LA REGRESION PRINCIPAL DE roles.py.

    `_load()` hacia `DELETE FROM roles` y `DELETE FROM users` en cada arranque y
    despues solo reimportaba de `data/roles.json`, un archivo que ya no existe
    (los datos llevaban semanas en el SQLite). O sea: **cada reinicio del bot
    dejaba la tabla vacia y los roles se perdian en silencio**.

    Se comprueba con VARIOS RoleManager de verdad sobre la misma base: cada uno
    es un reinicio.
    """
    import tempfile

    import roles as roles_mod

    carpeta = Path(tempfile.mkdtemp())
    original_db = roles_mod.DB_PATH
    original_json = roles_mod.ROLES_PATH
    roles_mod.DB_PATH = carpeta / "roles.db"
    roles_mod.ROLES_PATH = carpeta / "NO_EXISTE_roles.json"
    try:
        # Arranque 1: se asignan roles.
        r1 = roles_mod.RoleManager()
        r1.set_role(77, "admin", "Dueño")
        r1.set_role(88, "dj", "Dj Uno")
        r1.set_role(99, "user", "Invitado")

        # Arranque 2 (el reinicio): antes esto vaciaba todo.
        r2 = roles_mod.RoleManager()
        assert r2.get_role(77) == "admin", f"se perdio el admin: {r2._roles}"
        assert r2.get_role(88) == "dj", f"se perdio el dj: {r2._roles}"
        assert r2.get_name(77) == "Dueño", f"se perdio el nombre: {r2._users}"
        assert r2.get_joined_at(88) is not None, "se perdio la fecha de entrada"

        # Arranque 3, por las dudas.
        r3 = roles_mod.RoleManager()
        assert r3.get_role(77) == "admin", r3._roles
        assert len(r3.known_users()) == 3, r3.known_users()

        # Y quitar uno de verdad lo quita de la base, no solo de la memoria.
        r3.remove_user(99)
        r4 = roles_mod.RoleManager()
        assert r4.get_name(99) is None, f"no se borro de la base: {r4.known_users()}"
        assert r4.get_role(77) == "admin", "borrar uno se llevo los demas"
    finally:
        roles_mod.DB_PATH = original_db
        roles_mod.ROLES_PATH = original_json


def test_los_roles_sobreviven_a_la_migracion_del_legacy():
    """El fallo que solo se ve con un arranque de verdad.

    El `store` renombra el `roles.db` viejo a `.migrado` al migrar. Si
    `RoleManager` sigue con su propio archivo, a partir de ese momento abre un
    `roles.db` NUEVO y vacio en cada llamada y el primer SELECT revienta con
    `no such table: roles`.

    Como `register_user` se llama en el handler que re-coloca la tarjeta, el
    bot moria AHI: la tarjeta dejaba de re-renderizarse y los roles dejaban de
    funcionar. En las pruebas anteriores no se veia porque cada una parcheaba
    `roles.DB_PATH` a una carpeta temporal: nunca se daba el cruce de la
    migracion.

    Aqui se reproduce el cruce de verdad: base con legacy -> Store migra y
    renombra -> RoleManager sigue funcionando y NO crea un segundo archivo.
    """
    import json
    import sqlite3
    import tempfile

    import roles as roles_mod
    import store as store_mod

    carpeta = Path(tempfile.mkdtemp())
    # El roles.db legacy, con el admin real del usuario.
    legado = carpeta / "roles.db"
    conn = sqlite3.connect(str(legado))
    conn.execute("CREATE TABLE roles (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE users (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO roles VALUES ('7', 'admin')")
    conn.execute(
        "INSERT INTO users VALUES ('7', ?)", (json.dumps({"name": "Dueño", "joined_at": 1}),)
    )
    conn.commit()
    conn.close()
    (carpeta / "state.json").write_text(
        json.dumps({"version": 1, "playlist": [], "history": []}), encoding="utf-8"
    )

    base = carpeta / "ytremote.db"
    db_original = roles_mod.DB_PATH
    roles_path_original = roles_mod.ROLES_PATH
    try:
        roles_mod.DB_PATH = base
        roles_mod.ROLES_PATH = carpeta / "roles.json"

        # 1) El store migra: lee el legacy y lo renombra. Esto es lo que hacia
        #    el bot al arrancar.
        store = store_mod.Store(base)
        assert (carpeta / "roles.db.migrado").is_file(), "el legacy no se renombro"

        # 2) El bot arma su RoleManager DESPUES de esa migracion.
        rm = roles_mod.RoleManager()
        assert rm.get_role(7) == "admin", rm.get_role(7)
        assert rm.get_name(7) == "Dueño", rm.get_name(7)

        # 3) Y lo que reventaba en produccion: registrar un usuario cualquiera.
        assert rm.register_user(99, "Alguien") is True
        assert rm.get_name(99) == "Alguien"

        # 4) El fallo de verdad: NO puede haber un segundo archivo de roles.
        assert not (carpeta / "roles.db").exists(), (
            "RoleManager volvio a crear su propio roles.db: hay dos bases y la "
            "migracion las desincroniza"
        )
        assert len(list(carpeta.glob("*.db"))) == 1, sorted(p.name for p in carpeta.glob("*.db"))

        # 5) Y el rol sobrevive a un reinicio (se lee de la misma base).
        assert roles_mod.RoleManager().get_role(7) == "admin"
        store.cerrar()
    finally:
        roles_mod.DB_PATH = db_original
        roles_mod.ROLES_PATH = roles_path_original


def run():
    print("\n=== Tests de modelo de roles ===\n")
    total = run_sync_tests(globals())
    print(f"\nROLES TESTS OK ({total} pruebas)")


if __name__ == "__main__":
    run()
