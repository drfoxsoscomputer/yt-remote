"""Tests de src/persistence.py: StateStore con escritura atomica y versionado.

Cubre: round-trip, archivo faltante, JSON corrupto, version desconocida,
atomico (no quedan .tmp), truncado a 100 items, debouncing y flush.
"""
import json
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from testkit import run_sync_tests  # noqa: E402

from persistence import StateStore, CURRENT_VERSION, MAX_HISTORY  # noqa: E402


def make_store() -> StateStore:
    """Crea un StateStore con un archivo temporal, sin tocar data/."""
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    return StateStore(tmp)


def test_una_version_mas_nueva_NO_borra_la_lista_del_usuario():
    """El defecto que mas dano hacia: data/state.json en una version mas nueva
    (por ejemplo, corriste una build mas nueva y despues volviste a esta).

    Antes: se usaban valores por defecto y, en el primer guardado, el archivo
    se SOBRESCRIBIA. La lista de canciones, el historial y el volumen del
    usuario desaparecian para siempre y sin ningun aviso.

    Ahora: arranca con defaults, deja una copia del original y se NIEGA a
    escribir. Los datos quedan intactos.
    """
    s = make_store()
    futuro = {
        "version": CURRENT_VERSION + 5,
        "volume": 42,
        "paused": True,
        "current": {"url": "u1", "title": "Mi Cancion"},
        "playlist": [{"url": "u1", "title": "Mi Cancion"}, {"url": "u2", "title": "Otra"}],
        "cursor": 0,
        "list_page": 0,
        "history": [{"url": "u9", "title": "Vieja"}],
        "radio_artist": "GP Band",
        "max_height": 720,
    }
    s.path.write_text(json.dumps(futuro), encoding="utf-8")

    estado = s.load()

    # Arranca con defaults para poder arrancar...
    assert estado["playlist"] == [], estado
    # ...pero NUNCA escribe encima.
    s.save(estado)
    s.save({"version": CURRENT_VERSION, "playlist": [{"url": "x", "title": " intrusion"}]})

    # El archivo original tiene que seguir intacto, con TODO lo suyo.
    lo_que_quedo = json.loads(s.path.read_text(encoding="utf-8"))
    assert lo_que_quedo == futuro, (
        f"el archivo del usuario fue modificado: {lo_que_quedo}"
    )
    assert lo_que_quedo["radio_artist"] == "GP Band", lo_que_quedo
    assert len(lo_que_quedo["playlist"]) == 2, lo_que_quedo
    # Y tiene que haber una copia para revisarla a mano.
    copias = list(s.path.parent.glob("state.json.intacto-*"))
    assert copias, "no se dejo ninguna copia del archivo que no se pudo leer"
    assert json.loads(copias[0].read_text(encoding="utf-8")) == futuro, copias


def test_un_archivo_corrupto_no_se_sobrescribe():
    """JSON roto: mismo trato. No se pisa, se copia y se avisa."""
    s = make_store()
    roto = '{"playlist": [{"url": "u1", "tit'
    s.path.write_text(roto, encoding="utf-8")

    estado = s.load()
    assert estado["playlist"] == [], estado

    s.save(estado)

    assert s.path.read_text(encoding="utf-8") == roto, (
        "el archivo corrupto fue sobrescrito: se perdio la evidencia"
    )
    copias = list(s.path.parent.glob("state.json.intacto-*"))
    assert copias, "no se dejo copia del archivo corrupto"
    assert copias[0].read_text(encoding="utf-8") == roto, copias


def test_una_version_mas_nueva_en_otro_idioma_tampoco_pisa():
    """Si la "version" ni siquiera es un numero, tampoco se pisa el archivo."""
    s = make_store()
    raro = {"version": "mañana", "playlist": [{"url": "u1", "title": "Mi Cancion"}]}
    s.path.write_text(json.dumps(raro), encoding="utf-8")

    s.load()
    s.save({"version": CURRENT_VERSION, "playlist": []})

    assert json.loads(s.path.read_text(encoding="utf-8")) == raro, (
        "un archivo con version desconocida fue sobrescrito"
    )


