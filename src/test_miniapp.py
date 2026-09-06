"""Tests de integracion de la Web App (tarea 6/6: mini app).

Monta el servidor HTTP real (MiniAppServer) con un bot de prueba
(Player y bot de Telegram fake) y dispara POST /api con initData
firmado para los 3 modos: search, roles y queue.

Sin red ni mpv: search() y _stream_for() se stubbean.
"""

import asyncio
import hashlib
import hmac
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import bot as bot_mod
from mini_app import MiniAppServer, derive_validation_key


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=100 + len(self.sent))


class FakePlayer:
    is_running = False
    starts = 0

    async def start(self):
        self.starts += 1
        self.is_running = True

    async def load(self, url, audio=None):
        pass

    async def play(self):
        pass

    async def pause(self):
        pass

    async def stop(self):
        self.is_running = False


class FakeApp:
    def __init__(self):
        self.bot = FakeBot()


def make_bot(owner_id=1):
    player = FakePlayer()
    bot_mod.Player = lambda *a, **k: player
    config = SimpleNamespace(
        mpv_path="mpv",
        owner_id=owner_id,
        allowed_chat_id=44,
        max_results=5,
        mini_app_port=8765,
        token="TOKEN_PRUEBA",
    )
    b = bot_mod.YTRemoteBot(config)
    b._app = FakeApp()
    b.player = player
    b.roles._roles = {"1": "admin", "2": "dj", "3": "user"}
    b.roles._save = lambda: None
    return b


def build_initdata(uid, token="TOKEN_PRUEBA"):
    secret = derive_validation_key(token)
    user_raw = json.dumps({"id": uid, "first_name": "Juan", "username": "u%d" % uid})
    user = urllib.parse.quote(user_raw, safe="")
    data = "auth_date=1600000000&user=" + user
    pairs = sorted(urllib.parse.parse_qsl(data))
    check = "\n".join(f"{k}={v}" for k, v in pairs)
    return data + "&hash=" + hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


async def post(session, payload, token="TOKEN_PRUEBA"):
    import urllib.error

    def _do():
        req = urllib.request.Request(
            f"{session}/api",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    return await asyncio.to_thread(_do)


async def main():
    import search as search_mod

    fake_results = [
        search_mod.SearchResult(
            url="https://youtu.be/aaa111bbb22",
            title="Tema test 1",
            duration="3:45",
            thumbnail="",
            duration_seconds=225,
            channel="Canal X",
        ),
        search_mod.SearchResult(
            url="https://youtu.be/ccc333ddd44",
            title="Tema test 2",
            duration="2:10",
            thumbnail="",
            duration_seconds=130,
            channel="Canal Y",
        ),
    ]

    b = make_bot()
    search_orig = bot_mod.search
    bot_mod.search = lambda query, max_results: fake_results
    async def _stream_for(url):
        return ("https://stream/1", None)
    b._stream_for = _stream_for

    async def on_api(data):
        return await b._on_api(data)

    server = MiniAppServer("TOKEN_PRUEBA", port=8982, on_api=on_api, loop=asyncio.get_running_loop())
    server.start()
    base = "http://127.0.0.1:8982"

    # GET / sirve el HTML
    import urllib.request as ur

    with ur.urlopen(base + "/?view=roles", timeout=5) as resp:
        html = resp.read().decode()
    assert "view-queue" in html and "view-roles" in html and "view-search" in html
    print("GET / html OK", len(html))

    # Sin firma -> 401
    code, body = await post(base, {"initData": "", "mode": "search", "payload": {}})
    assert code == 401, (code, body)
    print("401 sin firma OK")

    # Modo desconocido
    code, body = await post(base, {
        "initData": build_initdata(3), "mode": "nope", "payload": {}
    })
    assert code == 200 and not body["ok"]
    print("modo desconocido OK")

    # SEARCH: usuario 3 (user) busca artista + cancion; publica listado en el chat
    b._app.bot.sent.clear()
    code, body = await post(base, {
        "initData": build_initdata(3),
        "mode": "search",
        "payload": {"artist": "GP Band", "song": "Sin Remedio"},
    })
    assert code == 200 and body["ok"], body
    sent_choose = [s for s in b._app.bot.sent if s[1] == "Elige un video:"]
    assert sent_choose, b._app.bot.sent
    kb = sent_choose[-1][2].get("reply_markup")
    assert kb and len(kb.inline_keyboard) == 2, kb
    assert "1. Tema test 1" in kb.inline_keyboard[0][0].text
    print("search OK: listado publicado con pick buttons")
    # search incompleto -> error
    code, body = await post(base, {
        "initData": build_initdata(3),
        "mode": "search",
        "payload": {"artist": "GP", "song": ""},
    })
    assert not body["ok"]
    print("search incompleto OK")

    # ROLES: admin lista y edita; user no puede
    code, body = await post(base, {
        "initData": build_initdata(1), "mode": "roles", "payload": {"action": "list"}
    })
    assert code == 200 and body["ok"]
    assert {u["user_id"]: u["role"] for u in body["users"]} == {1: "admin", 2: "dj", 3: "user"}
    print("roles list OK")

    code, body = await post(base, {
        "initData": build_initdata(1),
        "mode": "roles",
        "payload": {"action": "set", "user_id": 3, "role": "dj"},
    })
    assert body["ok"]
    assert b.roles.get_role(3) == "dj"
    code, body = await post(base, {
        "initData": build_initdata(3),
        "mode": "roles",
        "payload": {"action": "list"},
    })
    assert not body["ok"] and "administrador" in body["error"]
    print("roles set + bloqueo a user OK")

    # QUEUE: list para cualquiera; editar solo dj/admin
    b.queue.set_playlist([
        SimpleNamespace(url="u1", title="Tema A"),
        SimpleNamespace(url="u2", title="Tema B"),
        SimpleNamespace(url="u3", title="Tema C"),
    ])
    code, body = await post(base, {
        "initData": build_initdata(4), "mode": "queue", "payload": {"action": "list"}
    })
    assert body["ok"] and len(body["items"]) == 3 and body["items"][0]["title"] == "Tema A"
    print("queue list OK")

    code, body = await post(base, {
        "initData": build_initdata(4),
        "mode": "queue",
        "payload": {"action": "remove", "position": 2},
    })
    assert not body["ok"] and "DJ" in body["error"]
    print("queue edit bloqueada a user OK")

    code, body = await post(base, {
        "initData": build_initdata(2),
        "mode": "queue",
        "payload": {"action": "remove", "position": 2},
    })
    assert body["ok"] and body["title"] == "Tema B"
    assert [x.title for x in b.queue.all()] == ["Tema A", "Tema C"]
    print("queue remove por dj OK")

    code, body = await post(base, {
        "initData": build_initdata(2),
        "mode": "queue",
        "payload": {"action": "jump", "position": 2},
    })
    assert body["ok"]
    assert b.queue.current.title == "Tema C"
    print("queue jump OK")

    code, body = await post(base, {
        "initData": build_initdata(1),
        "mode": "queue",
        "payload": {"action": "clear"},
    })
    assert body["ok"]
    assert b.queue.all() == [] and b.queue.current is None
    print("queue clear OK")

    print("\nWEB APP INTEGRATION OK")


if __name__ == "__main__":
    asyncio.run(main())