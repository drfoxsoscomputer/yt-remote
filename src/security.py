#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Manejo seguro de sesion unificada con DPAPI nativo de Windows (crypt32).
Archivo unico: data/session.enc

La clave la gestiona Windows y queda ligada al usuario y a la maquina:
session.enc solo se puede descifrar en la misma PC/usuario donde se creo
(por eso viaja el archivo pero no la credencial). Sin cryptography, sin
keyring: solo ctypes (libreria estandar).
"""

import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, Optional

if getattr(sys, "frozen", False):
    # Dentro del .exe: la data vive al lado del ejecutable (portable).
    _BASE_DIR = Path(sys.executable).resolve().parent
else:
    _BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = _BASE_DIR / "data"
SESSION_FILE = DATA_DIR / "session.enc"
BOT_DATA_FILE = DATA_DIR / "settings.enc"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _write_blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data) if data else 1)
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _read_blob(blob: _DATA_BLOB) -> bytes:
    if not blob.pbData or blob.cbData == 0:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


CRYPTPROTECT_UI_FORBIDDEN = 0x01

_crypt32 = ctypes.WinDLL("crypt32")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),
    wintypes.LPCWSTR,
    ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_DATA_BLOB),
]
_crypt32.CryptProtectData.restype = wintypes.BOOL

_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),
    ctypes.POINTER(wintypes.LPCWSTR),
    ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_DATA_BLOB),
]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL

_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def _dpapi_protect(data: bytes) -> bytes:
    in_blob = _write_blob(data)
    out_blob = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _read_blob(out_blob)
    finally:
        if out_blob.pbData:
            _kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    in_blob = _write_blob(data)
    out_blob = _DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _read_blob(out_blob)
    finally:
        if out_blob.pbData:
            _kernel32.LocalFree(out_blob.pbData)


# None en runtime no-Windows (el launcher solo corre en Windows).
_DPAPI_OK = sys.platform == "win32"


class SessionManager:
    """Gestor de sesion unificado: data/session.enc cifrado con DPAPI."""

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def encrypt_session(self, data: Dict[str, Any]) -> bytes:
        if not _DPAPI_OK:
            raise RuntimeError("DPAPI solo disponible en Windows")
        json_data = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return _dpapi_protect(json_data)

    def decrypt_session(self, encrypted: bytes) -> Optional[Dict[str, Any]]:
        if not _DPAPI_OK:
            return None
        try:
            decrypted = _dpapi_unprotect(encrypted)
            return json.loads(decrypted.decode("utf-8"))
        except Exception:
            return None

    def save_session(self, data: Dict[str, Any]) -> bool:
        try:
            encrypted = self.encrypt_session(data)
            with open(SESSION_FILE, "wb") as f:
                f.write(encrypted)
            return True
        except Exception as e:
            print(f"Error guardando sesión: {e}")
            return False

    def load_session(self) -> Optional[Dict[str, Any]]:
        if not SESSION_FILE.exists():
            return None
        try:
            with open(SESSION_FILE, "rb") as f:
                encrypted = f.read()
            return self.decrypt_session(encrypted)
        except Exception:
            return None

    def clear_session(self) -> bool:
        try:
            if SESSION_FILE.exists():
                SESSION_FILE.unlink()
            return True
        except Exception as e:
            print(f"Error borrando sesión: {e}")
            return False

    def is_valid_session(self, session: Dict[str, Any]) -> bool:
        if not session:
            return False
        expires_at = session.get("expires_at")
        if expires_at and time.time() > expires_at:
            return False
        return True


# Funciones de conveniencia
def get_session_manager() -> SessionManager:
    """Retorna instancia singleton del SessionManager."""
    return SessionManager()


def save_session(data: Dict[str, Any]) -> bool:
    return get_session_manager().save_session(data)


def load_session() -> Optional[Dict[str, Any]]:
    return get_session_manager().load_session()


def clear_session() -> bool:
    return get_session_manager().clear_session()


def is_valid_session(session: Dict[str, Any]) -> bool:
    return get_session_manager().is_valid_session(session)


# ─── Datos del bot cifrados (data/settings.enc) ───────────────────
# Mismo DPAPI que session.enc, pero para el estado que gestiona el propio
# bot (hoy: ALLOWED_CHAT_ID). Antes ese campo se escribía en el .env en
# texto plano; aquí queda cifrado ligado al usuario/máquina de Windows.
def load_bot_data() -> Dict[str, Any]:
    """Carga el dict de datos del bot desde settings.enc ({} si falla/no existe)."""
    if not _DPAPI_OK:
        return {}
    if not BOT_DATA_FILE.exists():
        return {}
    try:
        with open(BOT_DATA_FILE, "rb") as f:
            encrypted = f.read()
        return json.loads(_dpapi_unprotect(encrypted).decode("utf-8"))
    except Exception:
        return {}


def save_bot_data(data: Dict[str, Any]) -> bool:
    """Persiste el dict de datos del bot cifrado con DPAPI."""
    if not _DPAPI_OK:
        return False
    try:
        BOT_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with open(BOT_DATA_FILE, "wb") as f:
            f.write(_dpapi_protect(payload))
        return True
    except Exception as e:
        print(f"Error guardando datos del bot: {e}")
        return False


def get_allowed_chat_id() -> Optional[int]:
    """Devuelve el chat permitido guardado encriptado, o None si no hay."""
    value = load_bot_data().get("allowed_chat_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def set_allowed_chat_id(chat_id: "int | str | None") -> bool:
    """Guarda encriptado el chat permitido (reemplaza al .env en texto plano)."""
    if chat_id is None:
        return False
    data = load_bot_data()
    data["allowed_chat_id"] = str(chat_id).strip()
    return save_bot_data(data)


def migrate_dotenv_allowed_chat() -> None:
    """Migra ALLOWED_CHAT_ID del .env legacy a settings.enc y limpia el .env.

    El bot antes escribía el ID del grupo permitido en texto plano (.env).
    Ahora vive cifrado con DPAPI; en el arranque se mueve el valor viejo y se
    deja de crear ese archivo. Si el .env queda sin claves restantes se
    elimina; si conserva TELEGRAM_TOKEN/OWNER_ID (flujo manual legacy) se
    mantiene solo ese contenido.
    """
    env_path = _BASE_DIR / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    rest: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == "ALLOWED_CHAT_ID" and value.strip():
            set_allowed_chat_id(value.strip())
            continue
        rest.append(line)
    if rest:
        try:
            env_path.write_text("\n".join(rest) + "\n", encoding="utf-8")
        except OSError:
            pass
    else:
        try:
            env_path.unlink()
        except OSError:
            pass