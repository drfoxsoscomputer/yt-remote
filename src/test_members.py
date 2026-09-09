import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from test_card import FakeBot, FakeUpdate
from test_roles import InMemoryRoles, make_bot
from bot import YTRemoteBot


class MemUpdate:
    """Callback query del editor de usuarios (chat privado del admin)."""

    def __init__(self, data, from_id=1, chat_id=1, message_id=101):
        self.data = data
        self.answered = None
        self.answered_kwargs = {}

        class _Query:
            def __init__(self, parent):
                self.parent = parent
                self.message = SimpleNamespace(
                    message_id=message_id,
                    chat=SimpleNamespace(type="private", id=chat_id),
                )

            @property
            def from_user(self):
                return SimpleNamespace(id=from_id)

            @property
            def data(self):
                return self.parent.data

            async def answer(self, *args, **kwargs):
                self.parent.answered = args or (kwargs.get("text"),)
                self.parent.answered_kwargs = kwargs

        self.callback_query = _Query(self)


def seed_roles(b):
    """Admin 1, dj 51 y user 50 con nombre para la lista de usuarios."""
    b.roles.set_role(1, "admin", "Admin")
    b.roles.set_role(50, "user", "Invitado")
    b.roles.set_role(51, "dj", "DJ Uno")


async def test_usuarios_button_blocked_for_non_admin():
    """El botón 👥 Usuarios está visible para todos, pero solo el admin puede
    operarlo: dj/user reciben alerta y NO se envía nada al privado."""
    b = make_bot()
    seed_roles(b)
    app = b._app
    app.bot.sent.clear()

    # dj toca el botón: rechazado.
    upd = FakeUpdate("ctl:usuarios", message_id=7, chat_id=44, user_id=51)
    await b._on_control(upd, SimpleNamespace(args=[]), "usuarios")
    assert upd.answered and "Solo el admin" in upd.answered[0], upd.answered
    assert app.bot.sent == [], f"no debe enviarse lista: {app.bot.sent}"

    # user toca el botón: rechazado igual.
    upd = FakeUpdate("ctl:usuarios", message_id=7, chat_id=44, user_id=50)
    await b._on_control(upd, SimpleNamespace(args=[]), "usuarios")
    assert upd.answered and "Solo el admin" in upd.answered[0], upd.answered
    assert app.bot.sent == [], f"no debe enviarse lista: {app.bot.sent}"


async def test_usuarios_button_opens_private_list_for_admin():
    """El admin recibe la lista de usuarios en su chat privado y el estado del
    editor queda armado."""
    b = make_bot()
    seed_roles(b)
    app = b._app
    app.bot.sent.clear()

    upd = FakeUpdate("ctl:usuarios", message_id=7, chat_id=44, user_id=1)
    await b._on_control(upd, SimpleNamespace(args=[]), "usuarios")

    assert upd.answered and "chat privado" in upd.answered[0], upd.answered
    dm = [m for m in app.bot.sent if m[0] == 1]
    assert dm, app.bot.sent
    assert "Usuarios (3):" in dm[0][1], dm[0][1]
    assert "Invitado" not in dm[0][1]

    assert b._members_chat_id == 1
    assert b._members_message_id is not None
    assert b._members_user_id == 1
    assert b._members_page == 0
    assert b._members_staged == {}

    # El teclado del editor lista a los 3 usuarios con su rol.
    kb = b._members_keyboard()
    labels = [btn.text for row in kb.inline_keyboard for btn in row]
    assert any("🔒 Admin" in t for t in labels), labels
    assert any("👤 Invitado" in t for t in labels), labels
    assert any("🎧 DJ Uno" in t for t in labels), labels
    # Y los botones de cierre/confirmacion:
    footer = [btn.text for btn in kb.inline_keyboard[-1]]
    assert footer == ["·", "❌", "·", "·"], footer


