"""Garantias del almacenamiento: esquema real y datos que no se pierden.

Tres cosas que el almacenamiento viejo hacia mal y que estas pruebas fijan:

1. No se borra nada al arrancar. `roles.py` hacia `DELETE FROM roles` en cada
   arranque y solo reimportaba de un `roles.json` que ya no existe: cada
   reinicio dejaba la tabla vacia y los roles se perdian en silencio.
2. Hay un esquema de verdad: tablas con columnas, indices y relaciones, no un
   JSON dentro de una columna.
3. Los datos viejos entran una sola vez y el archivo original NO se borra: queda
   renombrado con `.migrado`.
"""

import json
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from store import CLAVE_MIGRADO, SCHEMA_VERSION, Store  # noqa: E402


def _store() -> Store:
    """Store en una carpeta temporal. Aísla TAMBIÉN los archivos viejos.

    No hace falta tocar `LEGACY_STATE` / `LEGACY_ROLES`: el Store busca los
    archivos viejos en la CARPETA de su propia base. Antes sí había que
    parchearlos a mano, y ese parche estaba tapando el problema de verdad.
    """
    import store as store_mod

    carpeta = Path(tempfile.mkdtemp())
    original = (store_mod.data_dir, store_mod.DB_PATH)
    store_mod.data_dir = lambda: carpeta
    store_mod.DB_PATH = carpeta / "ytremote.db"
    s = store_mod.DB_PATH
    _RESTAURAR.append((store_mod, original))
    return Store(s)


# Cada _store() guarda lo que restaura; el ultimo test lo deja todo como estaba.
_RESTAURAR: list = []


def _restaurar_todo() -> None:
    import store as store_mod

    while _RESTAURAR:
        store_mod, original = _RESTAURAR.pop()
        store_mod.data_dir, store_mod.DB_PATH = original


