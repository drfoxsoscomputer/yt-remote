"""Garantias del launcher: lo que la ventana NO debe exponer.

El usuario reporto que al abrir Configuracion veia el token del bot y su ID en
los campos. Guardar bien y despues mostrar es PEOR que no guardar bien: la
credencial queda en el DOM de la ventana, en la respuesta HTTP local y en
cualquier captura de pantalla.

Estas pruebas fijan que el token sea de SOLO LECTURA para afuera y que la
ventana no pueda hablar con el launcher sin la llave del arranque.

IMPORTANTE: acá se reemplaza `_load_session` por un dato fijo. Estas pruebas NO
tocan `data/session.enc`: es la sesión real del usuario y una prueba que la lea
o la escriba con un token de mentira le deja el launcher sin conexión.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

import launcher_web  # noqa: E402

TOKEN_DE_PRUEBA = "1234567890:SEGREDO-DE-PRUEBA"
SESION_FALSA = {
    "bot_token": TOKEN_DE_PRUEBA,
    "admin_id": 4242,
    "kick_after_hours": 3,
    "bot_username": "ytremoto_bot",
    "mpv_path": "runtime\\mpv\\mpv.exe",
}


def _con_sesion(callback):
    """Corre callback con la sesión falsa puesta, y la saca al terminar."""
    original = launcher_web._load_session
    launcher_web._load_session = lambda: dict(SESION_FALSA)
    try:
        return callback()
    finally:
        launcher_web._load_session = original


def test_el_token_no_vuelve_a_la_pantalla():
    """/api/session dice QUE hay sesion, pero nunca DEVUELVE el token."""
    with launcher_web.app.test_client() as c:

        def pedir():
            r = c.get("/api/session", headers={"X-Ytr-Token": launcher_web._API_TOKEN})
            return r.get_json(), r.get_data(as_text=True)

        datos, crudo = _con_sesion(pedir)

    assert datos.get("has_session") is True, datos
    assert "bot_token" not in datos, f"/api/session volvio a devolver el token: {datos}"
    assert "admin_id" not in datos, f"/api/session volvio a devolver el ID: {datos}"
    # El texto crudo tampoco lo puede contener (ni en un campo escondido).
    assert TOKEN_DE_PRUEBA not in crudo, "el token aparece en la respuesta cruda"
    assert TOKEN_DE_PRUEBA not in str(datos), "el token aparece en los datos"
    # Lo que sí puede decir: si hay ID puesto y las horas, que no son secretos.
    assert datos.get("admin_id_set") is True, datos
    assert datos.get("kick_after_hours") == 3, datos
    assert datos.get("bot_username") == "ytremoto_bot", datos


def test_las_llamadas_exigen_la_llave_del_arranque():
    """Sin la llave, /api/* no responde. Evita que una pagina ajena en el
    navegador use el launcher como puente."""
    with launcher_web.app.test_client() as c:
        sin_llave = c.get("/api/session")
        llave_inventada = c.get("/api/session", headers={"X-Ytr-Token": "no-es-la-llave"})
        con_llave = c.get("/api/session", headers={"X-Ytr-Token": launcher_web._API_TOKEN})

    assert sin_llave.status_code == 403, sin_llave.status_code
    assert llave_inventada.status_code == 403, llave_inventada.status_code
    assert con_llave.status_code == 200, con_llave.status_code
    # La llave tiene que ser larga: no es una constante escrita a mano.
    assert len(launcher_web._API_TOKEN) >= 32, "la llave es demasiado corta"


def test_guardar_sin_escribir_el_token_conserva_el_guardado():
    """Editar la config sin tocar el token NO lo borra.

    Como el token ya no se muestra, cambiar las horas de invitado tiene que
    poder hacerse dejando el campo vacío. Si eso lo borrara, el usuario
    tendría que volver a escribir el token cada vez que cambia una hora.
    """
    guardado: dict = {}
    original_load = launcher_web._load_session
    original_save = launcher_web._save_session
    launcher_web._load_session = lambda: dict(SESION_FALSA)
    launcher_web._save_session = lambda data: (guardado.update(data), True)[1]
    try:
        with launcher_web.app.test_client() as c:
            r = c.post(
                "/api/save",
                json={"bot_token": "", "admin_id": "", "kick_after_hours": 9},
                headers={"X-Ytr-Token": launcher_web._API_TOKEN},
            )
    finally:
        launcher_web._load_session = original_load
        launcher_web._save_session = original_save

    assert r.get_json().get("ok") is True, r.get_json()
    assert guardado.get("bot_token") == TOKEN_DE_PRUEBA, (
        f"se perdio el token al guardar vacio: {guardado.get('bot_token')!r}"
    )
    assert guardado.get("admin_id") == 4242, guardado
    assert guardado.get("kick_after_hours") == 9, guardado


def test_escribir_un_token_nuevo_reemplaza_el_anterior():
    """Si el usuario escribe un token, ese gana: el vacio conserva, no manda."""
    guardado: dict = {}
    original_load = launcher_web._load_session
    original_save = launcher_web._save_session
    original_running = launcher_web.bot_process.is_running
    launcher_web._load_session = lambda: dict(SESION_FALSA)
    launcher_web._save_session = lambda data: (guardado.update(data), True)[1]
    launcher_web.bot_process.is_running = lambda: False
    try:
        with launcher_web.app.test_client() as c:
            r = c.post(
                "/api/save",
                json={"bot_token": "999:OTRO", "admin_id": "77", "kick_after_hours": 1},
                headers={"X-Ytr-Token": launcher_web._API_TOKEN},
            )
    finally:
        launcher_web._load_session = original_load
        launcher_web._save_session = original_save
        launcher_web.bot_process.is_running = original_running

    assert r.get_json().get("ok") is True, r.get_json()
    assert guardado.get("bot_token") == "999:OTRO", guardado
    assert guardado.get("admin_id") == 77, guardado


def test_sin_sesion_el_token_sigue_siendo_obligatorio():
    """Sin sesión guardada, guardar vacío es un error de verdad."""
    original = launcher_web._load_session
    launcher_web._load_session = lambda: None
    try:
        with launcher_web.app.test_client() as c:
            r = c.post(
                "/api/save",
                json={"bot_token": "", "admin_id": "", "kick_after_hours": 0},
                headers={"X-Ytr-Token": launcher_web._API_TOKEN},
            )
    finally:
        launcher_web._load_session = original

    assert r.status_code == 400, r.status_code
    assert "token" in r.get_json().get("error", "").lower(), r.get_json()


def run():
    total = 0
    for nombre, fn in sorted(globals().items()):
        if nombre.startswith("test_") and callable(fn):
            fn()
            total += 1
    print(f"LAUNCHER SECRETO TESTS OK ({total} pruebas)")


if __name__ == "__main__":
    run()
