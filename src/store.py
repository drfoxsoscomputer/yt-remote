"""Almacenamiento unico de YT-Remote: un SQLite con esquema de verdad.

POR QUE ESTE MODULO EXISTE

Antes habia dos almacenes, y los dos estaban mal:

1. `data/state.json`: la cola, el historial y los ajustes, en un JSON que se
   reescribia COMPLETO en cada cambio. Lo unico que crecia es justo lo que
   estaba en la peor herramienta.
2. `data/roles.db`: dos tablas `roles(key, value)` y `users(key, value)`, o
   sea un JSON dentro de un archivo abierto con sqlite3. Sin columnas, sin
   indices, sin relaciones: SQLite no aportaba nada.

Y lo peor de `roles.py` era un `DELETE FROM roles` en cada arranque: como el
JSON viejo ya no existe, cada reinicio dejaba la tabla vacia. Los roles y los
usuarios se perdian en silencio, igual que la lista de canciones.

AQUI HAY UN ESQUEMA REAL

- `tracks`: el catalogo de canciones, una fila por video (clave: video_id).
- `queue`: la lista en orden, con su posicion. Referencia a `tracks`.
- `history`: lo que ya sono, con su fecha.
- `usuarios`: id, nombre, rol y cuando se lo vio por primera vez.
- `ajustes`: volumen, pausa, artista de la radio, calidad, pagina del listado
  y la identidad del mensaje de la tarjeta.
- `schema_version`: que migraciones se corrieron.

MIGRACIONES QUE SI MIGRAN

`migrar()` aplica en orden las funciones de `_MIGRACIONES` y anota la version
en `schema_version`. Si el archivo dice una version MAS NUEVA que este codigo,
NO se abre para escribir: es el mismo criterio que usa StateStore. Perder datos
en silencio es peor que no arrancar.

Ademas, la primera vez, importa lo que hubiera en los archivos viejos
(`state.json` y el `roles.db` de clave-valor) y los deja renombrados con
`.migrado`. No se borra nada: si algo sale mal, los originales siguen ahi.
"""

import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("bot")

SCHEMA_VERSION = 1


def data_dir() -> Path:
    """Carpeta data/. Junto al .exe cuando es portable."""
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(os.path.abspath(sys.executable))) / "data"
    return Path(__file__).resolve().parent.parent / "data"


DB_PATH = data_dir() / "ytremote.db"
LEGACY_STATE = data_dir() / "state.json"
LEGACY_ROLES = data_dir() / "roles.db"

CLAVE_MIGRADO = "migracion_legacy"


# --- Migraciones ---------------------------------------------------------
# Cada una lleva el esquema de una version a la siguiente. Solo hacia adelante.

_MIGRACIONES: dict[int, tuple[str, list[str]]] = {
    1: (
        "esquema inicial: catalogo, cola, historial, usuarios y ajustes",
        [
            """
            CREATE TABLE IF NOT EXISTS tracks (
                video_id         TEXT PRIMARY KEY,
                url              TEXT NOT NULL,
                title            TEXT NOT NULL DEFAULT '',
                duration         TEXT NOT NULL DEFAULT '',
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                thumbnail        TEXT NOT NULL DEFAULT '',
                channel          TEXT NOT NULL DEFAULT '',
                artist           TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS queue (
                position INTEGER PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES tracks(video_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                played_at INTEGER NOT NULL,
                video_id  TEXT NOT NULL REFERENCES tracks(video_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_history_played ON history(played_at)",
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                user_id   INTEGER PRIMARY KEY,
                name      TEXT NOT NULL DEFAULT '',
                role      TEXT NOT NULL DEFAULT 'user',
                joined_at INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ajustes (
                clave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            )
            """,
        ],
    ),
}


def _version_de(conn: sqlite3.Connection) -> int:
    fila = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(fila[0]) if fila and fila[0] is not None else 0


