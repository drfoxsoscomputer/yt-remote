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


def validate_token(token: str) -> tuple[bool, str]:
    """Valida el token del bot contra Telegram (getMe), sin dependencias extra.

    Devuelve (True, nombre_del_bot) si es valido, o (False, motivo) si no.
    Esto evita el falso "Conectado": antes el launcher arrancaba el subproceso
    y, si moria a los 2 segundos por token invalido, la UI ya habia mentido.
    """
    import json as _json
    import urllib.error
    import urllib.request

    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = _json.loads(r.read().decode("utf-8"))
        if data.get("ok"):
            auth = data.get("result", {})
            nombre = auth.get("username") or auth.get("first_name") or "bot"
            return True, f"@{nombre}"
        desc = data.get("description") or "Token rechazado por Telegram"
        return False, desc
    except urllib.error.HTTPError as e:
        try:
            body = _json.loads(e.read().decode("utf-8", "replace"))
            desc = body.get("description") or str(e.code)
        except Exception:
            desc = str(e.code)
        return False, desc
    except Exception as e:  # noqa: BLE001 - sin red / DNS / timeout
        return False, f"Sin conexion con Telegram: {e}"


class BotProcess:
    """Maneja el ciclo de vida del proceso del bot."""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._last_error: str = ""

    def last_error(self) -> str:
        """Ultimo motivo de fallo al iniciar el bot (para mostrarlo en la UI)."""
        with self._lock:
            return self._last_error

    def _log_path(self) -> Path:
        """data/bot.log junto al exe (misma ubicacion que session.enc)."""
        if getattr(sys, "frozen", False):
            base = Path(os.path.dirname(os.path.abspath(sys.executable)))
        else:
            base = Path(__file__).resolve().parent.parent
        return base / "data" / "bot.log"

    def _append_log(self, text: str) -> None:
        """Vuelca stdout+stderr del bot a data/bot.log (era invisible con --windowed)."""
        try:
            path = self._log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(text)
                f.write("\n")
        except Exception as exc:  # noqa: BLE001 - el log jamas debe tumbar al launcher
            print(f"Error escribiendo bot.log: {exc}")

    def start(self, session_data: Optional[Dict[str, Any]] = None) -> bool:
        """Inicia el bot como subprocess usando el runtime python portable.

        En el .exe (frozen) NO se relanza sys.executable (eso abría otra
        ventana del launcher): se usa runtime\\python\\python.exe junto al
        ejecutable, con src\\main.py al lado. La config viaja por variables
        de entorno (nunca se escribe en disco).

        La salida del bot (stdout+stderr) se vuelca a data/bot.log para que
        el arranque sea diagnosticable: en --windowed antes se perdía.
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

                py = Path(sys.executable)
                if getattr(sys, "frozen", False):
                    # La carpeta que contiene al .exe (portable).
                    base_dir = Path(os.path.dirname(os.path.abspath(sys.executable)))
                    python_exe = base_dir / "runtime" / "python" / "python.exe"
                    bot_script = base_dir / "src" / "main.py"
                else:
                    python_exe = py
                    bot_script = Path(__file__).resolve().parent / "main.py"

                # Validar de antemano: faltan archivos del bundle → error claro.
                if not python_exe.is_file():
                    self._last_error = (
                        f"No se encontro {python_exe}. "
                        "Ejecuta la app desde la carpeta completa (runtime junto al .exe)."
                    )
                    print(self._last_error)
                    return False
                if not bot_script.is_file():
                    self._last_error = (
                        f"No se encontro {bot_script}. Falta la carpeta src/ junto al .exe."
                    )
                    print(self._last_error)
                    return False

                self._append_log(
                    f"--- [{time.strftime('%Y-%m-%d %H:%M:%S')}] lanzando bot: "
                    f"{python_exe} {bot_script}"
                )
                self.process = subprocess.Popen(
                    [str(python_exe), str(bot_script)],
                    env=env,
                    cwd=str(python_exe.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                # Pequeña pausa para verificar que arranca bien
                time.sleep(0.6)
                if self.process.poll() is not None:
                    try:
                        salida, _ = self.process.communicate(timeout=2)
                    except Exception:
                        salida = b""
                    texto = salida.decode("utf-8", "replace").strip()
                    self._append_log(texto or "(sin salida)")
                    # Resumir en una linea el motivo: tipicamente la excepcion final
                    motivo = "" if not texto else texto.splitlines()[-1]
                    if len(motivo) > 400:
                        motivo = motivo[-400:]
                    self._last_error = (
                        f"El bot arranco y se cerro enseguida{': ' + motivo if motivo else '.'}"
                    )
                    print(self._last_error)
                    return False

                # Proceso vivo: el seguimiento (muerte posterior, log) queda en
                # un hilo de vigia que escribe los logs cuando el bot termine.
                self._monitor_dead()
                return True
            except Exception as e:
                self._last_error = f"Error iniciando bot: {e}"
                print(self._last_error)
                return False

    def _monitor_dead(self) -> None:
        """Hilo vigia: cuando el bot muera, vuelca su salida final a bot.log.

        Asi un proceso que 'conecto' de mentira (vivo 0.6s) deja rastro: si
        muere a los 2 segundos porque el token es invalido, el por que queda
        registrado aunque la UI ya hubiera mostrado el estado previo.
        """

        def _watch() -> None:
            proc = self.process
            if proc is None:
                return
            try:
                salida, _ = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    salida, _ = proc.communicate(timeout=5)
                except Exception:
                    salida = b""
            except Exception:
                salida = b""
            with self._lock:
                if self.process is proc:
                    self.process = None
            texto = salida.decode("utf-8", "replace").strip()
            if texto:
                self._append_log(texto)

        threading.Thread(target=_watch, daemon=True).start()

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