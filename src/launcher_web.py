"""launcher_web.py — Flask backend para el launcher pywebview.

Rutas:
  GET  /launcher           → HTML del formulario de conexión (templates/launcher.html)
  GET  /api/session        → Verifica si hay sesión válida (session.enc)
  POST /api/connect        → Valida token/admin, guarda session.enc, arranca bot
  GET  /api/status         → Estado del bot (running?)
  POST /shutdown           → Apaga bot + os._exit(0) (funnel único de salida)
"""

import sys
import os
import json
import threading
from pathlib import Path
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify, render_template, send_from_directory

# Asegurar que src/ esté en el path para imports
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

from bot_process import bot_process, validate_token
from security import SessionManager

# Rutas de archivos (portable: junto al exe si frozen)
if getattr(sys, "frozen", False):
    BASE_DIR = Path(os.path.dirname(os.path.abspath(sys.executable)))
else:
    BASE_DIR = SRC_DIR.parent

# Carpeta de recursos: en el bundle frozen vive en _internal (donde está
# este módulo); en desarrollo, en la raíz del repo. Con rutas absolutas el
# servidor local funciona igual empaquetado y en dev.
RES_DIR = Path(__file__).resolve().parent if getattr(sys, "frozen", False) else SRC_DIR.parent

SESSION_FILE = BASE_DIR / "session.enc"

app = Flask(
    __name__,
    template_folder=str(RES_DIR / "templates"),
    static_folder=str(RES_DIR / "static"),
)
app.config["JSON_AS_ASCII"] = False

# Tope de la auto-expulsión de invitados: 7 días en horas. Coincide con el
# max="168" del input en templates/launcher.html.
KICK_MAX_HOURS = 168


def _clamp_kick_hours(kick_hours) -> int:
    """Normaliza kick_after_hours: entero no negativo con tope KICK_MAX_HOURS."""
    try:
        valor = int(kick_hours) if str(kick_hours).isdigit() else 0
    except (ValueError, TypeError):
        valor = 0
    return min(max(0, valor), KICK_MAX_HOURS)


# ─── Helpers ──────────────────────────────────────────────────────
def _load_session() -> Optional[Dict[str, Any]]:
    """Intenta cargar session.enc via SessionManager."""
    try:
        sm = SessionManager()
        data = sm.load_session()
        return data
    except Exception:
        return None


def _save_session(data: Dict[str, Any]) -> bool:
    """Guarda sesión encriptada."""
    try:
        sm = SessionManager()
        sm.save_session(data)
        return True
    except Exception as e:
        print(f"Error guardando sesión: {e}")
        return False


def _clear_session() -> bool:
    """Borra session.enc (logout)."""
    try:
        sm = SessionManager()
        sm.clear_session()
        return True
    except Exception as e:
        print(f"Error limpiando sesión: {e}")
        return False


def _persist_username(handle: str) -> None:
    """Guarda el @handle del bot (getMe) en la sesión para el link t.me."""
    try:
        sm = SessionManager()
        data = sm.load_session() or {}
        data["bot_username"] = handle.lstrip("@")
        sm.save_session(data)
    except Exception as e:
        print(f"Error guardando bot_username: {e}")


# ─── Rutas API ────────────────────────────────────────────────────
# Anti-CSRF local: el launcher es un servidor localhost y un POST cross-site
# (fetch/form desde una página externa) podría tumbar la app. Los navegadores
# SIEMPRE envían el header Origin en los POST cross-site; aquí solo se acepta
# el origen propio (http://<Host>). Un cliente no-navegador (curl, el runtime)
# no manda Origin y no puede ser objetivo de CSRF del navegador.
@app.before_request
def _guard_csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin", "")
        if origin:
            host = request.headers.get("Host", "")
            if origin.rstrip("/") != f"http://{host}":
                return jsonify({"ok": False, "error": "Origen no permitido"}), 403


@app.route("/launcher")
def launcher_page():
    """Sirve el HTML del formulario de conexión."""
    return render_template("launcher.html")


@app.route("/api/session", methods=["GET"])
def api_session():
    """Verifica si existe sesión válida y devuelve sus datos (para precargar
    el formulario; el servidor es localhost, la sesión ya está en claro al
    descifrarla en esta máquina)."""
    data = _load_session()
    if data:
        resp = jsonify({
            "has_session": True,
            "bot_token": data.get("bot_token", ""),
            "admin_id": data.get("admin_id"),
            "kick_after_hours": data.get("kick_after_hours", 0),
            "bot_username": data.get("bot_username", ""),
        })
    else:
        resp = jsonify({"has_session": False})
    # La respuesta lleva datos sensibles en claro: prohibido cachear.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/save", methods=["POST"])
