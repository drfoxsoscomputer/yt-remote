"""Demo visual del modelo de roles de YT-Remote en terminal.

Crea un bot fake y simula las acciones de cada rol (user, dj, admin),
imprimiendo en la terminal que veria cada uno. Sin dependencias externas.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

# Forzar UTF-8 en Windows (la salida incluye emojis)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from test_roles import make_bot, FakeMessageUpdate  # noqa: E402
from test_card import FakeUpdate  # noqa: E402

USER_ID = 10
DJ_ID = 20
ADMIN_ID = 30


def color(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


RED = "91"
GREEN = "92"
YELLOW = "93"
BLUE = "94"
MAGENTA = "95"
CYAN = "96"
GRAY = "90"
BOLD = "1"


def role_header(rolename: str, uid: int) -> None:
    print()
    print(color(f"  [ {rolename.upper()} — ID: {uid} ]", f"{BOLD};{MAGENTA}"))


def action(cmd: str, result: str, ok: bool) -> None:
    status = color("OK", GREEN) if ok else color("DENEGADO", RED)
    lines = str(result).splitlines()
    first = lines[0] if lines else ""
    print(f"  {color('>', GRAY)} {color(cmd, CYAN)}")
    print(f"    {status}: {first}")
    for line in lines[1:]:
        print(f"          {line}")


async def show_user(b):
    app = b._app
    role_header("USER", USER_ID)

    # /buscar (user recibe denegado)
    upd = FakeMessageUpdate("/buscar", user_id=USER_ID)
    ctx = SimpleNamespace(args=["GP", "Band", "-", "Inexplicable"])
    wrapped = b._require("dj", b.cmd_play)
    await wrapped(upd, ctx)
    action("/buscar GP Band - Inexplicable", upd.sent[-1][0] if upd.sent else "(sin respuesta)", ok=False)

    for cb in ["pp", "next", "prev", "stop", "vol+10", "vol-10"]:
        upd_cb = FakeUpdate(f"ctl:{cb}", user_id=USER_ID)
        await b._on_control(upd_cb, SimpleNamespace(args=[]), cb)
        toast = upd_cb.answered[0] if upd_cb.answered else "(sin toast)"
        action(f"boton {cb}", toast, ok=False)

    upd_cb = FakeUpdate("ctl:lista", user_id=USER_ID)
    await b._on_control(upd_cb, SimpleNamespace(args=[]), "lista")
    action("boton lista", upd_cb.answered[0] if upd_cb.answered else "(sin respuesta)", ok=False)
    # Sin rol NO hay puerta de solicitud: /solicitar se fue; el aviso unico
    # de /start es la unica respuesta que recibe.
    upd = FakeMessageUpdate("/start", user_id=USER_ID)
    upd.chat.title = "Grupo de demo"
    await b.cmd_start(upd, SimpleNamespace(args=[]))
    action("/start (sin rol)", upd.sent[-1][0] if upd.sent else "(sin respuesta)", ok=False)


async def show_dj(b, app):
    role_header("DJ", DJ_ID)

    # Stub _run_search para que no intente buscar en la red
    original_run_search = b._run_search

    async def fake_run_search(update, context, query):
        await update.message.reply_text(f"[simulado] Buscando: {query}")
        return []

    b._run_search = fake_run_search

    upd = FakeMessageUpdate("/buscar", user_id=DJ_ID)
    ctx = SimpleNamespace(args=["Mafe", "Restrepo"])
    wrapped = b._require("dj", b.cmd_play)
    await wrapped(upd, ctx)
    action("/buscar Mafe Restrepo", upd.sent[-1][0] if upd.sent else "(busqueda lanzada)", ok=True)

    from queue_manager import QueueItem
    b.queue.set_current(QueueItem(url="u1", title="Una Cancion"))

    upd_cb = FakeUpdate("ctl:pp", user_id=DJ_ID)
    await b._on_control(upd_cb, SimpleNamespace(args=[]), "pp")
    action("boton pp", "(toggle play/pause)", ok=True)

    upd_cb = FakeUpdate("ctl:vol-10", user_id=DJ_ID)
    await b._on_control(upd_cb, SimpleNamespace(args=[]), "vol-10")
    action("boton vol-10", f"(volumen: {b._volume})", ok=True)

    b._run_search = original_run_search


async def show_admin(b):
    app = b._app
    role_header("ADMIN", ADMIN_ID)

    # La UNICA puerta a dj: el admin habilita con 👥 Usuarios de la tarjeta
    # (el aviso que recibe el user por DM es el mismo del cambio de rol).
    b.roles.set_role(USER_ID, "dj")
    await b._notify_role_change(USER_ID, "dj")
    dm = [m for m in app.bot.sent if m[0] == USER_ID]
    action("👥 habilitar dj", dm[-1][1].splitlines()[0] if dm else "(sin DM)", ok=True)

    b.roles.set_role(USER_ID, "user")
    await b._notify_role_change(USER_ID, "user")
    dm2 = [m for m in app.bot.sent if m[0] == USER_ID]
    action("👥 quitar rol", dm2[-1][1].splitlines()[0] if dm2 else "(sin DM)", ok=True)


def show_help(b):
    print()
    print(color("  --- HELP POR ROL (lo que muestra /start) ---", f"{BOLD};{CYAN}"))
    user_help = b.help_for_role("user")
    print(f"  user : {color(user_help if user_help else '(vacio, no ve comandos)', GRAY)}")
    print(f"  dj   : {color(b.help_for_role('dj'), YELLOW)}")
    print(f"  admin: {color(b.help_for_role('admin'), GREEN)}")


def show_summary():
    print()
    print(color("  --- RESUMEN DE PERMISOS ---", f"{BOLD};{CYAN}"))
    rows = [
        ("Accion", "sin rol", "dj", "admin"),
        ("-", "-", "-", "-"),
        ("/start", "aviso", "si", "si"),
        ("/buscar", "NO", "si", "si"),
        ("👥 habilitar/quitar", "DM del bot", "NO", "si"),
        ("boton ⏯⏭⏮⏹🔊", "NO", "si", "si"),
        ("boton 📋", "NO", "si", "si"),
    ]
    print()
    for ri, row in enumerate(rows):
        cells = []
        for ci, cell in enumerate(row):
            text = cell
            if ci == 0:
                text = color(cell, BOLD)
            elif cell == "si":
                text = color(" si ", f"{BOLD};42") if ci > 0 else cell
            elif cell == "NO":
                text = color(" NO ", f"{BOLD};41") if ci > 0 else cell
            elif "toast" in cell:
                text = color("toast", f"{BOLD};43")
            cells.append(text.ljust(15 if ci == 0 else 12))
        print(f"  {('| ' + ' | '.join(cells) + ' |')}")


async def run_demo():
    b = make_bot()
    b.config.allowed_chat_id = 44
    app = b._app
    b.roles.set_role(USER_ID, "user")
    b.roles.set_role(DJ_ID, "dj")
    b.roles.set_role(ADMIN_ID, "admin")

    print()
    print(color("  YT-Remote — Demo del modelo de roles", f"{BOLD};{BLUE}"))
    print(color("  user=10  dj=20  admin=30  (todos en la misma sesion, aislados)", GRAY))
    print(color("  " + "=" * 64, GRAY))

    await show_user(b)
    await show_dj(b, app)
    await show_admin(b)
    show_help(b)
    show_summary()

    print()
    print(color("  Demo terminada.", GRAY))


if __name__ == "__main__":
    asyncio.run(run_demo())
