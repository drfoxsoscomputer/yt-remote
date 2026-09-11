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

from bot_process import bot_process
from security import SessionManager

# Rutas de archivos (portable: junto al exe si frozen)
if getattr(sys, "frozen", False):
    BASE_DIR = Path(os.path.dirname(os.path.abspath(sys.executable)))
else:
    BASE_DIR = SRC_DIR.parent

SESSION_FILE = BASE_DIR / "session.enc"

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_AS_ASCII"] = False


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


# ─── Rutas API ────────────────────────────────────────────────────
@app.route("/launcher")
def launcher_page():
    """Sirve el HTML del formulario de conexión."""
    return render_template("launcher.html")


@app.route("/api/session", methods=["GET"])
def api_session():
    """Verifica si existe sesión válida."""
    data = _load_session()
    if data:
        return jsonify({
            "has_session": True,
            "bot_token": data.get("bot_token", "")[:10] + "..." if data.get("bot_token") else "",
            "admin_id": data.get("admin_id"),
            "kick_after_hours": data.get("kick_after_hours", 0),
        })
    return jsonify({"has_session": False})


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
        kick = int(kick_hours) if str(kick_hours).isdigit() else 0
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

    # Si el bot ya corría, reiniciar con nueva config
    if bot_process.is_running():
        bot_process.stop()

    if bot_process.start(config_data):
        return jsonify({"ok": True, "message": "Bot conectado"})
    else:
        return jsonify({"ok": False, "error": "No se pudo iniciar el bot. Revisa token e ID."}), 500


@app.route("/api/status", methods=["GET"])
def api_status():
    """Estado actual del bot."""
    running = bot_process.is_running()
    return jsonify({"bot_running": running})


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


# ─── Static files (para desarrollo sin build) ─────────────────────
@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


# ─── Main ─────────────────────────────────────────────────────────
def run_flask(port: int = 8081):
    """Arranca Flask en hilo daemon (para ser llamado desde main_launcher.py)."""
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    # Modo standalone para testing: python -m src.launcher_web
    run_flask()