"""BotProcess: maneja el ciclo de vida del subproceso del bot.

Extraído de launcher.py para reutilización en launcher_web.py (Flask + pywebview).
"""

import os
import sys
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any


class BotProcess:
    """Maneja el ciclo de vida del proceso del bot."""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def start(self, session_data: Optional[Dict[str, Any]] = None) -> bool:
        """Inicia el bot como subprocess usando el runtime python portable.

        En el .exe (frozen) NO se relanza sys.executable (eso abría otra
        ventana del launcher): se usa runtime\\python\\python.exe junto al
        ejecutable, con src\\main.py al lado. La config viaja por variables
        de entorno (nunca se escribe en disco).
        """
        with self._lock:
            if self.process and self.process.poll() is None:
                return True  # Ya está corriendo

            try:
                env = os.environ.copy()
                session_data = session_data or {}
                bot_token = session_data.get("bot_token")
                if bot_token:
                    env["TELEGRAM_TOKEN"] = bot_token
                owner_id = session_data.get("admin_id")
                if owner_id:
                    env["OWNER_ID"] = str(owner_id)
                kick = session_data.get("kick_after_hours")
                if kick is not None:
                    env["KICK_AFTER_HOURS"] = str(kick)
                mpv_path = session_data.get("mpv_path")
                if mpv_path:
                    env["MPV_PATH"] = str(mpv_path)

                if getattr(sys, "frozen", False):
                    # La carpeta que contiene al .exe (portable).
                    base_dir = Path(os.path.dirname(os.path.abspath(sys.executable)))
                    python_exe = base_dir / "runtime" / "python" / "python.exe"
                    bot_script = base_dir / "src" / "main.py"
                else:
                    python_exe = Path(sys.executable)
                    bot_script = Path(__file__).resolve().parent / "main.py"

                self.process = subprocess.Popen(
                    [str(python_exe), str(bot_script)],
                    env=env,
                    cwd=str(python_exe.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                # Pequeña pausa para verificar que arranca bien
                time.sleep(0.5)
                if self.process.poll() is not None:
                    stdout, stderr = self.process.communicate()
                    print(f"Bot falló al iniciar: {stderr.decode()}")
                    return False
                return True
            except Exception as e:
                print(f"Error iniciando bot: {e}")
                return False

    def stop(self) -> bool:
        """Detiene el proceso del bot."""
        with self._lock:
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
                    return True
                except Exception as e:
                    print(f"Error deteniendo bot: {e}")
                    return False
            return True

    def is_running(self) -> bool:
        with self._lock:
            return self.process is not None and self.process.poll() is None

    def get_logs(self, lines: int = 50) -> list[str]:
        """Obtiene las últimas líneas de log del bot (stub: usa archivo/logger real si se implementa)."""
        # En implementación real se usaría un pipe o archivo de log
        return []


# Instancia global para uso en Flask routes
bot_process = BotProcess()