def _segundos(valor: Any) -> int:
    """Lee una duracion a segundos sin tirar nunca.

    En el `state.json` real hay items de historial con `duration_seconds` como
    texto ("10:22") en vez de numero. Con un `int()` directo, ESE item revienta
    la migracion entera y se pierden los otros 55. Ademas convertir "10:22" a
    622 conserva la informacion, mientras que descartarla la pierde.
    """
    if isinstance(valor, bool):
        return 0
    if isinstance(valor, (int, float)):
        return int(valor)
    texto = str(valor or "").strip()
    if not texto:
        return 0
    try:
        return int(texto)
    except ValueError:
        pass
    partes = texto.split(":")
    if len(partes) in (2, 3) and all(p.strip().isdigit() for p in partes):
        numeros = [int(p) for p in partes]
        if len(numeros) == 2:
            return numeros[0] * 60 + numeros[1]
        return numeros[0] * 3600 + numeros[1] * 60 + numeros[2]
    return 0


class Store:
    """Acceso a la base. Una instancia por proceso."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.solo_lectura = False
        self.motivo_solo_lectura = ""
        self._abrir()

    def _abrir(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
        )
        self.conn.commit()
        actual = _version_de(self.conn)

        if actual > SCHEMA_VERSION:
            self.solo_lectura = True
            self.motivo_solo_lectura = (
                f"la base es de la version {actual} y este bot entiende hasta "
                f"la {SCHEMA_VERSION}"
            )
            logger.error(
                "%s. No se va a escribir: tus datos quedan intactos.", self.motivo_solo_lectura
            )
            return

        for version in sorted(_MIGRACIONES):
            if version <= actual:
                continue
            nombre, sentencias = _MIGRACIONES[version]
            logger.info("Migracion %d: %s", version, nombre)
            for sql in sentencias:
                self.conn.execute(sql)
            self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            self.conn.commit()

        self._importar_legado()

    # --- Ajustes ---------------------------------------------------------
    def leer_ajuste(self, clave: str, por_defecto: Any = None) -> Any:
        fila = self.conn.execute(
            "SELECT valor FROM ajustes WHERE clave = ?", (clave,)
        ).fetchone()
        if fila is None:
            return por_defecto
        try:
            return json.loads(fila["valor"])
        except (ValueError, TypeError):
            return por_defecto

    def escribir_ajuste(self, clave: str, valor: Any) -> None:
        self._verificar_escritura()
        self.conn.execute(
            "INSERT OR REPLACE INTO ajustes (clave, valor) VALUES (?, ?)",
            (clave, json.dumps(valor, ensure_ascii=False)),
        )
        self.conn.commit()

    # --- Canciones -------------------------------------------------------
    def guardar_track(self, track: dict) -> None:
        """Inserta o ACTUALIZA una cancion del catalogo.

        OJO con `INSERT OR REPLACE`: eso borra la fila vieja y mete una nueva.
        Con `queue` y `history` referenciando a `tracks`, ese borrado cascada y
        se lleva por delante las filas que la apuntaban. Pasaba de verdad: una
        cancion que estaba en la cola y tambien en el historial (o repetida en
        el historial) desaparecia de la cola o del historial al actualizarse.
        Se perdia justo la cancion que estaba sonando, y en silencio.

        Por eso es un UPSERT de verdad: actualiza la fila, nunca la borra.
        """
        self._verificar_escritura()
        video_id = track.get("video_id") or track.get("url") or ""
        if not video_id:
            return
        self.conn.execute(
            """
            INSERT INTO tracks
                (video_id, url, title, duration, duration_seconds,
                 thumbnail, channel, artist)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                url              = excluded.url,
                title            = excluded.title,
                duration         = excluded.duration,
                duration_seconds = excluded.duration_seconds,
                thumbnail        = excluded.thumbnail,
                channel          = excluded.channel,
                artist           = excluded.artist
            """,
            (
                video_id,
                track.get("url", ""),
                track.get("title", ""),
                track.get("duration", ""),
                _segundos(track.get("duration_seconds", 0)),
                track.get("thumbnail", ""),
                track.get("channel", ""),
                track.get("artist", ""),
            ),
        )

    def tracks_por_video_id(self, ids: list[str]) -> dict[str, dict]:
        """Canciones pedidas por id, en un solo viaje a la base.

        Antes la cola entera se guardaba como un JSON gigante reescrito cada
        vez; ahora solo se tocan las filas que cambian.
        """
        if not ids:
            return {}
        encontrados: dict[str, dict] = {}
        # SQLite limita los parametros de una consulta: se va de a tandas.
        for inicio in range(0, len(ids), 400):
            tanda = ids[inicio : inicio + 400]
            marcas = ",".join("?" * len(tanda))
            for fila in self.conn.execute(
                f"SELECT * FROM tracks WHERE video_id IN ({marcas})", tanda
            ):
                encontrados[fila["video_id"]] = dict(fila)
        return encontrados

    # --- Cola -----------------------------------------------------------
    def guardar_cola(self, tracks: list[dict]) -> None:
        self._verificar_escritura()
        for t in tracks:
            self.guardar_track(t)
        self.conn.execute("DELETE FROM queue")
        self.conn.executemany(
            "INSERT INTO queue (position, video_id) VALUES (?, ?)",
            [
                (pos, t.get("video_id") or t.get("url") or "")
                for pos, t in enumerate(tracks)
            ],
        )
        self.conn.commit()

    def leer_cola(self) -> list[dict]:
        filas = self.conn.execute(
            "SELECT video_id FROM queue ORDER BY position"
        ).fetchall()
        if not filas:
            return []
        encontrados = self.tracks_por_video_id([f["video_id"] for f in filas])
        salida: list[dict] = []
        for fila in filas:
            t = encontrados.get(fila["video_id"])
            if t:
                salida.append(dict(t))
        return salida

    # --- Historial ------------------------------------------------------
    def agregar_historial(self, track: dict, cuando: Optional[int] = None) -> None:
        self._verificar_escritura()
        video_id = track.get("video_id") or track.get("url") or ""
        if not video_id:
            return
        self.guardar_track(track)
        self.conn.execute(
            "INSERT INTO history (played_at, video_id) VALUES (?, ?)",
            (int(cuando if cuando is not None else time.time()), video_id),
        )
        self.conn.commit()

    def leer_historial(self, limite: int = 100) -> list[dict]:
        filas = self.conn.execute(
            "SELECT video_id FROM history ORDER BY id DESC LIMIT ?", (limite,)
        ).fetchall()
        if not filas:
            return []
        encontrados = self.tracks_por_video_id([f["video_id"] for f in filas])
        salida: list[dict] = []
        for fila in filas:
            t = encontrados.get(fila["video_id"])
            if t:
                salida.append(dict(t))
        return salida

    def podar_historial(self, limite: int = 100) -> None:
        self._verificar_escritura()
        self.conn.execute(
            """
            DELETE FROM history WHERE id NOT IN (
                SELECT id FROM history ORDER BY id DESC LIMIT ?
            )
            """,
            (limite,),
        )
        self.conn.commit()

    # --- Usuarios -------------------------------------------------------
    def leer_usuarios(self) -> list[dict]:
        return [dict(f) for f in self.conn.execute("SELECT * FROM usuarios")]

    def guardar_usuario(
        self, user_id: int, name: str, role: str, joined_at: int
    ) -> None:
        self._verificar_escritura()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO usuarios (user_id, name, role, joined_at)
            VALUES (?, ?, ?, ?)
            """,
            (int(user_id), name, role, int(joined_at)),
        )

    def borrar_usuario(self, user_id: int) -> None:
        self._verificar_escritura()
        self.conn.execute("DELETE FROM usuarios WHERE user_id = ?", (int(user_id),))

    def guardar_usuarios(self, filas: list[tuple[int, str, str, int]]) -> None:
        self._verificar_escritura()
        if filas:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO usuarios (user_id, name, role, joined_at)
                VALUES (?, ?, ?, ?)
                """,
                filas,
            )
            self.conn.commit()

    # --- Migracion de datos viejos --------------------------------------
    def _importar_legado(self) -> None:
        """Trae lo de `state.json` y del `roles.db` viejo, UNA sola vez.

        Los archivos viejos no se borran: se renombran con `.migrado`. Si algo
        sale mal, el original sigue ahi y no se perdio nada.
        """
        if self.leer_ajuste(CLAVE_MIGRADO):
            return

        importo = False
        estado = _leer_json(LEGACY_STATE)
        if estado:
            try:
                self._importar_state(estado)
                importo = True
                _marcar_migrado(LEGACY_STATE)
                logger.info(
                    "Migracion de datos: playlist (%d), historial (%d) y ajustes "
                    "traidos de state.json. El original quedo como %s",
                    len(estado.get("playlist") or []),
                    len(estado.get("history") or []),
                    LEGACY_STATE.name + ".migrado",
                )
            except Exception as exc:  # noqa: BLE001 - no debe tumbar el arranque
                logger.error("No se pudo migrar state.json: %s", exc)

        roles = _leer_roles_viejo(LEGACY_ROLES)
        if roles:
            try:
                self.guardar_usuarios(
                    [
                        (uid, entry.get("name") or str(uid), entry.get("role") or "user",
                         int(entry.get("joined_at") or 0))
                        for uid, entry in roles.items()
                    ]
                )
                importo = True
                _marcar_migrado(LEGACY_ROLES)
                logger.info(
                    "Migracion de datos: %d usuario(s) con su rol traidos de "
                    "roles.db. El original quedo como %s",
                    len(roles),
                    LEGACY_ROLES.name + ".migrado",
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("No se pudo migrar roles.db: %s", exc)

        if importo:
            self.escribir_ajuste(CLAVE_MIGRADO, time.time())

    def _importar_state(self, estado: dict) -> None:
        self._verificar_escritura()
        playlist = [
            {**t, "video_id": t.get("video_id") or t.get("url")}
            for t in (estado.get("playlist") or [])
            if isinstance(t, dict)
        ]
        if playlist:
            self.guardar_cola(playlist)

        historial = [t for t in (estado.get("history") or []) if isinstance(t, dict)]
        if historial:
            for t in historial:
                self.agregar_historial(
                    {**t, "video_id": t.get("video_id") or t.get("url")}
                )
            self.podar_historial(100)

        ajustes = {
            "volume": estado.get("volume", 100),
            "paused": estado.get("paused", False),
            "cursor": estado.get("cursor", 0),
            "list_page": estado.get("list_page", 0),
            "radio_artist": estado.get("radio_artist", ""),
            "max_height": estado.get("max_height"),
            "card": estado.get("card") or {},
            "current": estado.get("current"),
        }
        for clave, valor in ajustes.items():
            self.escribir_ajuste(clave, valor)

    def _verificar_escritura(self) -> None:
        if self.solo_lectura:
            logger.error(
                "NO se escribe en la base: %s. Los datos quedan intactos.",
                self.motivo_solo_lectura,
            )
            raise RuntimeError(f"base en solo lectura: {self.motivo_solo_lectura}")

    def cerrar(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def _leer_json(ruta: Path) -> Optional[dict]:
    try:
        if not ruta.exists():
            return None
        with ruta.open(encoding="utf-8") as f:
            datos = json.load(f)
        return datos if isinstance(datos, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo leer %s: %s", ruta.name, exc)
        return None


def _leer_roles_viejo(ruta: Path) -> dict[int, dict]:
    """Del `roles.db` de clave-valor (o del `roles.json` mas antiguo todavia)."""
    salida: dict[int, dict] = {}
    try:
        if ruta.exists():
            conn = sqlite3.connect(str(ruta))
            try:
                roles = dict(conn.execute("SELECT key, value FROM roles").fetchall())
                usuarios = dict(conn.execute("SELECT key, value FROM users").fetchall())
            finally:
                conn.close()
            for uid, rol in roles.items():
                salida.setdefault(int(uid), {})["role"] = rol
            for uid, crudo in usuarios.items():
                try:
                    entrada = json.loads(crudo)
                except (ValueError, TypeError):
                    entrada = {}
                salida.setdefault(int(uid), {}).update(entrada)
    except Exception as exc:  # noqa: BLE001
        logger.error("No se pudo leer el roles.db viejo: %s", exc)

    # roles.json, el mas antiguo de todos (aun puede estar en equipos viejos).
    if not salida:
        datos = _leer_json(ruta.parent / "roles.json")
        if datos:
            for uid, rol in (datos.get("roles") or {}).items():
                salida.setdefault(int(uid), {})["role"] = rol
            for uid, entrada in (datos.get("users") or {}).items():
                if isinstance(entrada, dict):
                    salida.setdefault(int(uid), {}).update(entrada)
    return salida


def _marcar_migrado(ruta: Path) -> None:
    """Renombra el archivo viejo para que no se vuelva a leer."""
    try:
        if ruta.exists():
            ruta.rename(ruta.with_name(ruta.name + ".migrado"))
    except OSError as exc:
        logger.error("No se pudo renombrar %s: %s", ruta.name, exc)