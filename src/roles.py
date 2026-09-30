"""Sistema de roles de YT-Remote.

Roles:
- admin: acceso total
- dj: gestion de reproduccion (play, pause, skip, cola, volumen)
- user: solo pedir canciones

Persistencia en data/roles.json con dos mapas:
- "roles": <id> -> rol (admin|dj|user)
- "users": <id> -> {"name": nombre visible, "joined_at": epoch en el que el
  bot vio al usuario por primera vez (origen del conteo de la expulsion 24h)}
"""

import json
import sqlite3
import time
from pathlib import Path

VALID_ROLES = {"admin", "dj", "user"}
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "roles.db"
ROLES_PATH = DATA_DIR / "roles.json"


class RoleManager:
    """Gestiona usuarios, sus roles y nombres, persistidos en data/roles.json."""

    def __init__(self) -> None:
        self._roles: dict[str, str] = {}
        self._users: dict[str, dict[str, str | int]] = {}
        # Lo que se borro en memoria y hay que borrar tambien en la base.
        self._roles_borrados: set[str] = set()
        self._users_borrados: set[str] = set()
        self._load()

    def _load(self) -> None:
        """Carga los roles y usuarios desde la base de datos SQLite.

        NO borra nada. Antes hacia `DELETE FROM roles` y `DELETE FROM users` en
        CADA arranque y solo volvia a llenar las tablas desde `data/roles.json`,
        un archivo que ya no existe (los datos estaban en el SQLite). O sea:
        cada reinicio del bot dejaba las tablas vacias y los roles se perdian
        en silencio, sin ningun aviso. Es el mismo tipo de fallo que el del
        `state.json`: perder datos callados es peor que no arrancar.

        Ahora se lee lo que hay. Si la base es de una version mas nueva que
        este codigo, se deja en solo lectura igual que el estado.
        """
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            # El esquema se crea si falta, pero NO se borra nada. Estas dos
            # tablas son clave-valor: el esquema de verdad con columnas esta en
            # store.py (`usuarios`, `tracks`, `queue`...). Este modulo se migra
            # en la tarea siguiente.
            conn.execute("CREATE TABLE IF NOT EXISTS roles (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS users (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
            # Si el roles.json mas antiguo todavia esta (equipos viejos), se
            # importa UNA vez a la base; despues el archivo queda como esta.
            if ROLES_PATH.exists() and not self._hay_datos(conn):
                self._importar_json(conn)
            for fila in conn.execute("SELECT key, value FROM roles"):
                self._roles[fila["key"]] = fila["value"]
            for fila in conn.execute("SELECT key, value FROM users"):
                try:
                    self._users[fila["key"]] = json.loads(fila["value"])
                except (ValueError, TypeError):
                    continue
        finally:
            conn.close()

    @staticmethod
    def _hay_datos(conn: sqlite3.Connection) -> bool:
        return (
            conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] > 0
            or conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
        )

    def _importar_json(self, conn: sqlite3.Connection) -> None:
        """Del `roles.json` mas antiguo a la base. Si falla, la base sigue
        valida y vacia; no se rompe el arranque por eso."""
        try:
            with open(ROLES_PATH, "r", encoding="utf-8") as jf:
                data = json.load(jf)
            if not isinstance(data, dict):
                return
            for k, v in (data.get("roles") or {}).items():
                conn.execute("INSERT OR REPLACE INTO roles (key, value) VALUES (?, ?)", (k, v))
            for k, v in (data.get("users") or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO users (key, value) VALUES (?, ?)",
                    (k, json.dumps(v)),
                )
            conn.commit()
        except Exception:  # noqa: BLE001 - la migracion no debe tumbar el arranque
            pass

    def _save(self) -> None:
        """Persiste roles y usuarios en la base de datos SQLite.

        Solo lo que cambio: antes reescribia TODAS las filas en cada llamada,
        y como `_load` las borraba antes, cualquier fallo en medio dejaba la
        base vacia.
        """
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        for key, role in self._roles.items():
            cur.execute(
                "INSERT OR REPLACE INTO roles (key, value) VALUES (?, ?)", (key, role)
            )
        for key, entry in self._users.items():
            cur.execute(
                "INSERT OR REPLACE INTO users (key, value) VALUES (?, ?)",
                (key, json.dumps(entry)),
            )
        # Los que se quitaron en memoria tambien tienen que irse de la base.
        for key in [k for k in self._roles_borrados]:
            cur.execute("DELETE FROM roles WHERE key = ?", (key,))
        for key in [k for k in self._users_borrados]:
            cur.execute("DELETE FROM users WHERE key = ?", (key,))
        conn.commit()
        conn.close()
        self._roles_borrados.clear()
        self._users_borrados.clear()

    def get_role(self, user_id: int, default: str = "user") -> str:
        key = str(user_id)
        return self._roles.get(key, default)

    def has_role(self, user_id: int, required: str) -> bool:
        """Orden de roles: admin > dj > user. Un rol cumple con los inferiores."""
        rank = {"user": 0, "dj": 1, "admin": 2}
        return rank.get(self.get_role(user_id), 0) >= rank.get(required, 0)

    def register_user(self, user_id: int, name: str | None) -> bool:
        """Registra (o actualiza) un usuario conocido. Devuelve True si es nuevo.

        La fecha de entrada (joined_at) se marca UNA sola vez, la primera vez
        que el bot ve al usuario; actualizar el nombre no la rearma. No toca
        el rol: quien no tiene rol asignado es "user" por defecto.
        """
        key = str(user_id)
        existing = self._users.get(key)
        if existing is not None:
            if name and existing.get("name") != name:
                existing["name"] = name
                self._save()
            return False
        self._users[key] = {"name": name or key, "joined_at": int(time.time())}
        self._save()
        return True

    def set_role(self, user_id: int, role: str, name: str | None = None) -> dict:
        """Asigna el rol y garantiza la entrada del usuario en el registro."""
        if role not in VALID_ROLES:
            raise ValueError(f"Rol invalido: {role}. Validos: {sorted(VALID_ROLES)}")
        key = str(user_id)
        self._roles[key] = role
        entry = self._users.get(key)
        if entry is None:
            self._users[key] = {"name": name or key, "joined_at": int(time.time())}
        elif name:
            entry["name"] = name
        self._save()
        return {"id": user_id, "name": self.get_name(user_id) or key, "role": role}

    def toggle_user_role(self, user_id: int) -> str | None:
        """Alterna user <-> dj. Devuelve el rol nuevo, o None si no se toca
        (usuario admin o desconocido al sistema de roles)."""
        current = self.get_role(user_id)
        if current == "admin":
            return None
        new_role = "dj" if current == "user" else "user"
        self.set_role(user_id, new_role, self.get_name(user_id))
        return new_role

    def remove_user(self, user_id: int) -> bool:
        key = str(user_id)
        existed = key in self._roles or key in self._users
        if key in self._roles:
            del self._roles[key]
            self._roles_borrados.add(key)
        if key in self._users:
            del self._users[key]
            self._users_borrados.add(key)
        if existed:
            self._save()
        return existed

    def get_name(self, user_id: int) -> str | None:
        entry = self._users.get(str(user_id))
        if not entry:
            return None
        name = entry.get("name")
        return name if isinstance(name, str) else None

    def get_joined_at(self, user_id: int) -> int | None:
        entry = self._users.get(str(user_id))
        if not entry:
            return None
        joined = entry.get("joined_at")
        return joined if isinstance(joined, int) else None

    def all_users(self) -> dict[str, str]:
        return dict(self._roles)

    def get_all_with_role(self, role: str) -> list[int]:
        """Devuelve todos los user IDs que tienen ese rol exacto."""
        return [int(uid) for uid, r in self._roles.items() if r == role]

    def known_users(self) -> list[dict]:
        """Todos los usuarios conocidos, ordenados por nombre visible.

        Cada entrada: {"id", "name", "role", "joined_at"}. Quien no tiene rol
        asignado aparece como "user".
        """
        ids = set(self._roles) | set(self._users)
        items: list[dict] = []
        for uid in ids:
            entry = self._users.get(uid)
            items.append(
                {
                    "id": int(uid),
                    "name": (entry.get("name") if entry else None) or uid,
                    "role": self._roles.get(uid, "user"),
                    "joined_at": entry.get("joined_at") if entry else None,
                }
            )
        items.sort(key=lambda u: str(u["name"]).lower())
        return items