async def test_members_toggle_stages_and_reverts_live():
    """Tocar un usuario alterna user<->dj SOLO en memoria (staging): el rol
    real no cambia hasta el commit. Volver a tocar revierte en vivo."""
    b = make_bot()
    seed_roles(b)
    await b._open_members_list(FakeUpdate("ctl:usuarios", user_id=1), 1)
    assert b.roles.get_role(50) == "user"

    upd = MemUpdate("mem:user:50")
    await b._on_members_callback(upd, SimpleNamespace())
    assert b._members_staged == {50: "dj"}, b._members_staged
    assert b.roles.get_role(50) == "user", "no debe persistirse hasta el commit"
    assert upd.answered and "ahora es dj" in upd.answered[0], upd.answered
    assert "1 cambio pendiente" in b._members_text(), b._members_text()

    # Revertir: vuelve a user y la lista de cambios queda vacía.
    upd = MemUpdate("mem:user:50")
    await b._on_members_callback(upd, SimpleNamespace())
    assert b._members_staged == {}, b._members_staged
    assert b.roles.get_role(50) == "user"
    assert upd.answered and "ahora es user" in upd.answered[0], upd.answered


async def test_members_commit_applies_and_notifies():
    """✔ aplica todos los cambios staged a roles.json, avisa al usuario por
    privado y borra el mensaje de la lista."""
    b = make_bot()
    seed_roles(b)
    app = b._app
    app.bot.sent.clear()
    await b._open_members_list(FakeUpdate("ctl:usuarios", user_id=1), 1)

    await b._on_members_callback(MemUpdate("mem:user:50"), SimpleNamespace())
    await b._on_members_callback(MemUpdate("mem:user:51"), SimpleNamespace())

app.bot.sent.clear()
        # Primero pulsa ✔ (commit) -> muestra alerta de confirmación
        upd = MemUpdate("mem:commit")
        await b._on_members_callback(upd, SimpleNamespace())
        # Verifica que se mostró la alerta de confirmación
        assert "¿Aplicar 2 cambios?" in app.bot.edited[-1][2]
        kb = app.bot.edited[-1][3]["reply_markup"]
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        assert "✔ Sí, aplicar" in labels
        assert "⚠️ No" in labels
        # Luego pulsa Sí -> aplica y cierra
        app.bot.sent.clear()
        app.bot.edited = []
        upd2 = MemUpdate("mem:commit-yes")
        await b._on_members_callback(upd2, SimpleNamespace())
    # Ambos roles efectivamente aplicados.
    assert b.roles.get_role(50) == "dj"
    assert b.roles.get_role(51) == "user"
    # Notificación por privado a cada usuario avisando el rol nuevo.
    notified = [m for m in app.bot.sent if m[0] == 50]
    notified51 = [m for m in app.bot.sent if m[0] == 51]
    assert notified and "Ahora eres DJ" in notified[0][1], notified
    assert notified51 and "ya no eres DJ" in notified51[0][1], notified51
    assert upd2.answered and "Roles guardados (2 cambios)." in upd2.answered[0], upd2.answered
    # El mensaje del editor se borra y el estado queda limpio.
    assert app.bot.deleted, app.bot.deleted
    assert b._members_chat_id is None
    assert b._members_message_id is None
    assert b._members_staged == {}


async def test_members_cancel_without_changes():
    """❌ sin cambios: solo cierra (toast 'Sin cambios.') sin tocar roles."""
    b = make_bot()
    seed_roles(b)
    await b._open_members_list(FakeUpdate("ctl:usuarios", user_id=1), 1)
    upd = MemUpdate("mem:cancel")
    await b._on_members_callback(upd, SimpleNamespace())
    assert upd.answered and "Sin cambios." in upd.answered[0], upd.answered
    assert b.roles.get_role(50) == "user"
    assert b.roles.get_role(51) == "dj"
    assert b._members_chat_id is None
    assert b._members_message_id is None


async def test_members_cancel_discards_staged():
    """❌ con cambios staged: descarta cambios, no notifica, borra la lista."""
    b = make_bot()
    seed_roles(b)
    app = b._app
    app.bot.sent.clear()
    await b._open_members_list(FakeUpdate("ctl:usuarios", user_id=1), 1)

    await b._on_members_callback(MemUpdate("mem:user:50"), SimpleNamespace())
    assert b._members_staged == {50: "dj"}

    upd = MemUpdate("mem:cancel")
    await b._on_members_callback(upd, SimpleNamespace())

    assert b.roles.get_role(50) == "user", "no debe persistirse"
    assert b.roles.get_role(51) == "dj"
    assert b._members_staged == {}, b._members_staged
    assert app.bot.deleted, app.bot.deleted
    assert app.bot.sent == [], app.bot.sent


