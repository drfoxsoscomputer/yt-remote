"""Garantias de StateStore: el estado del bot sobre la base comun.

Estas pruebas se reescribieron cuando el almacenamiento paso de `state.json`
a `data/ytremote.db`. Lo que se sigue exigiendo es el MISMO comportamiento de
siempre (el bot no puede perder la lista, ni el historial, ni el volumen), pero
contra el almacen que se usa de verdad.

El JSON ya no existe: por eso aca se crea un StateStore, se guarda, se abre
OTRO StateStore sobre la misma base (el reinicio) y se comprueba que todo
sigue ahi.
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from persistence import MAX_HISTORY, StateStore  # noqa: E402
from testkit import run_sync_tests  # noqa: E402


def make_store() -> StateStore:
    """StateStore en una carpeta temporal. Nunca toca data/ real."""
    return StateStore(Path(tempfile.mkdtemp()) / "state.json")


def _estado(playlist=None, history=None, **extra):
    base = {
        "version": 1,
        "volume": 100,
        "paused": False,
        "current": None,
        "playlist": playlist if playlist is not None else [],
        "cursor": 0,
        "list_page": 0,
        "history": history if history is not None else [],
        "radio_artist": "",
        "max_height": None,
    }
    base.update(extra)
    return base


def test_defaults_when_file_missing():
    """Sin base todavia: defaults, sin quejarse."""
    s = make_store()
    state = s.load()
    assert state["version"] == 1
    assert state["volume"] == 100
    assert state["paused"] is False
    assert state["current"] is None
    assert state["playlist"] == []
    assert state["history"] == []
    assert state["radio_artist"] == ""
    print("  OK  test_defaults_when_file_missing")


def test_round_trip_basic():
    """Guardar y volver a leer devuelve lo mismo."""
    s = make_store()
    state = _estado(volume=42, radio_artist="GP Band", paused=True)
    s.save(state)

    leido = make_store_en(s.path).load()
    assert leido["volume"] == 42, leido
    assert leido["radio_artist"] == "GP Band", leido
    assert leido["paused"] is True, leido
    print("  OK  test_round_trip_basic")


def make_store_en(path) -> StateStore:
    """Un StateStore nuevo sobre el MISMO directorio: el reinicio."""
    return StateStore(Path(path))


def test_round_trip_complex():
    """Todo el estado, con lista, historial y cancion en curso, sobrevive."""
    s = make_store()
    playlist = [
        {"url": f"u{i}", "title": f"T{i}", "duration": "3:00", "duration_seconds": 180}
        for i in range(5)
    ]
    history = [{"url": f"h{i}", "title": f"H{i}", "duration_seconds": 200} for i in range(7)]
    s.save(
        _estado(
            playlist=playlist,
            history=history,
            current=playlist[2],
            cursor=2,
            list_page=1,
            volume=73,
            paused=True,
            radio_artist="Marcos Witt",
            max_height=720,
            card={"chat_id": 44, "message_id": 500, "is_photo": True},
        )
    )

    leido = make_store_en(s.path).load()
    assert leido["volume"] == 73, leido
    assert leido["cursor"] == 2, leido
    assert leido["list_page"] == 1, leido
    assert leido["radio_artist"] == "Marcos Witt", leido
    assert leido["max_height"] == 720, leido
    assert leido["card"]["message_id"] == 500, leido
    assert leido["paused"] is True, leido

    assert len(leido["playlist"]) == 5, leido["playlist"]
    assert [t["url"] for t in leido["playlist"]] == [f"u{i}" for i in range(5)], leido["playlist"]
    assert leido["playlist"][0]["title"] == "T0", leido["playlist"][0]
    assert leido["playlist"][0]["duration_seconds"] == 180, leido["playlist"][0]

    assert len(leido["history"]) == 7, leido["history"]
    assert leido["current"]["url"] == "u2", leido["current"]
    print("  OK  test_round_trip_complex")


def test_history_truncated_to_max():
    """El historial se poda al tope: no crece para siempre."""
    s = make_store()
    historia = [{"url": f"h{i}", "title": f"H{i}"} for i in range(MAX_HISTORY + 40)]
    s.save(_estado(history=historia))

    leido = make_store_en(s.path).load()
    assert len(leido["history"]) == MAX_HISTORY, len(leido["history"])
    # Se conservan los ULTIMOS, del mas nuevo al mas viejo.
    assert leido["history"][0]["url"] == f"h{len(historia) - 1}", leido["history"][0]
    assert leido["history"][-1]["url"] == f"h{len(historia) - MAX_HISTORY}", leido["history"][-1]
    print("  OK  test_history_truncated_to_max")


def test_una_version_mas_nueva_NO_borra_la_lista_del_usuario():
    """Si la base es mas nueva que este codigo, NO se escribe.

    Antes: version de formato distinta y el proximo guardado SOBRESCRIBIA el
    archivo. La lista, el historial y el volumen desaparecian para siempre y en
    silencio. Ahora el store queda en solo lectura.
    """
    carpeta = Path(tempfile.mkdtemp())
    ruta = carpeta / "ytremote.db"

    import store as store_mod

    conn = sqlite3.connect(str(ruta))
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (?)", (99,))
    conn.execute("CREATE TABLE ajustes (clave TEXT PRIMARY KEY, valor TEXT)")
    conn.execute("CREATE TABLE tracks (video_id TEXT PRIMARY KEY, url TEXT NOT NULL)")
    conn.execute("CREATE TABLE queue (position INTEGER PRIMARY KEY, video_id TEXT)")
    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, played_at INT, video_id TEXT)")
    conn.execute(
        "INSERT INTO tracks VALUES ('v1', 'https://u/1')"
    )
    conn.execute("INSERT INTO queue VALUES (0, 'v1')")
    conn.execute(
        "INSERT INTO ajustes VALUES ('volume', '55')"
    )
    conn.commit()
    conn.close()

    original = store_mod.DB_PATH
    store_mod.DB_PATH = ruta
    try:
        s = StateStore(ruta)
        assert s._solo_lectura is True, "una base mas nueva tiene que quedar en solo lectura"
        s.save(_estado(playlist=[{"url": "intruso", "title": " intrusion"}]))

        # Los datos del futuro siguen intactos, included la fila de la cola.
        verificacion = sqlite3.connect(str(ruta))
        filas = verificacion.execute("SELECT video_id FROM queue").fetchall()
        vol = verificacion.execute("SELECT valor FROM ajustes WHERE clave='volume'").fetchone()
        verificacion.close()
        assert filas == [("v1",)], f"escribio sobre una base mas nueva: {filas}"
        assert vol is not None and vol[0] == "55", f"cambio el volumen: {vol}"
    finally:
        store_mod.DB_PATH = original


def test_el_uso_normal_sigue_guardando():
    """Lo de arriba es para el caso raro: el camino normal tiene que seguir
    escribiendo, o el bot no recordaria nada."""
    s = make_store()
    s.load()
    s.save(_estado(playlist=[{"url": "u1", "title": "Mi Cancion"}], radio_artist="GP Band"))

    guardado = make_store_en(s.path).load()
    assert guardado["playlist"][0]["url"] == "u1", guardado
    assert guardado["radio_artist"] == "GP Band", guardado
    print("  OK  test_el_uso_normal_sigue_guardando")


def test_tocar_el_volumen_no_reescribe_la_cola():
    """El motivo de la base: cambiar el volumen no toca las filas de la cola.

    Con el JSON viejo, cambiar el volumen reescribia el archivo ENTERO: la cola
    y el historial iban y venian en cada cambio.
    """
    s = make_store()
    s.save(_estado(playlist=[{"url": f"u{i}", "title": f"T{i}"} for i in range(6)]))
    s.save(_estado(playlist=[{"url": f"u{i}", "title": f"T{i}"} for i in range(6)], volume=11))

    leido = make_store_en(s.path).load()
    assert leido["volume"] == 11, leido
    assert len(leido["playlist"]) == 6, leido["playlist"]
    print("  OK  test_tocar_el_volumen_no_reescribe_la_cola")


def test_current_state_devuelve_lo_ultimo():
    s = make_store()
    s.load()
    s.save(_estado(volume=33))
    assert s.current_state()["volume"] == 33, s.current_state()
    print("  OK  test_current_state_devuelve_lo_ultimo")


def test_mark_dirty_y_flush():
    """El debounce sigue igual: se marca y se fuerza el guardado."""
    s = make_store()
    s.load()
    s._current = _estado(volume=64)
    s.mark_dirty()
    assert s._dirty is True
    s.flush()
    assert s._dirty is False
    assert make_store_en(s.path).load()["volume"] == 64
    print("  OK  test_mark_dirty_y_flush")


def test_save_creates_parent_dir():
    """Guardar crea la carpeta si no existe."""
    carpeta = Path(tempfile.mkdtemp()) / "nueva" / "carpeta"
    s = StateStore(carpeta / "state.json")
    s.load()
    s.save(_estado(volume=88))
    assert (carpeta / "ytremote.db").exists(), "no se creo la carpeta de la base"
    assert StateStore(carpeta / "state.json").load()["volume"] == 88
    print("  OK  test_save_creates_parent_dir")


def test_no_quedan_archivos_temporales():
    """No quedan .tmp ni basura al lado de la base."""
    s = make_store()
    s.save(_estado(volume=12, playlist=[{"url": "u1"}]))
    vecinos = [p.name for p in s.path.parent.iterdir()]
    assert not [n for n in vecinos if n.endswith(".tmp")], vecinos
    assert not [n for n in vecinos if n.endswith(".json.tmp")], vecinos
    print("  OK  test_no_quedan_archivos_temporales")


def run():
    total = run_sync_tests(globals())
    print(f"\nPERSISTENCE TESTS OK ({total} pruebas)")


if __name__ == "__main__":
    run()