def test_el_esquema_tiene_columnas_de_verdad():
    """No es un JSON dentro de una columna: son tablas con columnas."""
    s = _store()
    tablas = {
        f["name"] for f in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for t in ("tracks", "queue", "history", "usuarios", "ajustes", "schema_version"):
        assert t in tablas, f"falta la tabla {t}: {sorted(tablas)}"

    columnas = {f["name"] for f in s.conn.execute("PRAGMA table_info(tracks)")}
    for c in ("video_id", "url", "title", "duration_seconds", "thumbnail", "channel", "artist"):
        assert c in columnas, f"tracks no tiene la columna {c}: {sorted(columnas)}"

    # La version del esquema queda anotada (las migraciones se pueden aplicar).
    assert s.leer_ajuste is not None
    fila = s.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert fila[0] == SCHEMA_VERSION, f"version de esquema: {fila[0]}"
    s.cerrar()


def test_la_cola_sobrevive_a_cerrar_y_abrir_de_nuevo():
    """La lista se guarda de verdad, no se reescribe un JSON gigante."""
    s = _store()
    s.guardar_cola(
        [
            {"url": "u1", "video_id": "v1", "title": "Uno", "duration": "3:00", "channel": "A"},
            {"url": "u2", "video_id": "v2", "title": "Dos", "duration": "4:00", "channel": "B"},
        ]
    )
    s.cerrar()

    otra = Store(s.path)
    leida = otra.leer_cola()
    assert [t["url"] for t in leida] == ["u1", "u2"], leida
    assert leida[0]["title"] == "Uno" and leida[0]["channel"] == "A", leida
    otra.cerrar()


def test_reordenar_la_cola_no_duplica_ni_pierde_canciones():
    """Cambiar el orden borra las posiciones viejas, no las apila."""
    s = _store()
    s.guardar_cola([{"url": f"u{i}", "video_id": f"v{i}"} for i in range(5)])
    s.guardar_cola([{"url": "u9", "video_id": "v9"}, {"url": "u0", "video_id": "v0"}])
    leida = [t["url"] for t in s.leer_cola()]
    assert leida == ["u9", "u0"], leida
    s.cerrar()


def test_el_historial_se_poda_al_tope():
    """El historial no crece para siempre: es lo unico que se va llenando."""
    s = _store()
    for i in range(150):
        s.agregar_historial({"url": f"u{i}", "video_id": f"v{i}"}, cuando=1700000000 + i)
    s.podar_historial(100)
    leido = s.leer_historial(100)
    assert len(leido) == 100, len(leido)
    # Se conservan los ULTIMOS, no los primeros.
    assert leido[0]["url"] == "u149", leido[0]
    s.cerrar()


def test_los_usuarios_sobreviven_a_reabrir():
    """LA REGRESION PRINCIPAL: antes `roles.py` hacia DELETE en cada arranque,
    asi que reiniciar el bot borraba los roles. Aqui sobreviven."""
    s = _store()
    s.guardar_usuarios([(77, "Dueño", "admin", 1700000000), (88, "Dj", "dj", 1700000001)])
    s.cerrar()

    otra = Store(s.path)
    filas = {f["user_id"]: f for f in otra.leer_usuarios()}
    assert set(filas) == {77, 88}, filas
    assert filas[77]["role"] == "admin", filas
    assert filas[88]["name"] == "Dj", filas
    otra.cerrar()

    # Y otra vez mas, para probar que NO hay borrado en el arranque.
    tercera = Store(s.path)
    assert len(tercera.leer_usuarios()) == 2, tercera.leer_usuarios()
    tercera.cerrar()


def test_borrar_un_usuario_no_se_borra_el_resto():
    s = _store()
    s.guardar_usuarios([(1, "A", "admin", 1), (2, "B", "dj", 2), (3, "C", "user", 3)])
    s.borrar_usuario(2)
    assert {f["user_id"] for f in s.leer_usuarios()} == {1, 3}, s.leer_usuarios()
    s.cerrar()


def test_una_base_mas_nueva_no_se_sobrescribe():
    """Igual que el estado: si la base es de una version mas nueva que este
    codigo, se deja en solo lectura. Perder datos en silencio es peor que no
    arrancar."""
    carpeta = Path(tempfile.mkdtemp())
    ruta = carpeta / "futuro.db"

    import sqlite3

    conn = sqlite3.connect(str(ruta))
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION + 7,))
    conn.execute("CREATE TABLE cosa_del_futuro (x INTEGER)")
    conn.execute("INSERT INTO cosa_del_futuro VALUES (42)")
    conn.commit()
    conn.close()

    s = Store(ruta)
    assert s.solo_lectura is True, "una base mas nueva tiene que quedar en solo lectura"

    escritura = False
    try:
        s.guardar_usuarios([(1, "A", "admin", 1)])
    except RuntimeError:
        escritura = True
    assert escritura, "no se negatively a escribir en una base mas nueva"

    # Los datos del futuro siguen ahi, intactos.
    fila = s.conn.execute("SELECT x FROM cosa_del_futuro").fetchone()
    assert fila[0] == 42, "los datos de la base mas nueva foram tocados"
    s.cerrar()