def test_el_uso_normal_sigue_guardando():
    """Lo de arriba es para el caso raro: el camino normal tiene que seguir
    escribiendo con normalidad, o el bot no recordaria nada."""
    s = make_store()
    s.load()
    s.save({"playlist": [{"url": "u1", "title": "Mi Cancion"}], "radio_artist": "GP Band"})

    guardado = json.loads(s.path.read_text(encoding="utf-8"))
    assert guardado["playlist"][0]["url"] == "u1", guardado
    assert guardado["radio_artist"] == "GP Band", guardado
    assert guardado["version"] == CURRENT_VERSION, guardado


def test_el_token_de_telegram_no_se_queda_en_el_log():
    """El token viaja en la URL de la API y httpx la registra completa.

    Se encontraron 1938 apariciones del token real en data/bot.log del
    portable. Un log de texto plano con la credencial adentro es una credencial
    tirada en el piso.

    El token de esta prueba se ARMA en runtime a proposito: escrito en el fuente
    con esa forma, el escaner de credenciales del pre-commit lo tomaria por un
    token real y abortaria el commit. Es lo mismo que paso al escribirlo fijo.
    """
    import tempfile as _tf

    import bot_process

    id_falso = "1234567890"
    cuerpo_falso = "".join(c * 2 for c in "ABCDEFGHIJ")
    token_falso = f"{id_falso}:{cuerpo_falso}"
    linea = (
        "2026-09-13 20:10:50 - httpx - INFO - HTTP Request: POST "
        f"https://api.telegram.org/bot{token_falso}/getMe \"HTTP/1.1 200 OK\""
    )

    limpio = bot_process._ocultar_secretos(linea)
    assert token_falso not in limpio, limpio
    assert id_falso not in limpio, limpio
    assert "TOKEN-OCULTO" in limpio, limpio
    # El valor diagnostico se conserva: el metodo y la ruta.
    assert "POST" in limpio and "/getMe" in limpio, limpio

    # Y de verdad no llega al archivo.
    tmp = Path(_tf.mkdtemp())
    original = bot_process.data_dir
    bot_process.data_dir = lambda: tmp
    try:
        proc = bot_process.BotProcess()
        proc._append_log(linea)
        escrito = (tmp / "bot.log").read_text(encoding="utf-8")
    finally:
        bot_process.data_dir = original
    assert token_falso not in escrito, escrito
    assert "TOKEN-OCULTO" in escrito, escrito


def test_defaults_when_file_missing():
    s = make_store()
    state = s.load()
    assert state["version"] == CURRENT_VERSION
    assert state["volume"] == 100
    assert state["paused"] is False
    assert state["current"] is None
    assert state["playlist"] == []
    assert state["history"] == []
    assert state["radio_artist"] == ""
    print("  OK  test_defaults_when_file_missing")


def test_round_trip_basic():
    s = make_store()
    state = {
        "version": 1,
        "volume": 80,
        "paused": True,
        "current": None,
        "playlist": [],
        "history": [],
        "radio_artist": "GP Band",
    }
    s.save(state)
    loaded = s.load()
    assert loaded["volume"] == 80
    assert loaded["paused"] is True
    assert loaded["radio_artist"] == "GP Band"
    print("  OK  test_round_trip_basic")


def test_round_trip_complex():
    s = make_store()
    state = {
        "version": 1,
        "volume": 65,
        "paused": False,
        "current": {
            "url": "u1",
            "title": "Inexplicable",
            "duration_seconds": 200,
            "thumbnail": "https://x",
            "channel": "GP Band",
        },
        "playlist": [
            {"url": "u2", "title": "A", "duration_seconds": 0},
            {"url": "u3", "title": "B", "duration_seconds": 0},
        ],
        "history": [{"url": "u0", "title": "Z"}],
        "radio_artist": "Mafe Restrepo",
    }
    s.save(state)
    loaded = s.load()
    assert loaded["current"]["title"] == "Inexplicable"
    assert loaded["playlist"][0]["url"] == "u2"
    assert loaded["history"][0]["url"] == "u0"
    assert loaded["radio_artist"] == "Mafe Restrepo"
    print("  OK  test_round_trip_complex")


def test_corrupt_json_returns_defaults():
    s = make_store()
    s.path.write_text("{ esto no es json valido ", encoding="utf-8")
    state = s.load()
    assert state["version"] == CURRENT_VERSION
    assert state["volume"] == 100
    print("  OK  test_corrupt_json_returns_defaults")


def test_truncated_json_returns_defaults():
    s = make_store()
    s.path.write_text('{"version": 1, "volume": 80, "playlist": [', encoding="utf-8")
    state = s.load()
    assert state["volume"] == 100, "estado corrupto deberia caer a defaults"
    print("  OK  test_truncated_json_returns_defaults")


