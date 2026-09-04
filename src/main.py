"""Entry point de YT-Remote.

Arranca el bot de Telegram que controla mpv para reproducir
videos de YouTube en la PC.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    from bot import YTRemoteBot
    from config import load_config

    config = load_config()
    bot = YTRemoteBot(config)
    app = bot.build()
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
