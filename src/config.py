"""Carga de configuracion desde config.json y .env.

El token de Telegram se lee PRIMERO del archivo .env (que NO se sube a
GitHub, por seguridad). config.json mantiene valores no sensibles y un
placeholder de token como respaldo.
"""

import json
from pathlib import Path

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

    def __init__(self, token: str, mpv_path: str, default_role: str, max_results: int) -> None:
        self.token = token
        self.mpv_path = mpv_path
        self.default_role = default_role
        self.max_results = max_results


def load_config() -> Config:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    raw_mpv = str(data.get("mpv_path", "mpv"))
    mpv_path = Path(raw_mpv)
    if not mpv_path.is_absolute():
        mpv_path = PROJECT_ROOT / mpv_path
    mpv_path = str(mpv_path)

    # El token real va en .env (no se sube a GitHub). El de config.json es
    # solo un placeholder de respaldo.
    env = _read_dotenv()
    token = env.get("TELEGRAM_TOKEN", str(data.get("telegram_token", "")))

    config = Config(
        token=token,
        mpv_path=mpv_path,
        default_role=str(data.get("default_role", "user")),
        max_results=int(data.get("max_results", 5)),
    )

    if config.token in ("", "TU_TOKEN_AQUI"):
        raise ValueError(
            "El token de Telegram no esta configurado.\n"
            f"1. Crea el archivo .env en {PROJECT_ROOT}\n"
            "2. Escribe en el:  TELEGRAM_TOKEN=tu_token_de_botfather\n"
            "(El .env no se sube a GitHub; el token queda solo en tu maquina)."
        )

    return config