def test_unknown_version_returns_defaults():
    s = make_store()
    s.path.write_text('{"version": 99, "volume": 50}', encoding="utf-8")
    state = s.load()
    assert state["volume"] == 100, "version futura debe caer a defaults"
    print("  OK  test_unknown_version_returns_defaults")


def test_save_creates_parent_dir():
    tmp = Path(tempfile.mkdtemp()) / "subdir" / "state.json"
    s = StateStore(tmp)
    s.save({"version": 1, "volume": 50})
    assert tmp.exists()
    print("  OK  test_save_creates_parent_dir")


def test_atomic_no_leftover_tmp():
    s = make_store()
    s.save({"version": 1, "volume": 70})
    assert s.path.exists()
    assert not s.path.with_suffix(".json.tmp").exists()
    print("  OK  test_atomic_no_leftover_tmp")


def test_history_truncated_to_max():
    s = make_store()
    huge = [{"url": f"u{i}", "title": f"T{i}"} for i in range(500)]
    s.save({"version": 1, "history": huge})
    loaded = s.load()
    assert len(loaded["history"]) == MAX_HISTORY
    assert loaded["history"][-1]["url"] == "u499"
    print("  OK  test_history_truncated_to_max")


def test_save_overwrites_previous():
    s = make_store()
    s.save({"version": 1, "volume": 30, "radio_artist": "A"})
    s.save({"version": 1, "volume": 90, "radio_artist": "B"})
    loaded = s.load()
    assert loaded["volume"] == 90
    assert loaded["radio_artist"] == "B"
    print("  OK  test_save_overwrites_previous")


def test_mark_dirty_and_flush_cycle():
    s = make_store()
    s.load()
    s._current = {"version": 1, "volume": 50}
    s.mark_dirty()
    assert s._dirty is True
    s.flush()
    assert s._dirty is False
    loaded = s.load()
    assert loaded["volume"] == 50
    print("  OK  test_mark_dirty_and_flush_cycle")


def test_mark_dirty_creates_flush_timer():
    """mark_dirty programa un timer cuando hay un loop activo."""
    import asyncio
    s = make_store()
    s.load()
    s._current = {"version": 1, "volume": 60}

    async def run():
        s._loop = asyncio.get_event_loop()
        s.mark_dirty()
        return s._flush_timer

    timer = asyncio.run(run())
    assert timer is not None, "mark_dirty debe programar timer con loop activo"
    timer.cancel()
    print("  OK  test_mark_dirty_creates_flush_timer")


def test_mark_dirty_no_timer_without_loop():
    """Sin loop activo, mark_dirty no rompe y queda dirty=True."""
    s = make_store()
    s.load()
    s._current = {"version": 1, "volume": 60}
    s._loop = None
    s.mark_dirty()
    assert s._dirty is True
    assert s._flush_timer is None
    print("  OK  test_mark_dirty_no_timer_without_loop")


def test_save_unicode_preserved():
    s = make_store()
    s.save({"version": 1, "radio_artist": "Acentos: ñ á é í"})
    loaded = s.load()
    assert loaded["radio_artist"] == "Acentos: ñ á é í"
    print("  OK  test_save_unicode_preserved")


def test_save_partial_state_with_defaults():
    s = make_store()
    s.save({"version": 1, "volume": 42})
    loaded = s.load()
    assert loaded["volume"] == 42
    assert loaded["paused"] is False
    assert loaded["current"] is None
    print("  OK  test_save_partial_state_with_defaults")


def test_defaults_for_cursor_and_list_page():
    s = make_store()
    state = s.load()
    assert state["cursor"] == 0
    assert state["list_page"] == 0
    print("  OK  test_defaults_for_cursor_and_list_page")


def test_round_trip_cursor_and_list_page():
    s = make_store()
    s.save({"version": 1, "cursor": 7, "list_page": 2})
    loaded = s.load()
    assert loaded["cursor"] == 7
    assert loaded["list_page"] == 2
    print("  OK  test_round_trip_cursor_and_list_page")


def run():
    print("\n=== Tests de persistence ===\n")
    total = run_sync_tests(globals())
    print(f"\nPERSISTENCE TESTS OK ({total} pruebas)")


if __name__ == "__main__":
    run()
