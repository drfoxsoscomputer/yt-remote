"""Las garantias que el producto le debe al usuario, escritas como pruebas.

Estas pruebas describen lo que se VE, no variables internas. estan en rojo a
proposito: describen el defecto antes de arreglarlo. Cada arreglo se acepta
cuando la pone en verde, y si un cambio futuro la rompe, la suite avisa sola.

Garantia 1: la tarjeta del reproductor es SIEMPRE el ultimo mensaje del grupo,
             aunque el bot mande un aviso, un error o entre alguien nuevo.
Garantia 2: la lista de canciones y el historial NO se pierden por una busqueda
             que no termina de sonar. Solo se reemplazan cuando la cancion
             elegida arranca de verdad.
Garantia 3: lo que se compila es lo que se escribio. Vive en test_build_sync.py.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from queue_manager import QueueItem  # noqa: E402
from search import SearchResult  # noqa: E402
from test_card import (  # noqa: E402
    FakeMessageUpdate,
    FakeUpdate,
    _FakeBacklogUpd,
    make_bot,
)
from testkit import run_sync_tests  # noqa: E402


def _ultimo_mensaje_del_bot(bot) -> int:
    """Id del ultimo mensaje que envio el bot. El fake los numera en orden,
    asi que el ultimo enviado es el unico que puede ser la tarjeta."""
    return len(bot.sent) + 99


async def test_garantia_tarjeta_al_final_tras_bienvenida_de_nuevo_miembro():
    """Entra alguien al grupo: el bot saluda, y la tarjeta sigue abajo."""
    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    b._app.bot.sent.clear()

    member = SimpleNamespace(id=55, full_name="Nueva", username="nueva_p", first_name="Nueva")
    upd = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=44), new_chat_members=[member]),
        effective_user=member,
        callback_query=None,
    )
    await b._on_new_members(upd, b._app)

    assert b._app.bot.sent, "el bot debe saludar al nuevo miembro"
    assert b._card_message_id == _ultimo_mensaje_del_bot(b._app.bot), (
        f"la tarjeta quedo arriba del saludo: card={b._card_message_id}, "
        f"ultimo mensaje={_ultimo_mensaje_del_bot(b._app.bot)}"
    )


async def test_garantia_tarjeta_al_final_tras_aviso_de_red():
    """Se cae la conexion y vuelve: el bot avisa, y la tarjeta sigue abajo."""
    b = make_bot()
    b.config.allowed_chat_id = 44
    b._card_chat_id = 44
    b._card_message_id = 7
    fb = b._app.bot
    fb.sent.clear()
    fb.pending_updates = [_FakeBacklogUpd(44, "/pause")]
    ctx = SimpleNamespace(bot=fb, application=SimpleNamespace(updater=None))
    b._net_offline = True
    fb.get_me_ok = True

    await b._net_watch_job(ctx)

    assert any("La conexion del bot se perdio" in str(m[1]) for m in fb.sent), fb.sent
    assert b._card_message_id == _ultimo_mensaje_del_bot(fb), (
        f"la tarjeta quedo arriba del aviso: card={b._card_message_id}, "
        f"ultimo mensaje={_ultimo_mensaje_del_bot(fb)}"
    )


async def test_garantia_tarjeta_al_final_tras_aviso_de_rol():
    """Cambia el rol de alguien y el privado no se entrega: el bot avisa en el
    grupo, y la tarjeta sigue abajo."""
    b = make_bot()
    b.config.allowed_chat_id = 44
    b._card_chat_id = 44
    b._card_message_id = 7
    fb = b._app.bot
    fb.sent.clear()
    original = fb.send_message

    async def solo_grupo(chat_id, text, **kwargs):
        if chat_id != 44:
            raise RuntimeError("privado no entregado")
        return await original(chat_id, text, **kwargs)

    fb.send_message = solo_grupo
    await b._dm_or_group(55, "Ahora eres DJ")

    assert any("Ahora eres DJ" in str(m[1]) for m in fb.sent), fb.sent
    assert b._card_message_id == _ultimo_mensaje_del_bot(fb), (
        f"la tarjeta quedo arriba del aviso: card={b._card_message_id}, "
        f"ultimo mensaje={_ultimo_mensaje_del_bot(fb)}"
    )


async def test_garantia_tarjeta_al_final_tras_un_comando():
    """Control: un comando ya reposiciona la tarjeta. No debe romperse."""
    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    b._app.bot.sent.clear()

    async def handler(update, context):
        await b._app.bot.send_message(44, "respuesta del comando")

    await b._with_card_reposition(handler)(FakeMessageUpdate("/x"), SimpleNamespace(args=[]))

    assert b._card_message_id == _ultimo_mensaje_del_bot(b._app.bot), (
        f"la tarjeta quedo arriba de la respuesta: card={b._card_message_id}"
    )


async def test_garantia_lista_sobrevive_si_el_pick_no_reproduce():
    """Elegis un resultado de /buscar pero no arranca: la lista de antes y el
    artista de la radio quedan como estaban, la tarjeta no se borra, la cancion
    que sonaba sigue sonando, y /prev todavia te devuelve la anterior."""
    b = make_bot()
    b._card_chat_id = 44
    b._card_message_id = 7
    b.queue.set_playlist(
        [
            QueueItem(url="v1", title="Antes 1"),
            QueueItem(url="v2", title="Antes 2"),
            QueueItem(url="v3", title="Antes 3"),
        ]
    )
    b.queue.jump_to(2)
    b._radio_artist = "GP Band"
    b._search_artist = "Otro Artista"
    b._search_cache["pick:0"] = SearchResult(
        url="nuevo", title="Nuevo", duration="3:00", thumbnail=""
    )

    pedidos: list[str] = []

    async def play_falla(update, item, **kwargs):
        pedidos.append(item.url)
        return False

    async def no_se_resuelve(url):
        return None

    b._play_item = play_falla
    b._stream_for = no_se_resuelve
    await b.on_callback(FakeUpdate("pick:0", message_id=500, chat_id=44), SimpleNamespace(args=[]))

    assert b._card_message_id == 7, "la tarjeta no se borra si no se reprodujo"
    assert [i.url for i in b.queue.all()] == ["v1", "v2", "v3"], (
        f"se perdio la lista anterior: {[i.url for i in b.queue.all()]}"
    )
    assert b._radio_artist == "GP Band", f"se perdio el artista de la radio: {b._radio_artist!r}"
    assert b.queue.current is not None and b.queue.current.url == "v2", (
        "dejo de sonar la cancion que estaba sonando"
    )

    pedidos.clear()
    await b.cmd_prev(FakeMessageUpdate("/prev"), SimpleNamespace(args=[]))
    assert pedidos == ["v1"], f"/prev no devolvio la cancion anterior: {pedidos}"


async def test_garantia_lista_sobrevive_si_una_playlist_no_reproduce():
    """Pegás el link de una playlist y el primer tema no arranca: la lista de
    antes y el artista de la radio quedan como estaban."""
    import bot as bot_mod

    b = make_bot()
    b.queue.set_playlist([QueueItem(url="v1", title="Antes")])
    b._radio_artist = "GP Band"

    original_pl = bot_mod.is_playlist_url
    original_quick = bot_mod.quick_playlist
    original_expand = bot_mod.expand_playlist
    bot_mod.is_playlist_url = lambda q: True
    bot_mod.quick_playlist = lambda q, n: (
        [
            SearchResult(url="p1", title="P1", duration="3:00", thumbnail=""),
            SearchResult(url="p2", title="P2", duration="3:00", thumbnail=""),
        ],
        2,
    )
    bot_mod.expand_playlist = lambda q, n: []

    async def play_falla(update, item, **kwargs):
        return False

    b._play_item = play_falla
    link = "https://youtube.com/playlist?list=XYZ"
    try:
        await b._play_link_or_playlist(
            FakeMessageUpdate(link), SimpleNamespace(args=[]), link
        )
    finally:
        b._cancel_playlist_expansion()
        bot_mod.is_playlist_url = original_pl
        bot_mod.quick_playlist = original_quick
        bot_mod.expand_playlist = original_expand

    assert [i.url for i in b.queue.all()] == ["v1"], (
        f"se perdio la lista anterior: {[i.url for i in b.queue.all()]}"
    )
    assert b._radio_artist == "GP Band", f"se perdio el artista de la radio: {b._radio_artist!r}"


async def test_garantia_la_radio_sigue_al_artista_de_lo_que_suena():
    """Buscaste X, luego Y, y con /prev volviste a una de X: la siguiente
    canción tiene que ser de X. El ancla de la sesión quedó en Y, pero manda
    el artista del tema que está sonando realmente."""
    b = make_bot()
    b._radio_artist = "Y"
    b.queue.set_current(
        QueueItem(url="x1", title="X - Cancion A", channel="CanalX", artist="X")
    )
    semilla = b._artist_seed(b.queue.current)
    assert semilla == "X", f"la radio no siguio al tema real: {semilla!r}"


def run():
    total = run_sync_tests(globals())
    print(f"GARANTIAS TESTS OK ({total} pruebas)")


if __name__ == "__main__":
    run()
