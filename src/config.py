"""Carga de configuracion desde config.json."""

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"


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

    config = Config(
        token=str(data["telegram_token"]),
        mpv_path=str(data.get("mpv_path", "mpv")),
        default_role=str(data.get("default_role", "user")),
        max_results=int(data.get("max_results", 5)),
    )

    if config.token == "TU_TOKEN_AQUI":
        raise ValueError(
            "El token de Telegram no esta configurado. "
            f"Edita {CONFIG_PATH} y coloca tu token de @BotFather."
        )

    return config
