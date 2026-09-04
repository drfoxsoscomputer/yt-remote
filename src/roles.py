"""Sistema de roles de YT-Remote.

Roles:
- admin: acceso total
- dj: gestion de reproduccion (play, pause, skip, cola, volumen)
- user: solo pedir canciones
"""

import json
from pathlib import Path

VALID_ROLES = {"admin", "dj", "user"}
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ROLES_PATH = DATA_DIR / "roles.json"


class RoleManager:
    """Gestiona usuarios y sus roles, persistidos en data/roles.json."""

    def __init__(self) -> None:
        self._roles: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if ROLES_PATH.exists():
            with ROLES_PATH.open("r", encoding="utf-8") as f:
                self._roles = json.load(f)

    def _save(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        with ROLES_PATH.open("w", encoding="utf-8") as f:
            json.dump(self._roles, f, indent=2, ensure_ascii=False)

    def get_role(self, user_id: int, default: str = "user") -> str:
        key = str(user_id)
        return self._roles.get(key, default)

    def has_role(self, user_id: int, required: str) -> bool:
        """Orden de roles: admin > dj > user. Un rol cumple con los inferiores."""
        rank = {"user": 0, "dj": 1, "admin": 2}
        return rank.get(self.get_role(user_id), 0) >= rank.get(required, 0)

    def set_role(self, user_id: int, role: str) -> None:
        if role not in VALID_ROLES:
            raise ValueError(f"Rol invalido: {role}. Validos: {sorted(VALID_ROLES)}")
        self._roles[str(user_id)] = role
        self._save()

    def remove_user(self, user_id: int) -> bool:
        key = str(user_id)
        existed = key in self._roles
        if existed:
            del self._roles[key]
            self._save()
        return existed

    def all_users(self) -> dict[str, str]:
        return dict(self._roles)
