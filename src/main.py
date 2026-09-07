"""Entry point de YT-Remote.

Arranca el bot de Telegram que controla mpv para reproducir
videos de YouTube en la PC.
"""

import atexit
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Puerto local del lock de instancia unica: solo una copia del bot en esta PC.
_SINGLETON_PORT = 47631


def _acquire_singleton() -> socket.socket | None:
    """Lock de instancia local (puerto TCP en localhost).

    Dos bot con el mismo token se pisan en Telegram (conflicto de getUpdates)
    y los mensajes se pierden en silencio. Si el puerto ya esta tomado, la
    segunda copia no arranca y avisa al usuario.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", _SINGLETON_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def main() -> None:
    lock = _acquire_singleton()
    if lock is None:
        print("Ya hay una instancia del bot corriendo en esta PC.")
        print("Cierrala antes de arrancar otra (tambien en la consola del .bat).")
        return

    from bot import YTRemoteBot
    from config import load_config

    config = load_config()
    bot = YTRemoteBot(config)

    # Al salir el bot, matar tambien mpv: si no, cada cierre deja un proceso
    # mpv huerfano con su ventana abierta (se acumulan en segundo plano).
    atexit.register(bot.player._quit)
    # Persistir el estado antes de salir: el debounce podria tener un
    # flush pendiente y un Ctrl+C no lo ejecuta.
    atexit.register(bot._persist_flush)

    app = bot.build()
    # drop_pending_updates: los mensajes que llegaron mientras la PC estuvo
    # apagada NO quedan en cola para ejecutarse (el backend los descarta).
    # El aviso de que se perdieron lo manda el bot en post_init.
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
