"""Carga de configuracion desde config.json, .env y variables de entorno.

El orden de precedencia del token es: variables de entorno (las pasa el
launcher al subproceso del bot) -> archivo .env (que NO se sube a GitHub)
-> config.json (placeholder de respaldo).
"""

import json
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # Dentro del .exe: la data vive al lado del ejecutable (portable).
    PROJECT_ROOT = Path(os.path.dirname(os.path.abspath(sys.executable)))
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
ENV_PATH = PROJECT_ROOT / ".env"


def _read_dotenv() -> dict[str, str]:
    """Lee variables del archivo .env (formato CLAVE=valor, una por linea)."""
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Config:
    """Configuracion de YT-Remote."""

    def __init__(
        self,
        token: str,
        mpv_path: str,
        default_role: str,
        max_results: int,
        owner_id: int | None = None,
        allowed_chat_id: int | None = None,
        kick_after_hours: float = 0,
    ) -> None:
        self.token = token
        self.mpv_path = mpv_path
        self.default_role = default_role
        self.max_results = max_results
        self.owner_id = owner_id
        self.allowed_chat_id = allowed_chat_id
        self.kick_after_hours = kick_after_hours


def load_config() -> Config:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    raw_mpv = str(data.get("mpv_path", "mpv"))
    mpv_path = Path(raw_mpv)
    if not mpv_path.is_absolute():
        mpv_path = PROJECT_ROOT / mpv_path
    mpv_path = str(mpv_path)

    # Orden de precedencia: variable de entorno (la setea el launcher) ->
    # archivo .env -> config.json (placeholder de respaldo).
    env = _read_dotenv()
    token = os.environ.get(
        "TELEGRAM_TOKEN",
        env.get("TELEGRAM_TOKEN", str(data.get("telegram_token", ""))),
    )

    # Ruta de mpv: entorno/JSON (relativa a la raiz del proyecto).
    env_mpv = os.environ.get("MPV_PATH")
    raw_mpv = env_mpv or str(data.get("mpv_path", "mpv"))
    mpv_path = Path(raw_mpv)
    if not mpv_path.is_absolute():
        mpv_path = PROJECT_ROOT / mpv_path
    mpv_path = str(mpv_path)

    def _to_int(value: str | None) -> int | None:
        if value is None or str(value).strip() == "":
            return None
        try:
            return int(str(value).strip())
        except ValueError:
            return None

    def _to_int_env(key: str) -> int | None:
        return _to_int(os.environ.get(key))

    owner_id = _to_int_env("OWNER_ID")
    if owner_id is None:
        owner_id = _to_int(env.get("OWNER_ID"))
    allowed_chat_id = _to_int_env("ALLOWED_CHAT_ID")
    if allowed_chat_id is None:
        allowed_chat_id = _to_int(env.get("ALLOWED_CHAT_ID"))

    config = Config(
        token=token,
        mpv_path=mpv_path,
        default_role=str(data.get("default_role", "user")),
        max_results=int(data.get("max_results", 5)),
        owner_id=owner_id,
        allowed_chat_id=allowed_chat_id,
    )

    # Expulsion automatica de invitados: horas de tolerancia para un usuario
    # con rol 'user'. 0 o ausente = desactivada. Un valor invalido cae a 0.
    try:
        kick_raw = os.environ.get("KICK_AFTER_HOURS", data.get("kick_after_hours", 0))
        config.kick_after_hours = max(0.0, float(kick_raw or 0))
    except (TypeError, ValueError):
        config.kick_after_hours = 0.0

    if config.token in ("", "TU_TOKEN_AQUI"):
        raise ValueError(
            "El token de Telegram no está configurado.\n"
            f"1. Cree el archivo .env en {PROJECT_ROOT}\n"
            "2. Escriba en el archivo, con sus datos reales:\n"
            "      TELEGRAM_TOKEN=su_token_de_botfather\n"
            "      OWNER_ID=123456789\n"
            "3. OWNER_ID es su ID de Telegram (se consigue con @userinfobot).\n"
            "(El .env no se sube a GitHub; queda solo en su máquina)."
        )

    return config
