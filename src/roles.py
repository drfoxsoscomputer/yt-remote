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
        self._load()

    def _load(self) -> None:
        """Carga los roles y usuarios desde la base de datos SQLite."""
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS roles (key TEXT PRIMARY KEY, value TEXT)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS users (key TEXT PRIMARY KEY, value TEXT)"
            )
            cur.execute("DELETE FROM roles")
            cur.execute("DELETE FROM users")
            # Migración: si existe el archivo JSON viejo, importamos sus datos
            if ROLES_PATH.exists():
                try:
                    with open(ROLES_PATH, "r", encoding="utf-8") as jf:
                        data = json.load(jf)
                    if isinstance(data, dict) and "roles" in data:
                        for k, v in data.get("roles", {}).items():
                            cur.execute("INSERT INTO roles (key, value) VALUES (?, ?)", (k, v))
                    if isinstance(data, dict) and "users" in data:
                        for k, v in data.get("users", {}).items():
                            cur.execute(
                                "INSERT INTO users (key, value) VALUES (?, ?)",
                                (k, json.dumps(v)),
                            )
                except Exception:
                    pass  # Si falla la migración, la BD queda vacía pero válida
            conn.commit()
        finally:
            conn.close()

    def _save(self) -> None:
        """Persiste roles y usuarios en la base de datos SQLite."""
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        for key, role in self._roles.items():
            cur.execute("INSERT OR REPLACE INTO roles (key, value) VALUES (?, ?)", (key, role))
        for key, entry in self._users.items():
            cur.execute(
                "INSERT OR REPLACE INTO users (key, value) VALUES (?, ?)",
                (key, json.dumps(entry)),
            )
        conn.commit()
        conn.close()

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
        if key in self._users:
            del self._users[key]
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