def test_migra_los_archivos_viejos_y_no_los_borra():
    """state.json y el roles.db viejo entran una sola vez y quedan renombrados.

    Los originales NO se borran: si algo sale mal, el usuario todavia los
    tiene ahi.
    """
    carpeta = Path(tempfile.mkdtemp())
    estado_viejo = {
        "version": 1,
        "volume": 55,
        "paused": True,
        "radio_artist": "GP Band",
        "max_height": 720,
        "list_page": 2,
        "playlist": [
            {"url": "u1", "title": "Uno", "duration": "3:00", "channel": "A"},
            {"url": "u2", "title": "Dos", "duration": "4:00", "channel": "B"},
        ],
        "history": [{"url": "h1", "title": "Vieja"}, {"url": "h2", "title": "Vieja 2"}],
        "cursor": 1,
        "current": {"url": "u2", "title": "Dos"},
        "card": {"chat_id": 44, "message_id": 500, "is_photo": True},
    }
    (carpeta / "state.json").write_text(json.dumps(estado_viejo), encoding="utf-8")

    import sqlite3

    conn = sqlite3.connect(str(carpeta / "roles.db"))
    conn.execute("CREATE TABLE roles (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE users (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO roles VALUES ('77', 'admin')")
    conn.execute("INSERT INTO roles VALUES ('88', 'dj')")
    conn.execute("INSERT INTO users VALUES ('77', ?)", (json.dumps({"name": "Dueño", "joined_at": 1700000000}),))
    conn.commit()
    conn.close()

    import store as store_mod

    original_dir = store_mod.data_dir
    original_db = store_mod.DB_PATH
    original_state = store_mod.LEGACY_STATE
    original_roles = store_mod.LEGACY_ROLES
    store_mod.data_dir = lambda: carpeta
    store_mod.DB_PATH = carpeta / "ytremote.db"
    store_mod.LEGACY_STATE = carpeta / "state.json"
    store_mod.LEGACY_ROLES = carpeta / "roles.db"
    try:
        s = Store(carpeta / "ytremote.db")

        assert [t["url"] for t in s.leer_cola()] == ["u1", "u2"], s.leer_cola()
        assert s.leer_ajuste("volume") == 55, s.leer_ajuste("volume")
        assert s.leer_ajuste("radio_artist") == "GP Band"
        assert s.leer_ajuste("max_height") == 720
        assert s.leer_ajuste("list_page") == 2
        assert s.leer_ajuste("paused") is True
        assert s.leer_ajuste("cursor") == 1
        assert s.leer_ajuste("card")["message_id"] == 500
        assert len(s.leer_historial(100)) == 2, s.leer_historial(100)
        roles = {f["user_id"]: f["role"] for f in s.leer_usuarios()}
        assert roles == {77: "admin", 88: "dj"}, roles
        assert s.leer_ajuste(CLAVE_MIGRADO), "no se marco que la migracion ya se hizo"
        s.cerrar()

        # Los originales NO se borraron: quedaron con .migrado.
        assert not (carpeta / "state.json").exists(), "el state.json original se borro"
        assert (carpeta / "state.json.migrado").exists(), "no quedo la copia del state.json"
        assert (carpeta / "roles.db.migrado").exists(), "no quedo la copia del roles.db"
        guardados = json.loads((carpeta / "state.json.migrado").read_text(encoding="utf-8"))
        assert guardados["radio_artist"] == "GP Band", "la copia no esta intacta"

        # Y abrir de nuevo no vuelve a importar (ni duplica).
        otra = Store(carpeta / "ytremote.db")
        assert [t["url"] for t in otra.leer_cola()] == ["u1", "u2"], otra.leer_cola()
        assert len(otra.leer_usuarios()) == 2, otra.leer_usuarios()
        otra.cerrar()
    finally:
        store_mod.data_dir = original_dir
        store_mod.DB_PATH = original_db
        store_mod.LEGACY_STATE = original_state
        store_mod.LEGACY_ROLES = original_roles


def test_sin_archivos_viejos_no_pasa_nada():
    """Una instalacion nueva, sin nada que migrar, abre bien y queda vacia."""
    carpeta = Path(tempfile.mkdtemp())
    import store as store_mod

    original_dir = store_mod.data_dir
    store_mod.data_dir = lambda: carpeta
    try:
        s = Store(carpeta / "nueva.db")
        assert s.leer_cola() == []
        assert s.leer_historial(100) == []
        assert s.leer_usuarios() == []
        assert s.solo_lectura is False
        s.cerrar()
    finally:
        store_mod.data_dir = original_dir


