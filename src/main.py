"""Entry point de YT-Remote.

Arranca el bot de Telegram que controla mpv para reproducir
videos de YouTube en la PC.
"""

import atexit
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    from bot import YTRemoteBot
    from config import load_config

    config = load_config()
    bot = YTRemoteBot(config)

    # Al salir el bot, matar tambien mpv: si no, cada cierre deja un proceso
    # mpv huerfano con su ventana abierta (se acumulan en segundo plano).
    atexit.register(bot.player._quit)

    app = bot.build()
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
