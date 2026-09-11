#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main_launcher.py — Entry point del launcher pywebview + Flask.

Arquitectura (estilo AlbionHelper):
  - Mutex instancia única (CreateMutexW + MessageBoxW si duplicado)
  - Check WebView2 runtime antes de arrancar
  - Hilo Flask daemon en puerto 8081
  - webview.create_window (420x640, centrada en area de trabajo DPI-aware, hidden=False)
  - js_api=LauncherApi() expone: connect, get_status, stop_bot, quit_app, logout, splash_listo
  - events.closing -> diálogo nativo (Sí=minimizar tray / No=salir)
  - webview.start() bloqueante
  - finally -> _apagar_todo() (stop bot + os._exit(0))
  - Tray icon (pystray): "Abrir app" / "Detener bot" / "Salir"
"""

import sys
import os
import ctypes
import ctypes.wintypes
import threading
import time
import json
from pathlib import Path

# Asegurar src/ en path
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

import webview
from launcher_web import app as flask_app, bot_process, _load_session, _save_session, _clear_session

# ─── Configuración ────────────────────────────────────────────────
PORT = 8081
APP_NAME = "YT-Remote"
MUTEX_NAME = "Local\\ytremote_launcher"
WINDOW_WIDTH = 420
WINDOW_HEIGHT = 640

# Estado global
_window = None
_tray = None
_tray_image = None
_cerrar_programatico = False
_flask_thread = None

# ─── Mutex instancia única ────────────────────────────────────────
def _acquire_singleton_mutex() -> bool:
    """Solo UNA ventana del launcher por usuario."""
    if sys.platform != "win32":
        return True
    global _MUTEX_HANDLE
    try:
        ERROR_ALREADY_EXISTS = 183
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not mutex:
            return True
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(mutex)
            return False
        _MUTEX_HANDLE = mutex
        return True
    except Exception:
        return True


# ─── Check WebView2 runtime ───────────────────────────────────────
def _webview2_disponible() -> bool:
    """True si el runtime de WebView2 está instalado (Win10/11 lo traen)."""
    try:
        import winreg
    except Exception:
        return True  # fuera de Windows (dev): no bloquear
    GUID = "Microsoft\\EdgeUpdate\\Clients\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for vista in (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + GUID),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + GUID),
        (winreg.HKEY_CURRENT_USER, "SOFTWARE\\" + GUID),
    ):
        try:
            with winreg.OpenKey(vista[0], vista[1]):
                return True
        except OSError:
            continue
    return False


# ─── Centrado de ventana con corrección DPI ──────────────────────
def _centrado_xy(ancho: int, alto: int):
    """Coordenadas lógicas para centrar la ventana en el área de trabajo
    (pantalla menos la barra de inicio), horizontal y verticalmente.

    Usa SPI_GETWORKAREA para excluir la barra de inicio y convierte los
    píxeles físicos a lógicos (pywebview/WinForms re-escala x/y por DPI).
    """
    try:
        u = ctypes.windll.user32
        u.SystemParametersInfoW.argtypes = [
            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
        ]
        u.SystemParametersInfoW.restype = ctypes.c_bool
        try:
            escala = u.GetDpiForSystem() / 96.0
        except Exception:
            escala = 1.0
        if escala <= 0:
            escala = 1.0

        # Área de trabajo (excluye barra de inicio) en píxeles físicos
        rect = ctypes.wintypes.RECT()
        if not u.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return None

        # Convertir a coordenadas lógicas y centrar dentro del área útil
        lx = int((rect.right - rect.left) / escala)
        ly = int((rect.bottom - rect.top) / escala)
        if lx <= ancho or ly <= alto:
            return None
        ox = int(rect.left / escala)
        oy = int(rect.top / escala)
        return (ox + (lx - ancho) // 2, oy + (ly - alto) // 2)
    except Exception:
        return None


# ─── JS API expuesta a la ventana webview ────────────────────────
class LauncherApi:
    """Métodos llamados desde JavaScript via window.pywebview.api"""

    def splash_listo(self):
        """Llamado por launcher.html cuando la página está pintada."""
        global _window
        if _window:
            _window.show()

    def connect(self, bot_token: str, admin_id: str, kick_after_hours: int = 0) -> dict:
        """Valida credenciales, guarda session.enc y arranca el bot."""
        token = (bot_token or "").strip()
        admin_id_str = (admin_id or "").strip()
        try:
            kick = int(kick_after_hours) if str(kick_after_hours).isdigit() else 0
        except (ValueError, TypeError):
            kick = 0

        if not token:
            return {"ok": False, "error": "Token del bot requerido"}
        if not admin_id_str.isdigit() or int(admin_id_str) <= 0:
            return {"ok": False, "error": "ID de admin inválido (debe ser número positivo)"}

        config_data = {
            "bot_token": token,
            "admin_id": int(admin_id_str),
            "kick_after_hours": max(0, kick),
            "mpv_path": "runtime\\mpv\\mpv.exe",
        }

        if not _save_session(config_data):
            return {"ok": False, "error": "No se pudo guardar la sesión"}

        # Si el bot ya corría, reiniciar con nueva config
        if bot_process.is_running():
            bot_process.stop()

        if bot_process.start(config_data):
            # Ocultar ventana launcher y minimizar a tray
            global _window
            if _window:
                _window.hide()
            _minimizar_a_tray()
            return {"ok": True, "message": "Bot conectado"}
        else:
            return {"ok": False, "error": "No se pudo iniciar el bot. Revisa token e ID."}

    def get_status(self) -> dict:
        """Estado actual del bot."""
        return {"bot_running": bot_process.is_running()}

    def stop_bot(self) -> dict:
        """Detiene el bot (mantiene sesión)."""
        if bot_process.stop():
            return {"ok": True, "message": "Bot detenido"}
        return {"ok": False, "error": "No se pudo detener"}

    def quit_app(self):
        """Cierra la app completamente (para bot + os._exit)."""
        global _cerrar_programatico
        _cerrar_programatico = True
        _apagar_todo()

    def logout(self):
        """Cierra sesión: borra session.enc, detiene bot, vuelve al formulario."""
        if bot_process.is_running():
            bot_process.stop()
        _clear_session()
        global _window
        if _window:
            # Recargar launcher.html para mostrar formulario
            _window.load_url(f"http://127.0.0.1:{PORT}/launcher")


# ─── Tray icon ───────────────────────────────────────────────────
def _crear_tray_icon():
    """Crea el icono del system tray con menú."""
    global _tray, _tray_image
    if not _tray:
        try:
            import pystray
            from pystray import MenuItem as TrayMenuItem
            from PIL import Image

            # Usar el .ico generado si existe, sino crear uno simple
            ico_path = Path(__file__).resolve().parent.parent / "ytremote.ico"
            if ico_path.exists():
                _tray_image = Image.open(ico_path)
            else:
                _tray_image = Image.new('RGBA', (64, 64), (26, 26, 46, 255))
                from PIL import ImageDraw
                draw = ImageDraw.Draw(_tray_image)
                draw.polygon([(20, 16), (20, 48), (48, 32)], fill=(255, 0, 0, 255))

            menu = pystray.Menu(
                TrayMenuItem("YT-Remote", None, enabled=False),
                TrayMenuItem("Abrir app", _mostrar_ventana, default=True),
                TrayMenuItem("Detener bot", _detener_bot_desde_tray),
                TrayMenuItem("Salir", _salir_desde_tray),
            )

            _tray = pystray.Icon("ytremote", _tray_image, APP_NAME, menu)
            threading.Thread(target=_tray.run, daemon=True).start()
        except Exception as e:
            print(f"No se pudo crear tray icon: {e}")


def _mostrar_ventana():
    """Muestra la ventana principal."""
    global _window
    if _window:
        _window.show()
        _window.restore()


def _detener_bot_desde_tray():
    """Detiene el bot desde el tray."""
    if bot_process.is_running():
        bot_process.stop()
        # Recargar UI para mostrar formulario
        global _window
        if _window:
            _window.load_url(f"http://127.0.0.1:{PORT}/launcher")


def _salir_desde_tray():
    """Cierra la aplicación desde el tray."""
    global _cerrar_programatico
    _cerrar_programatico = True
    _apagar_todo()


def _minimizar_a_tray():
    """Minimiza la ventana al tray."""
    global _window
    if _window:
        _window.hide()
    _crear_tray_icon()


# ─── Cierre real (funnel único) ──────────────────────────────────
def _apagar_todo():
    """Cierre REAL: detiene bot, tray, y mata el proceso."""
    print("Apagando YT-Remote...")
    try:
        bot_process.stop()
    except Exception:
        pass
    global _tray
    if _tray is not None:
        try:
            _tray.stop()
        except Exception:
            pass
        _tray = None
    # os._exit en hilo para que webview.start() retorne limpio
    threading.Thread(target=lambda: os._exit(0), daemon=True).start()


# ─── Evento de cierre de ventana ─────────────────────────────────
def _on_closing():
    """Al cerrar la ventana con la X: diálogo nativo (Sí=tray / No=salir)."""
    global _cerrar_programatico, _window
    if _cerrar_programatico:
        return  # cierre programático: permitir

    # Diálogo nativo MessageBoxW (pywebview no tiene confirm dialog propio)
    MB_YESNO = 0x00000004
    MB_ICONQUESTION = 0x00000020
    MB_SYSTEMMODAL = 0x00001000
    IDYES = 6
    IDNO = 7

    resultado = ctypes.windll.user32.MessageBoxW(
        0,
        "¿Minimizar al lado del reloj? (El bot seguirá corriendo)\n\nNo = Detener bot y salir",
        APP_NAME,
        MB_YESNO | MB_ICONQUESTION | MB_SYSTEMMODAL,
    )

    if resultado == IDYES:
        _minimizar_a_tray()
        return False  # cancela el cierre: la ventana queda viva, oculta
    else:
        _cerrar_programatico = True
        _apagar_todo()
        return True  # permite el cierre (aunque os._exit mata el proceso)


# ─── Main ────────────────────────────────────────────────────────
def main():
    # 0) Instancia única
    if not _acquire_singleton_mutex():
        ctypes.windll.user32.MessageBoxW(
            0,
            "YT-Remote ya está en ejecución.\n"
            "Buscá la ventana abierta o el ícono junto al reloj.",
            APP_NAME,
            0x00000040,  # MB_ICONINFORMATION
        )
        return

    # 0.1) Check WebView2
    if not _webview2_disponible():
        ctypes.windll.user32.MessageBoxW(
            0,
            "YT-Remote necesita el runtime 'Microsoft Edge WebView2'.\n\n"
            "Windows 10/11 suelen traerlo instalado. Instalalo desde\n"
            "Windows Update o descargando 'Evergreen Standalone Installer'\n"
            "del sitio oficial de Microsoft Edge WebView2.",
            APP_NAME,
            0x00000030,  # MB_ICONWARNING
        )
        return

    # 0.2) Arrancar Flask en hilo daemon
    global _flask_thread
    _flask_thread = threading.Thread(
        target=lambda: flask_app.run(
            host="127.0.0.1", port=PORT, threaded=True, debug=False, use_reloader=False
        ),
        daemon=True,
    )
    _flask_thread.start()

    # Esperar a que Flask esté listo
    for _ in range(50):  # 5s max
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/launcher", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)
    else:
        print("No se pudo iniciar el servidor Flask a tiempo.", file=sys.stderr)
        sys.exit(1)

    # 1) Crear ventana webview (se muestra cuando la GUI arranca)
    global _window
    xy = _centrado_xy(WINDOW_WIDTH, WINDOW_HEIGHT)
    _window = webview.create_window(
        APP_NAME,
        f"http://127.0.0.1:{PORT}/launcher",
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        x=xy[0] if xy else None,
        y=xy[1] if xy else None,
        resizable=True,
        text_select=True,
        hidden=False,
        background_color="#1a1a2e",
        js_api=LauncherApi(),
    )
    _window.events.closing += _on_closing

    # 2) Auto-conectar si hay sesión guardada. Se ejecuta dentro de
    #    webview.start(func): ahí la GUI ya está inicializada y las llamadas
    #    a la ventana (hide/show) son seguras. Antes de start() están prohibidas.
    session = _load_session()

    def _post_start_connect():
        """Corre cuando la GUI ya inicializó; arranca el bot si hay sesión."""
        if not (session and session.get("bot_token") and session.get("admin_id")):
            return
        if bot_process.start(session):
            # Ocultar launcher y minimizar a tray
            if _window:
                _window.hide()
            _minimizar_a_tray()

    # 3) Crear tray icon (perezoso, tras mostrar ventana)
    def _start_tray_lazy():
        time.sleep(0.8)
        _crear_tray_icon()
    threading.Thread(target=_start_tray_lazy, daemon=True).start()

    # 4) Event loop bloqueante. El callback se dispara al iniciar la GUI.
    try:
        webview.start(_post_start_connect)
    except Exception as e:
        print(f"Error al iniciar la ventana: {e}")
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Error al iniciar la ventana:\n\n{e}",
                APP_NAME,
                0x00000010,  # MB_ICONERROR
            )
        except Exception:
            pass
    finally:
        _apagar_todo()


if __name__ == "__main__":
    main()