def test_actualizar_una_cancion_no_borra_la_cola_ni_el_historial():
    """LA REGRESION MAS IMPORTANTE DE ESTA TAREA.

    Se escribia la cancion con `INSERT OR REPLACE`, que en SQLite BORRA la fila
    vieja y mete una nueva. Como `queue` e `history` la referencian, ese borrado
    cascada y se llevaba la fila que la apuntaba.

    Se manifesto en la migracion de los datos REALES del usuario: de 9 canciones
    en la playlist migraron 8 (se perdio la posicion 0, la que estaba sonando) y
    de 56 en el historial migraron 29. Una sola causa para los tres numeros.

    El sintoma de aqui es el mismo y mucho mas facil de ver: actualizar una
    cancion NO puede desaparecer de la cola ni del historial.
    """
    s = _store()
    s.guardar_cola(
        [
            {"url": "u1", "video_id": "v1", "title": "Uno"},
            {"url": "u2", "video_id": "v2", "title": "Dos"},
        ]
    )
    s.agregar_historial({"url": "u1", "video_id": "v1", "title": "Uno"}, cuando=1)
    s.agregar_historial({"url": "u1", "video_id": "v1", "title": "Uno x2"}, cuando=2)

    # Se actualiza la cancion dos veces, como haria el historial o la cola.
    s.guardar_track({"url": "u1", "video_id": "v1", "title": "Uno EDITADO"})
    s.guardar_track({"url": "u2", "video_id": "v2", "title": "Dos EDITADO"})

    cola = [t["video_id"] for t in s.leer_cola()]
    assert len(cola) == 2, f"actualizar borro la cola: {cola}"
    assert cola == ["v1", "v2"], cola

    historial = s.leer_historial(100)
    assert len(historial) == 2, (
        f"actualizar borro el historial: quedo {len(historial)} de 2"
    )
    # Son dos entradas aunque las dos apunten al mismo video del catalogo: el
    # historial registra REPeticiones (la misma canción se puede volver a oir).
    assert [h["video_id"] for h in historial] == ["v1", "v1"], historial

    # El catalogo quedo actualizado, no duplicado.
    assert len(s.conn.execute("SELECT * FROM tracks").fetchall()) == 2, "se duplico el catalogo"
    titulos = [t["title"] for t in s.tracks_por_video_id(["v1", "v2"]).values()]
    assert any("EDITADO" in t for t in titulos), titulos
    s.cerrar()


def test_el_historial_conserva_las_repeticiones():
    """La misma canción dos veces son dos filas: se puede volver a escuchar."""
    s = _store()
    for i in range(5):
        s.agregar_historial({"url": "u1", "video_id": "v1", "title": "Uno"}, cuando=100 + i)
    assert len(s.leer_historial(100)) == 5, s.leer_historial(100)
    s.cerrar()


def test_la_migracion_no_pierde_una_sola_cancion():
    """El numero exacto del defecto, con la combinacion que lo causaba.

    `INSERT OR REPLACE` + `ON DELETE CASCADE`: cada vez que una cancion se
    actualizaba, el borrado cascada se llevaba la fila de la cola y la del
    historial que la apuntaban. En los datos reales del usuario se manifesto
    como 8 de 9 en la cola (la posicion 0, la cancion que estaba sonando) y 29
    de 56 en el historial.

    Aqui se reproduce el patron exacto: una cancion que esta en la cola y
    tambien repetida en el historial, y despues se actualiza.
    """
    import json as _json
    import tempfile as _tf

    import store as store_mod

    origen = {
        "version": 1,
        "volume": 100,
        "paused": False,
        "cursor": 0,
        "list_page": 0,
        "radio_artist": "",
        "max_height": None,
        "playlist": [
            {"url": f"u{i}", "title": f"T{i}", "duration_seconds": 100 + i} for i in range(9)
        ],
        # La cancion 0 aparece en el historial (y repetida, como pasa de verdad).
        "history": [{"url": "u0", "title": "T0"}, {"url": "u0", "title": "T0 otra vez"}],
    }
    carpeta = Path(_tf.mkdtemp())
    (carpeta / "state.json").write_text(_json.dumps(origen), encoding="utf-8")

    original = (
        store_mod.data_dir,
        store_mod.DB_PATH,
    )
    store_mod.data_dir = lambda: carpeta
    store_mod.DB_PATH = carpeta / "y.db"
    _RESTAURAR.append((store_mod, original))
    try:
        s = store_mod.Store(carpeta / "y.db")
        cola = s.leer_cola()
        assert len(cola) == 9, (
            f"la migracion perdio canciones de la cola: {len(cola)} de 9"
        )
        assert cola[0]["url"] == "u0", f"perdio la primera cancion: {cola[0]['url']}"
        assert len(s.leer_historial(100)) == 2, (
            f"la migracion perdio historial: {len(s.leer_historial(100))} de 2"
        )
        s.cerrar()
    finally:
        _restaurar_todo()