def api_save():
    """Guarda las credenciales y NO arranca el bot.

    Flujo: Guardar -> vuelve a la pantalla principal -> el usuario hace clic
    en Conectar. Si el bot corría con la config anterior, se detiene para que
    la nueva quede lista sin ejecutar nada.
    """
    payload = request.get_json(silent=True) or {}
    token = (payload.get("bot_token") or "").strip()
    admin_id_str = (payload.get("admin_id") or "").strip()
    kick_hours = payload.get("kick_after_hours", 0)

    if not token:
        return jsonify({"ok": False, "error": "Token del bot requerido"}), 400
    if not admin_id_str.isdigit() or int(admin_id_str) <= 0:
        return jsonify({"ok": False, "error": "ID de admin inválido (debe ser número positivo)"}), 400

    try:
        kick = _clamp_kick_hours(kick_hours)
    except (ValueError, TypeError):
        kick = 0

    config_data = {
        "bot_token": token,
        "admin_id": int(admin_id_str),
        "kick_after_hours": max(0, kick),
        "mpv_path": "runtime\\mpv\\mpv.exe",
    }

    if not _save_session(config_data):
        return jsonify({"ok": False, "error": "No se pudo guardar la sesión"}), 500

    if bot_process.is_running():
        bot_process.stop()

    return jsonify({"ok": True, "message": "Configuración guardada"})


@app.route("/api/start", methods=["POST"])
def api_start():
    """Conecta usando la sesión guardada (botón Conectar de la pantalla
    principal, sin pedir los datos otra vez)."""
    session = _load_session()
    if not (session and session.get("bot_token") and session.get("admin_id")):
        return jsonify({"ok": False, "error": "No hay sesión guardada. Inicie sesión primero."}), 400

    ok, info = validate_token(session["bot_token"])
    if not ok:
        return jsonify({"ok": False, "error": f"Token inválido: {info}"}), 400

    _persist_username(info)

    if bot_process.start(session):
        return jsonify({"ok": True, "message": f"Bot conectado ({info})", "bot_username": info.lstrip("@")})
    motivo = bot_process.last_error() or "Revise el token y el ID."
    return jsonify({"ok": False, "error": f"No se pudo iniciar el bot: {motivo}"}), 500


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Valida credenciales, guarda session.enc y arranca el bot."""
    payload = request.get_json(silent=True) or {}
    token = (payload.get("bot_token") or "").strip()
    admin_id_str = (payload.get("admin_id") or "").strip()
    kick_hours = payload.get("kick_after_hours", 0)

    if not token:
        return jsonify({"ok": False, "error": "Token del bot requerido"}), 400
    if not admin_id_str.isdigit() or int(admin_id_str) <= 0:
        return jsonify({"ok": False, "error": "ID de admin inválido (debe ser número positivo)"}), 400

    try:
        kick = _clamp_kick_hours(kick_hours)
    except (ValueError, TypeError):
        kick = 0

    # Validar el token ANTES de guardar y arrancar: un token invalido ya no
    # da el falso "Bot conectado" (el subproceso nacia, moria a los ~2s por
    # getMe fallido y la UI habia mentido).
    ok, info = validate_token(token)
    if not ok:
        return jsonify({"ok": False, "error": f"Token inválido: {info}"}), 400

    config_data = {
        "bot_token": token,
        "admin_id": int(admin_id_str),
        "kick_after_hours": max(0, kick),
        "mpv_path": "runtime\\mpv\\mpv.exe",
        "bot_username": info.lstrip("@"),
    }

    if not _save_session(config_data):
        return jsonify({"ok": False, "error": "No se pudo guardar la sesión"}), 500

    # Si el bot ya corría, reiniciar con nueva config
    if bot_process.is_running():
        bot_process.stop()

    if bot_process.start(config_data):
        return jsonify({"ok": True, "message": f"Bot conectado ({info})"})
    else:
        motivo = bot_process.last_error() or "Revise el token y el ID."
        return jsonify({"ok": False, "error": f"No se pudo iniciar el bot: {motivo}"}), 500


@app.route("/api/status", methods=["GET"])
def api_status():
    """Estado actual del bot."""
    running = bot_process.is_running()
    return jsonify({"bot_running": running})


@app.route("/api/log", methods=["POST"])
def api_log():
    """Telemetría de la UI: el JS vuelca pasos y errores a data/bot.log."""
    payload = request.get_json(silent=True) or {}
    texto = (payload.get("msg") or "").strip()
    if texto:
        bot_process._append_log(f"[UI] {texto}")
    return jsonify({"ok": True})


@app.route("/api/stop_bot", methods=["POST"])
def api_stop_bot():
    """Detiene el bot (mantiene sesión)."""
    if bot_process.stop():
        return jsonify({"ok": True, "message": "Bot detenido"})
    return jsonify({"ok": False, "error": "No se pudo detener"}), 500


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Funnel único de salida: detiene bot y mata el proceso (os._exit)."""
    print("Shutdown solicitado desde launcher → apagando...")
    try:
        bot_process.stop()
    except Exception:
        pass
    # os._exit en un hilo para que Flask responda antes de morir
    threading.Thread(target=lambda: os._exit(0), daemon=True).start()
    return jsonify({"ok": True, "message": "Cerrando..."})


# ─── Static files ─────────────────────────────────────────────────
# OJO: usar app.static_folder (absoluto = RES_DIR/static). Un path relativo
# ("static") se resuelve contra el CWD y da 404 cuando el exe se abre desde
# su propia carpeta (CWD = dist\ytremote, sin static/ al lado).
@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder or RES_DIR / "static", filename)


# ─── Main ─────────────────────────────────────────────────────────
def run_flask(port: int = 8081):
    """Arranca Flask en hilo daemon (para ser llamado desde main_launcher.py)."""
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    # Modo standalone para testing: python -m src.launcher_web
    run_flask()