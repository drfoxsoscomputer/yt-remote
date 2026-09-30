"""Persistencia del estado del bot en data/state.json.

Estado persistido:
- volume: nivel de volumen actual
- paused: True si esta en pausa al cerrarse
- current: QueueItem serializado de la cancion actual
- playlist: lista de QueueItem serializados de la cola fija
- cursor: posicion del tema actual dentro de la playlist
- list_page: pagina del listado 📋 en la que iba el usuario
- history: ultimas 100 canciones reproducidas
- radio_artist: semilla de la radio
- max_height: tope de resolucion elegido por el admin (None = 1080)

Escritura atomica con os.replace para no dejar archivos corruptos
si el bot se cierra a mitad de escritura.

Versionado:
- version 1: formato actual
- version > 1: se ignora y se usa defaults (futuro: migracion)
"""
import json
import logging
import os
import shutil
import time
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
        # Cuando el archivo NO se puede leer con seguridad (version de formato
        # mas nueva, o corrupto), el bot arranca con defaults pero se NIEGA a
        # sobrescribir el archivo.
        self._solo_lectura = False
        self._motivo_solo_lectura = ""

    # --- API publica ---

    def load(self) -> dict:
        """Carga el estado desde el archivo.

        Si el archivo no existe, es JSON invalido o su version de formato es
        mas nueva que la que este codigo entiende, arranca con defaults PERO se
        queda en solo lectura: jamas pisa un archivo que no sabe leer. Antes si
        lo pisaba, y eso hacia que la lista de canciones quedara perdida para
        siempre y en silencio, sin ningun aviso.
        """
        self._current = self._read_raw()
        return self._current

    def save(self, state: dict) -> None:
        """Escribe el estado de forma atomica en self.path.

        Creates parent directory if needed. Uses os.replace for atomicity:
        either the old file or the new one, never a partial write.

        Si el archivo existente es de una version mas nueva (o esta corrupto),
        NO escribe: esos datos son del usuario y este codigo no los entiende lo
        bastante como para reemplazarlos. Deja una copia del original al lado y
        lo avisa en el log.
        """
        if self._solo_lectura:
            logger.error(
                "NO se guarda el estado: %s. Se conserva el archivo tal cual "
                "(copia en %s). Arranca con valores por defecto.",
                self._motivo_solo_lectura,
                self._copia_de_seguridad(),
            )
            self._dirty = False
            return
        state.setdefault("version", CURRENT_VERSION)
        state.setdefault("volume", 100)
        state.setdefault("paused", False)
        state.setdefault("current", None)
        state.setdefault("playlist", [])
        state.setdefault("cursor", 0)
        state.setdefault("list_page", 0)
        state.setdefault("history", [])
        state.setdefault("radio_artist", "")
        state.setdefault("max_height", None)

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
            "cursor": 0,
            "list_page": 0,
            "history": [],
            "radio_artist": "",
            "max_height": None,
        }

    def _read_raw(self) -> dict:
        """Lee el archivo y valida version. Devuelve defaults si falla.

        PERO si el archivo existe y no se puede entender (corrupto, o de una
        version de formato mas nueva), deja el store en solo lectura y deja una
        copia. Antes devolvia defaults y el proximo guardado pisaba el archivo:
        la lista de canciones, el historial y el artista de la radio del
        usuario desaparecian sin que nadie se enterara. Perder datos en
        silencio es peor que no arrancar: por eso aqui se grita en el log y se
        conserva el original.
        """
        if not self.path.exists():
            logger.info("state.json no existe: se usaran defaults")
            return self._defaults()

        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.error(
                "state.json esta CORRUPTO (%s). Se arranca con defaults pero NO "
                "se va a sobrescribir: se conserva el archivo. Copia en %s",
                exc,
                self._copia_de_seguridad(),
            )
            self._solo_lectura = True
            self._motivo_solo_lectura = f"el archivo esta corrupto ({exc})"
            return self._defaults()

        if not isinstance(data, dict):
            logger.error(
                "state.json no es un objeto JSON: se conserva sin tocar. Copia en %s",
                self._copia_de_seguridad(),
            )
            self._solo_lectura = True
            self._motivo_solo_lectura = "el archivo no es un objeto JSON"
            return self._defaults()

        version = data.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool):
            # Una version que no es un numero no se puede comparar con nada.
            # Antes esto reventaba con TypeError al arrancar; ahora se trata
            # como lo que es: un archivo que este codigo no entiende.
            logger.error(
                "state.json tiene una version ilegible (%r). Se arranca con "
                "defaults pero NO se va a sobrescribir. Copia en %s",
                version,
                self._copia_de_seguridad(),
            )
            self._solo_lectura = True
            self._motivo_solo_lectura = (
                f"la version del archivo es ilegible ({version!r})"
            )
            return self._defaults()

        if version > CURRENT_VERSION:
            logger.error(
                "state.json es de la version %d y este bot solo entiende hasta "
                "la %d. Se arranca con defaults pero NO se va a sobrescribir: "
                "tu lista, tu historial y tu volumen quedan intactos. Copia "
                "en %s",
                version,
                CURRENT_VERSION,
                self._copia_de_seguridad(),
            )
            self._solo_lectura = True
            self._motivo_solo_lectura = (
                f"el archivo es de la version {version} y este bot entiende "
                f"hasta la {CURRENT_VERSION}"
            )
            return self._defaults()

        return data

    def _copia_de_seguridad(self) -> Path:
        """Copia el state.json actual al lado, con fecha, una sola vez.

        Se llama cuando el archivo no se pudo entender: el original se deja
        intacto y esta copia queda por si hay que revisarlo a mano.
        """
        if not self.path.exists():
            return self.path
        destino = self.path.with_name(
            f"{self.path.name}.intacto-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        try:
            shutil.copy2(self.path, destino)
            return destino
        except OSError as exc:
            logger.error("No se pudo hacer la copia de seguridad: %s", exc)
            return self.path

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
