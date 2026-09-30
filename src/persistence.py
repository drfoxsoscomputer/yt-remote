"""Estado del bot, guardado en la base comun (data/ytremote.db).

QUE ES ESTE ARCHIVO HOY

Una FACHADA. Antes escribia un `state.json` COMPLETO en cada cambio: la cola,
el historial y los ajustes, todo reescrito entero. Lo unico que crecia era justo
lo que estaba en la peor herramienta.

Ahora delega en `store.Store` (esquema con tablas de verdad) y traduce al
diccionario que el bot ya conoce, para que `bot.py` no tenga que cambiar ni
saber donde se guarda nada. Tocar el volumen ya no reescribe la cola.

Lo que se conserva intacto es la API que el bot ya usa: `load()`, `save()`,
`mark_dirty()`, `flush()` y `current_state()`.

LECTURA SEGURA

Si la base es de una version mas nueva que este codigo, o no se pudo abrir,
queda en SOLO LECTURA: el bot arranca con valores por defecto pero NO escribe.
Tu lista, tu historial y tu volumen quedan intactos. Perder datos en silencio
es peor que no arrancar.

`path` se sigue aceptando porque las pruebas lo usan para aislar: lo que manda
es el DIRECTORIO del archivo, y la base vive ahi (`<dir>/ytremote.db`).
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
NOMBRE_BASE = "ytremote.db"

logger = logging.getLogger("bot")


def _para_guardar(items) -> list[dict]:
    """Normaliza una lista de QueueItem/dict a filas del catalogo.

    El `video_id` se deriva de la URL: es la clave del catalogo. Si la URL no
    esta, no hay fila: guardar una cancion sin identidad no serviria de nada.
    """
    salida: list[dict] = []
    for item in items or []:
        if hasattr(item, "to_dict"):
            item = item.to_dict()
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        if not url:
            continue
        fila = dict(item)
        fila["video_id"] = item.get("video_id") or url
        salida.append(fila)
    return salida


class StateStore:
    """Estado del bot sobre la base comun, con debouncing."""

    def __init__(self, path: Path | None = None) -> None:
        # `path` manda como "directorio de datos"; si no viene, el de por
        # defecto. Asi las pruebas siguen aislando con un STATE_PATH temporal.
        base = Path(path) if path else STATE_PATH
        self.path = base
        self._dirty = False
        self._flush_timer: TimerHandle | None = None
        self._loop: AbstractEventLoop | None = None
        self._current: dict | None = None
        self._solo_lectura = False
        self._motivo_solo_lectura = ""
        self._store = None
        self._conectar(base.parent / NOMBRE_BASE)

    # --- Conexion -------------------------------------------------------
    def _conectar(self, ruta: Path) -> None:
        try:
            from store import Store

            self._store = Store(ruta)
            if self._store.solo_lectura:
                self._solo_lectura = True
                self._motivo_solo_lectura = self._store.motivo_solo_lectura
                logger.error(
                    "NO se guarda el estado: %s. Tu lista, tu historial y tu "
                    "volumen quedan intactos.",
                    self._motivo_solo_lectura,
                )
        except Exception as exc:  # noqa: BLE001 - sin base, se arranca igual
            self._solo_lectura = True
            self._motivo_solo_lectura = f"no se pudo abrir la base ({exc})"
            logger.error(
                "NO se guarda el estado: %s. Los datos que hubiera siguen en "
                "%s.",
                self._motivo_solo_lectura,
                self.path.name,
            )

    # --- API publica ----------------------------------------------------
    def load(self) -> dict:
        """Trae el estado completo como el diccionario que el bot ya conoce."""
        defaults = self._defaults()
        if self._store is None or self._solo_lectura:
            self._current = defaults
            return self._current
        try:
            s = self._store
            self._current = {
                "version": CURRENT_VERSION,
                "volume": s.leer_ajuste("volume", 100),
                "paused": s.leer_ajuste("paused", False),
                "current": s.leer_ajuste("current"),
                "playlist": s.leer_cola(),
                "cursor": s.leer_ajuste("cursor", 0),
                "list_page": s.leer_ajuste("list_page", 0),
                "history": s.leer_historial(MAX_HISTORY),
                "radio_artist": s.leer_ajuste("radio_artist", ""),
                "max_height": s.leer_ajuste("max_height"),
                "card": s.leer_ajuste("card", {}) or {},
            }
        except Exception as exc:  # noqa: BLE001 - no debe tumbar el arranque
            logger.error("No se pudo leer el estado: %s. Arranca con defaults", exc)
            self._current = defaults
        return self._current

    def save(self, state: dict) -> None:
        """Guarda el estado por partes: solo se tocan las filas que cambian."""
        if self._solo_lectura or self._store is None:
            self._dirty = False
            return
        for clave in (
            "volume",
            "paused",
            "cursor",
            "list_page",
            "radio_artist",
            "max_height",
            "card",
            "current",
        ):
            if clave in state:
                try:
                    self._store.escribir_ajuste(clave, state[clave])
                except RuntimeError as exc:
                    self._solo_lectura = True
                    self._motivo_solo_lectura = str(exc)
                    logger.error("NO se guarda el estado: %s", exc)
                    self._dirty = False
                    return
        if "playlist" in state:
            try:
                self._store.guardar_cola(_para_guardar(state["playlist"]))
            except RuntimeError as exc:
                self._solo_lectura = True
                self._motivo_solo_lectura = str(exc)
                logger.error("NO se guarda el estado: %s", exc)
                self._dirty = False
                return
        if "history" in state:
            try:
                self._store.reemplazar_historial(
                    _para_guardar(state["history"]), MAX_HISTORY
                )
            except RuntimeError as exc:
                self._solo_lectura = True
                self._motivo_solo_lectura = str(exc)
                logger.error("NO se guarda el estado: %s", exc)
                self._dirty = False
                return
        self._dirty = False
        self._current = state

    def marcar_historial(self, item) -> None:
        """Suma UNA cancion al historial (con repeticiones, como antes).

        Antes el historial vivia dentro del JSON y se reescribia entero en cada
        cancion. Ahora es una fila.
        """
        if self._solo_lectura or self._store is None:
            return
        filas = _para_guardar([item])
        if not filas:
            return
        try:
            self._store.agregar_historial(filas[0])
            self._store.podar_historial(MAX_HISTORY)
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudo guardar el historial: %s", exc)

    def marcar_lista_vacia(self) -> None:
        """Vacia la cola (el usuario la limpio desde el boton 📋)."""
        if self._solo_lectura or self._store is None:
            return
        try:
            self._store.guardar_cola([])
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudo vaciar la cola: %s", exc)

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
            self._flush_timer = self._loop.call_later(DEBOUNCE_SECS, self._do_flush)

    def flush(self) -> None:
        """Fuerza el guardado inmediato de estado pendiente."""
        self._cancel_timer()
        if self._dirty and self._current is not None:
            self.save(self._current)

    def current_state(self) -> dict:
        """Devuelve el estado en memoria, o defaults si no se ha cargado."""
        if self._current is not None:
            return self._current
        return self._defaults()

    # --- Internals ------------------------------------------------------
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

    def _do_flush(self) -> None:
        self._flush_timer = None
        if self._dirty and self._current is not None:
            self.save(self._current)

    def _cancel_timer(self) -> None:
        if self._flush_timer is not None:
            try:
                self._flush_timer.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._flush_timer = None