async def test_members_cancel_no_discard_on_no():
    """❌ con cambios staged + No: restaura mensaje, staged intactos."""
    b = make_bot()
    seed_roles(b)
    app = b._app
    app.bot.sent.clear()
    await b._open_members_list(FakeUpdate("ctl:usuarios", user_id=1), 1)

    await b._on_members_callback(MemUpdate("mem:user:50"), SimpleNamespace())
    assert b._members_staged == {50: "dj"}

    upd = MemUpdate("mem:cancel")
    await b._on_members_callback(upd, SimpleNamespace())
    assert "¿Descartar 1 cambio?" in app.bot.edited[-1][2], app.bot.edited[-1][2]

    upd = MemUpdate("mem:cancel-no")
    await b._on_members_callback(upd, SimpleNamespace())
    assert b._members_staged == {50: "dj"}
    assert app.bot.edited[-1][2].startswith("Usuarios (")


async def test_members_commit_no_discard_on_no():
    """✔ con cambios staged + No: restaura mensaje, staged intactos, roles sin tocar."""
    b = make_bot()
    seed_roles(b)
    app = b._app
    app.bot.sent.clear()
    await b._open_members_list(FakeUpdate("ctl:usuarios", user_id=1), 1)

    await b._on_members_callback(MemUpdate("mem:user:50"), SimpleNamespace())
    assert b._members_staged == {50: "dj"}

    upd = MemUpdate("mem:commit")
    await b._on_members_callback(upd, SimpleNamespace())
    assert "¿Aplicar 1 cambio?" in app.bot.edited[-1][2], app.bot.edited[-1][2]

    upd = MemUpdate("mem:commit-no")
    await b._on_members_callback(upd, SimpleNamespace())
    assert b._members_staged == {50: "dj"}
    assert b.roles.get_role(50) == "user"
    assert app.bot.edited[-1][2].startswith("Usuarios (")


async def test_members_admin_role_is_protected():
    """El rol del admin no se puede alternar: se avisa con alerta (y el propio
    admin no puede tocarse a si mismo)."""
    b = make_bot()
    seed_roles(b)
    b.roles.set_role(53, "admin", "Segundo Admin")
    await b._open_members_list(FakeUpdate("ctl:usuarios", user_id=1), 1)

    # Otro admin: rechazado con alerta.
    upd = MemUpdate("mem:user:53")
    await b._on_members_callback(upd, SimpleNamespace())
    assert upd.answered_kwargs.get("show_alert") is True, upd.answered_kwargs
    assert "El rol del admin" in upd.answered[0], upd.answered
    assert b._members_staged == {}

    # El propio admin: rechazado (auto-proteccion).
    upd = MemUpdate("mem:user:1")
    await b._on_members_callback(upd, SimpleNamespace())
    assert "Tu propio rol" in upd.answered[0], upd.answered
    assert b._members_staged == {}


async def test_members_callback_guards():
    """El handler mem:* exige chat privado, ser el propio admin y rol admin."""
    b = make_bot()
    seed_roles(b)

    # No-admin tocando desde su privado: rechazado.
    upd = MemUpdate("mem:user:50", from_id=50, chat_id=50)
    await b._on_members_callback(upd, SimpleNamespace())
    assert upd.answered_kwargs.get("show_alert") is True, upd.answered_kwargs
    assert "No tiene permiso" in upd.answered[0], upd.answered
    assert b._members_staged == {}

    # Admin tocando desde el grupo: rechazado.
    upd = MemUpdate("mem:user:50", from_id=1, chat_id=44)
    await b._on_members_callback(upd, SimpleNamespace())
    assert upd.answered_kwargs.get("show_alert") is True, upd.answered_kwargs
    assert "No tiene permiso" in upd.answered[0], upd.answered
    assert b._members_staged == {}

    # Admin tocando el propio admin: rechazado.
    upd = MemUpdate("mem:user:1", from_id=1, chat_id=1)
    await b._on_members_callback(upd, SimpleNamespace())
    assert upd.answered_kwargs.get("show_alert") is True, upd.answered_kwargs
    assert "Tu propio rol" in upd.answered[0], upd.answered
    assert b._members_staged == {}


