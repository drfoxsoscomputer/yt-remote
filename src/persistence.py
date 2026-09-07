"""Persistencia del estado del bot en data/state.json.

Estado persistido:
- volume: nivel de volumen actual
- paused: True si esta en pausa al cerrarse
- current: QueueItem serializado de la cancion actual
- playlist: lista de QueueItem serializados de la cola fija
- history: ultimas 100 canciones reproducidas
- radio_artist: semilla de la radio

Escritura atomica con os.replace para no dejar archivos corruptos
si el bot se cierra a mitad de escritura.

Versionado:
- version 1: formato actual
- version > 1: se ignora y se usa defaults (futuro: migracion)
"""
import json
import logging
import os
from asyncio import AbstractEventLoop, TimerHandle, get_event_loop
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "state.json"
CURRENT_VERSION = 1
MAX_HISTORY = 100
DEBOUNCE_SECS = 0.5

logger = logging.getLogger("bot")


class StateStore:
    """Carga, persiste y administra el estado del bot con debouncing."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_PATH
        self._dirty = False
        self._flush_timer: TimerHandle | None = None
        self._loop = None
        self._current: dict | None = None
        self._loop: AbstractEventLoop | None = None

    # --- API publica ---

    def load(self) -> dict:
        """Carga el estado desde el archivo.

        Devuelve defaults si el archivo no existe, es JSON invalido o
        la version es mayor que la actual.
        """
        self._current = self._read_raw()
        return self._current

    def save(self, state: dict) -> None:
        """Escribe el estado de forma atomica en self.path.

        Creates parent directory if needed. Uses os.replace for atomicity:
        either the old file or the new one, never a partial write.
        """
        state.setdefault("version", CURRENT_VERSION)
        state.setdefault("volume", 100)
        state.setdefault("paused", False)
        state.setdefault("current", None)
        state.setdefault("playlist", [])
        state.setdefault("history", [])
        state.setdefault("radio_artist", "")

        if "history" in state and len(state["history"]) > MAX_HISTORY:
            state["history"] = state["history"][-MAX_HISTORY:]

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._dirty = False
        self._current = state

    def mark_dirty(self) -> None:
        """Marca el estado como modificado. Programa un flush con debouncing."""
        self._dirty = True
        if self._flush_timer is not None:
            return
        try:
            if self._loop is None:
                self._loop = get_event_loop()
        except RuntimeError:
            return
        if self._loop.is_running():
            self._flush_timer = self._loop.call_later(
                DEBOUNCE_SECS, self._do_flush
            )

    def flush(self) -> None:
        """Fuerza el guardado inmediato de estado pendiente."""
        self._cancel_timer()
        if self._dirty and self._current is not None:
            self.save(self._current)

    def current_state(self) -> dict:
        """Devuelve el estado en memoria, o defaults si no hay nada cargado."""
        if self._current is not None:
            return self._current
        return self._defaults()

    # --- Internals ---

    def _defaults(self) -> dict:
        return {
            "version": CURRENT_VERSION,
            "volume": 100,
            "paused": False,
            "current": None,
            "playlist": [],
            "history": [],
            "radio_artist": "",
        }

    def _read_raw(self) -> dict:
        """Lee el archivo y valida version. Devuelve defaults si falla."""
        if not self.path.exists():
            logger.info("state.json no existe: se usaran defaults")
            return self._defaults()

        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError, OSError):
            logger.warning("state.json corrupto: se usaran defaults")
            return self._defaults()

        version = data.get("version", 1)
        if version > CURRENT_VERSION:
            logger.warning(
                "state.json version %d es mayor que %d: se usaran defaults",
                version, CURRENT_VERSION,
            )
            return self._defaults()

        return data

    def _do_flush(self) -> None:
        self._flush_timer = None
        if self._dirty and self._current is not None:
            self.save(self._current)

    def _cancel_timer(self) -> None:
        if self._flush_timer is not None:
            try:
                self._flush_timer.cancel()
            except Exception:
                pass
            self._flush_timer = None
