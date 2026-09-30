"""Sistema de roles de YT-Remote.

Roles:
- admin: acceso total
- dj: gestion de reproduccion (play, pause, skip, cola, volumen)
- user: solo pedir canciones

Vive en la tabla `usuarios` de la MISMA base que el estado (`data/ytremote.db`),
no en un `roles.db` aparte. Antes tenia su propio archivo, y eso no era solo
desperdicio: el `store` renombraba ese archivo a `.migrado` al migrar, y a
partir de ahi `RoleManager` abria un `roles.db` NUEVO y vacio en cada llamada.
El primer `SELECT` revienta con `no such table: roles`, y como `register_user`
se llama en el handler que re-coloca la tarjeta, el bot moria ahi: la tarjeta
dejaba de re-renderizarse y los roles dejaban de funcionar. Un archivo por
entidad no es solo mas archivos: es un archivo mas que se puede renombrar por
debajo de los pies del otro.
"""

import logging
import time

import store as store_mod
from store import Store

logger = logging.getLogger(__name__)

VALID_ROLES = {"admin", "dj", "user"}
# La MISMA base del estado. Se deja el nombre `DB_PATH` porque las pruebas lo
# parchean para aislar la base; `RoleManager` lo lee en cada __init__.
DB_PATH = store_mod.DB_PATH
ROLES_PATH = store_mod.data_dir() / "roles.json"


class RoleManager:
    """Gestiona usuarios, sus roles y nombres, en la tabla `usuarios`."""

    def __init__(self) -> None:
        self._roles: dict[str, str] = {}
        self._users: dict[str, dict[str, str | int]] = {}
        # Lo que se borro en memoria y hay que borrar tambien en la base.
        self._roles_borrados: set[str] = set()
        self._users_borrados: set[str] = set()
        # Se lee el global DB_PATH al construir, no al importar: las pruebas
        # parchean `roles.DB_PATH` DESPUES de importar el modulo.
        self._store = Store(DB_PATH)
        self._load()

    @property
    def solo_lectura(self) -> bool:
        """La base es de una version mas nueva que este bot."""
        return self._store.solo_lectura

    def _load(self) -> None:
        """Carga los roles y usuarios desde `usuarios`.

        NO borra nada. Antes hacia `DELETE FROM roles` y `DELETE FROM users` en
        CADA arranque y solo volvia a llenar las tablas desde `data/roles.json`,
        un archivo que ya no existe (los datos estaban en el SQLite). O sea:
        cada reinicio del bot dejaba las tablas vacias y los roles se perdian
        en silencio, sin ningun aviso. Es el mismo tipo de fallo que el del
        `state.json`: perder datos callados es peor que no arrancar.
        """
        for fila in self._store.leer_usuarios():
            uid = str(fila["user_id"])
            self._users[uid] = {
                "name": fila["name"],
                "joined_at": int(fila["joined_at"] or 0),
            }
            # Se guarda el rol tal cual, incluso "user": `all_users()` devuelve
            # solo los que tienen rol asignado.
            self._roles[uid] = fila["role"] or "user"

    def _save(self) -> None:
        """Persiste roles y usuarios en `usuarios`.

        Solo lo que cambio. Si la base es de una version mas nueva que este
        bot, NO se escribe (igual que el estado): tus datos quedan intactos.
        """
        if self._store.solo_lectura:
            logger.error(
                "NO se guardan los roles: %s. Los roles quedan como estaban.",
                self._store.motivo_solo_lectura,
            )
            return
        for key in list(self._roles_borrados) + list(self._users_borrados):
            self._store.borrar_usuario(int(key))
        for key, role in self._roles.items():
            entry = self._users.get(key) or {}
            self._store.guardar_usuario(
                int(key),
                str(entry.get("name") or key),
                role,
                int(entry.get("joined_at") or 0),
            )
        for key, entry in self._users.items():
            if key in self._roles:
                continue  # ya se guardo con su rol
            self._store.guardar_usuario(
                int(key), str(entry.get("name") or key), "user", int(entry.get("joined_at") or 0)
            )
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