async def test_welcome_registers_and_mentions():
    """Quien se une al grupo recibe la bienvenida con @username (registrado
    como usuario conocido). El bot mismo no genera bienvenida."""
    b = make_bot()
    app = b._app
    member = SimpleNamespace(id=55, full_name="Nueva Persona", username="nueva_p", first_name="Nueva")
    upd = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=44), new_chat_members=[member]),
        effective_user=member,
        callback_query=None,
    )
    await b._on_new_members(upd, app)

    assert b.roles.get_name(55) == "Nueva Persona"
    sent = app.bot.sent
    assert sent and sent[-1][0] == 44, sent
    assert "@nueva_p" in sent[-1][1], sent[-1][1]
    assert "invitado" in sent[-1][1]
    assert "24 h" not in sent[-1][1], sent[-1][1]

    # Con kick activo, la bienvenida avisa el plazo.
    b.config.kick_after_hours = 24
    app.bot.sent.clear()
    await b._on_new_members(upd, app)
    assert "Acceso de invitado: 24 h" in app.bot.sent[-1][1], app.bot.sent[-1][1]


async def test_rules_text_optional_kick_line():
    """El mensaje de reglas menciona el plazo solo si el kick está activo."""
    b = make_bot()
    assert "24 h" not in b.rules_text()
    b.config.kick_after_hours = 24
    assert "24 h" in b.rules_text()
    assert "👥 Usuarios" in b.rules_text()


async def test_dm_or_group_fallback():
    """El DM falla (usuario nunca habló con el bot): el aviso cae al grupo."""
    b = make_bot()
    b.config.allowed_chat_id = 44

    class FlakyBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            if chat_id == 50:
                raise RuntimeError("Bot can't initiate conversation")
            return await super().send_message(chat_id, text, **kwargs)

    flaky = FlakyBot()
    b._app.bot = flaky
    flaky.sent.clear()
    await b._dm_or_group(50, "aviso")
    assert flaky.sent == [(44, "aviso", {})], flaky.sent


async def test_auto_kick_job():
    """Con kick_after_hours activo, el job hace ban+unban (kick) SOLO a los
    users vencidos; dj/admin y los no vencidos quedan exentos."""
    b = make_bot()
    b.config.kick_after_hours = 2
    b.config.allowed_chat_id = 44
    seed_roles(b)  # joined_at=123 (siempre vencido en el fake)
    # Un user reciente (no vence).
    import time as _time
    b.roles._users["52"] = {"name": "Reciente", "joined_at": int(_time.time())}
    b.roles.set_role(52, "user")

    app = b._app
    await b._auto_kick_job(SimpleNamespace())

    illegal = [c for c in app.bot.banned if c[1] in (51, 1, 52)]
    assert not illegal, f"no se debe expulsar dj/admin/recientes: {illegal}"
    assert (44, 50) in app.bot.banned, app.bot.banned
    assert (44, 50) in app.bot.unbanned, app.bot.unbanned

    # Kick inactivo (0): no expulsa a nadie.
    b.config.kick_after_hours = 0
    app.bot.banned.clear()
    await b._auto_kick_job(SimpleNamespace())
    assert app.bot.banned == [], app.bot.banned


async def run():
    tests = [
        test_usuarios_button_blocked_for_non_admin,
        test_usuarios_button_opens_private_list_for_admin,
        test_members_toggle_stages_and_reverts_live,
        test_members_commit_applies_and_notifies,
        test_members_cancel_without_changes,
        test_members_cancel_discards_staged,
        test_members_cancel_no_discard_on_no,
        test_members_commit_no_discard_on_no,
        test_members_admin_role_is_protected,
        test_members_callback_guards,
        test_welcome_registers_and_mentions,
        test_rules_text_optional_kick_line,
        test_dm_or_group_fallback,
        test_auto_kick_job,
    ]
    print("\n=== Tests del administrador de usuarios ===\n")
    for t in tests:
        if asyncio.iscoroutinefunction(t):
            await t()
        else:
            t()
        print(f"  OK  {t.__name__}")

    # Asegurar que todo el suite anterior sigue verde.
    import subprocess

    print("\n=== Suite completa (roles + tarjeta) ===\n")

    def run_file(name):
        import os
        import sys as _sys

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        res = subprocess.run(
            [_sys.executable, str(SRC / name)],
            cwd=SRC.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        tail = "\n".join(res.stdout.splitlines()[-4:])
        print(tail)
        return res.returncode

    rc = 0
    for name in ("test_roles.py", "test_card.py"):
        rc |= run_file(name)
    if rc:
        raise SystemExit(f"Suite completa fallo (rc={rc})")
    print("\nMEMBERS TESTS OK")


if __name__ == "__main__":
    asyncio.run(run())
