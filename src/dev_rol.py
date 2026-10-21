#!/usr/bin/env python3
r"""Cambio de rol para relevar (SOLO desarrollo): prueba el bot como
invitado (sin nada) o como dj, y vuelve a admin despues.

NO es parte del bot: no importa nada del proyecto. Solo toca la base donde
el bot guarda los roles (la tabla `usuarios` de data/ytremote.db).

Reglas del script:
- El bot DEBE estar apagado cuando se corre (por eso "Detener" primero en
  el launcher): si no, el bot podria pisar el cambio con su memoria.
- Antes de bajar a alguien graba una copia de los que eran admin en
  `rol_backup.json` (junto a la base) para poder devolverlos con la
  opcion 3. Ademas, el dueno se devuelve admin solo al re-arrancar el bot.
- Uso avanzado: `python dev_rol.py --base <ruta\de\otra\base.db>` prueba
  sobre otra base (asi las pruebas nunca tocan la base real).

Uso: doble clic en `cambiar-rol.cmd` (raiz del repo) o:
    python src\\dev_rol.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _elegir_base() -> Path:
    """Resuelve la base a tocar: dist primero (el deploy en vivo), luego
    la data del repo. La ruta de la linea de comandos tiene prioridad."""
    if "--base" in sys.argv:
        idx = sys.argv.index("--base")
        if idx + 1 < len(sys.argv):
            return Path(sys.argv[idx + 1]).resolve()
    candidata = [
        REPO / "dist" / "ytremote" / "data" / "ytremote.db",
        REPO / "data" / "ytremote.db",
    ]
    for ruta in candidata:
        if ruta.exists():
            return ruta
    return candidata[-1]


def _abrir(ruta: Path) -> sqlite3.Connection | None:
    """Abre la base si tiene la tabla `usuarios` y el bot esta apagado."""
    if not ruta.exists():
        print(f"No encuentro la base del bot en:\n  {ruta}")
        return None
    try:
        conn = sqlite3.connect(ruta, timeout=2)
        tiene = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usuarios'"
        ).fetchone()
        if not tiene:
            print(
                "Esa base no tiene la tabla de usuarios del bot (todavia no"
                "\narranco nunca?). Si no, corre esta."
            )
            conn.close()
            return None
        # Candado: si el bot esta corriendo y tiene la base agarrada, esto
        # falla al instante (mejor un mensaje claro que pisar datos).
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        print(
            f"La base esta agarrada ({exc}).\n"
            "Casi seguro el bot esta corriendo: tocale DETENER en el"
            " launcher\ny corre este script de nuevo."
        )
        return None
    return conn


def _roles(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    filas = conn.execute(
        "SELECT user_id, name, role FROM usuarios ORDER BY user_id"
    ).fetchall()
    if not filas:
        print(
            "La base no tiene usuarios todavia (el bot nunca vio nadie?).\n"
            "No hay nada que cambiar."
        )
    return [(int(u), n, r) for u, n, r in filas]


ROTULO = {"user": "invitado", "dj": "dj", "admin": "admin"}


def _mostrar(conn: sqlite3.Connection, titulo: str) -> None:
    print(f"\n{titulo}")
    filas = _roles(conn)
    if not filas:
        return
    for uid, nombre, rol in filas:
        print(f"  {uid} {nombre}: {ROTULO.get(rol, rol)}")


def _backup_admins(conn: sqlite3.Connection, ruta_base: Path) -> None:
    admins = [
        {"user_id": uid, "name": nombre}
        for uid, nombre, rol in _roles(conn)
        if rol == "admin"
    ]
    ruta = ruta_base.parent / "rol_backup.json"
    if admins:
        ruta.write_text(
            json.dumps(admins, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"\nGuardado el respaldo de {len(admins)} admin(es) en:\n  {ruta}"
        )
    elif ruta.exists():
        # NUNCA pisar un respaldo viejo con una lista vacia: si TODOS los
        # admins ya fueron bajados, el respaldo es lo unico que dice
        # quien volvia a admin.
        print(
            "\nYa no hay admins (se bajaron antes). El respaldo anterior"
            " queda intacto."
        )
    else:
        print("\nNo habia admins y no hay respaldo previo: nada que guardar.")


def _a_quienes(conn: sqlite3.Connection, ruta_base: Path) -> list[int]:
    """A quienes aplica el rol nuevo: los admins del RESPALDO si hay (es
    la lista original de admins, aunque ya esten bajados), y si no hay
    respaldo, los que HOY abajo sean admin."""
    ruta = ruta_base.parent / "rol_backup.json"
    if ruta.exists():
        return [
            int(a["user_id"])
            for a in json.loads(ruta.read_text(encoding="utf-8"))
        ]
    return [uid for uid, _, rol in _roles(conn) if rol == "admin"]


def _restaurar(conn: sqlite3.Connection, ruta_base: Path) -> None:
    ruta = ruta_base.parent / "rol_backup.json"
    if not ruta.exists():
        print(
            "\nNo hay respaldo guardado. Camino corto: apaga y prende el"
            " bot\n(Detener -> Conectar) y el dueno vuelve a admin solo."
        )
        return
    admins = json.loads(ruta.read_text(encoding="utf-8"))
    for a in admins:
        conn.execute(
            "UPDATE usuarios SET role='admin' WHERE user_id=?", (a["user_id"],)
        )
    conn.commit()
    ruta.unlink()
    print(
        f"\nDevueltos a admin: {len(admins)} usuario(s). El respaldo se"
        " borro al aplicar todo."
    )


def _bajar(conn: sqlite3.Connection, nuevo: str, ids: list[int]) -> None:
    if not ids:
        print(
            "\nNo hay nadie como admin a quien bajar (y sin respaldo). No"
            " toque nada."
        )
        return
    conn.executemany(
        "UPDATE usuarios SET role=? WHERE user_id=?",
        [(nuevo, uid) for uid in ids],
    )
    conn.commit()
    print(
        f"\n{len(ids)} usuario(s) quedaron como {ROTULO[nuevo]}. El bot, al"
        " arrancar,\nlos trata asi (el dueno vuelve a admin solo en el"
        " proximo arranque)."
    )


def main() -> int:
    ruta_base = _elegir_base()
    print(f"Base del bot: {ruta_base}")
    conn = _abrir(ruta_base)
    if conn is None:
        return 1
    try:
        _mostrar(conn, "HOY el bot ve asi:")
        print(
            "\nQue rol quiero probar?\n"
            "  1) Invitado (el bot no me deja nada: el aviso unico)\n"
            "  2) DJ (controla la musica, sin calidad ni usuarios)\n"
            "  3) Volver a admin (despues de probar)\n"
            " 0) Salir sin tocar nada"
        )
        eleccion = input("Escribi el numero y Enter: ").strip()
        if eleccion == "1":
            _backup_admins(conn, ruta_base)
            _bajar(conn, "user", _a_quienes(conn, ruta_base))
        elif eleccion == "2":
            _backup_admins(conn, ruta_base)
            _bajar(conn, "dj", _a_quienes(conn, ruta_base))
        elif eleccion == "3":
            _restaurar(conn, ruta_base)
        elif eleccion == "0":
            print("No toque nada.")
        else:
            print(f"'{eleccion}' no es una opcion. No toque nada.")
            return 0
        _mostrar(conn, "Despues del cambio:")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