def test_una_duracion_en_texto_no_tira_la_migracion():
    """En el state.json real hay items con duration_seconds = "10:22" (texto).

    Con un int() directo, ESE item reventaba la migracion entera y se perdian
    los otros 55 del historial. Ademas "10:22" se convierte a 622: convertirla
    conserva el dato, descartarlo lo pierde.
    """
    from store import _segundos

    assert _segundos("10:22") == 622, _segundos("10:22")
    assert _segundos("4:25") == 265
    assert _segundos("1:02:03") == 3723
    assert _segundos(220) == 220
    assert _segundos("") == 0
    assert _segundos(None) == 0
    assert _segundos("no es un numero") == 0
    assert _segundos(True) == 0

    s = _store()
    s.guardar_track({"url": "u1", "video_id": "v1", "duration_seconds": "10:22"})
    fila = s.tracks_por_video_id(["v1"])["v1"]
    assert fila["duration_seconds"] == 622, fila
    s.cerrar()


def test_una_base_temporal_jamas_toca_la_data_del_usuario():
    """Un Store de prueba no puede leer NI renombrar los datos reales.

    Este es el fallo mas caro que hemos tenido: una prueba abria una base en
    una carpeta temporal, pero la migracion buscaba el `state.json` y el
    `roles.db` REALES, los importaba en la base de prueba y los renombraba a
    `.migrado`. Los datos del usuario se movian mientras corrian los tests.

    Aqui se comprueba que los archivos reales ni se abren ni desaparecen, y que
    la base de prueba empieza vacia.
    """
    import store as store_mod

    reales = store_mod.data_dir()
    if not reales.is_dir():
        print("  --  sin data/ local: no hay nada real que proteger")
        return

    antes = {p.name: p.stat().st_mtime_ns for p in reales.glob("*") if p.is_file()}

    carpeta = Path(tempfile.mkdtemp())
    (carpeta / "state.json").write_text(
        json.dumps({"version": 1, "playlist": [{"url": "u", "title": "Ajena"}],
                    "history": []}),
        encoding="utf-8",
    )
    s = Store(carpeta / "ytremote.db")

    # Importo lo que hay en SU carpeta, no lo de la carpeta real.
    assert len(s.leer_cola()) == 1, s.leer_cola()
    assert (carpeta / "state.json.migrado").is_file(), "no marco el legacy local"

    # Y los archivos reales siguen intactos: ni renombrados ni modificados.
    despues = {p.name: p.stat().st_mtime_ns for p in reales.glob("*") if p.is_file()}
    assert despues == antes, f"una base temporal toco la data real: {antes} -> {despues}"
    s.cerrar()


def test_la_base_se_repara_si_el_legacy_ya_estaba_renombrado():
    """Una corrida previa renombro el legacy y la base quedo vacia.

    Es el peor escenario posible y el mas silencioso: el bot no encuentra
    `state.json`, no importa nada, marca la migracion como hecha y el usuario
    arranca con la lista y el historial vacios PARA SIEMPRE, sin un solo error
    en pantalla. La base tiene que reconocer el `.migrado` y traerse los datos.
    """
    carpeta = Path(tempfile.mkdtemp())
    # El legacy ya fue renombrado antes de que existiera esta base.
    (carpeta / "state.json.migrado").write_text(
        json.dumps(
            {
                "version": 1,
                "volume": 80,
                "playlist": [{"url": "u1", "video_id": "v1", "title": "Uno"}],
                "history": [
                    {"url": "u9", "video_id": "v9", "title": "Vieja", "duration_seconds": 90}
                ],
                "card": {"chat_id": 7, "message_id": 8},
            }
        ),
        encoding="utf-8",
    )
    s = Store(carpeta / "ytremote.db")
    assert len(s.leer_cola()) == 1, f"no se reparo la cola: {s.leer_cola()}"
    assert len(s.leer_historial(100)) == 1, s.leer_historial(100)
    assert s.leer_ajuste("volume") == 80, s.leer_ajuste("volume")
    assert (carpeta / "state.json.migrado").is_file(), "el legacy se perdio"
    s.cerrar()


def run():
    total = 0
    try:
        for nombre, fn in sorted(globals().items()):
            if nombre.startswith("test_") and callable(fn):
                fn()
                total += 1
    finally:
        _restaurar_todo()
    print(f"STORE TESTS OK ({total} pruebas)")


if __name__ == "__main__":
    run()