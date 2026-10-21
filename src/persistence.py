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
from typing import Optional

from constants import MAX_HISTORY

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_PATH = DATA_DIR / "state.json"
CURRENT_VERSION = 1
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
            else:
                # Al abrir: depurar repetidas viejas del historial (los
                # datos de antes podian traer la misma cancion en varias
                # filas); queda la ultima reproduccion.
                try:
                    self._store.depurar_historial()
                except Exception as exc:  # noqa: BLE001 - no tumbar arranque
                    logger.warning("No se pudo depurar el historial: %s", exc)
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
                "posicion_guardada": s.leer_ajuste("posicion_guardada"),
                "radio_favs": s.leer_ajuste("radio_favs") or [],
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
            "posicion_guardada",
            "radio_favs",
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
        # OJO: el historial NO se escribe desde el estado. Es lo contrario:
        # la base es la fuente — cada cancion que suena llama a
        # marcar_historial() y el deque en memoria ya no manda (reescribir
        # la tabla entera en cada guardado BORRABA lo que sumaba
        # marcar_historial).
        self._dirty = False
        self._current = state

    def marcar_historial(self, item) -> None:
        """Suma UNA cancion al historial guardado.

        La base deja la posicion ESTABLE: si ya sonó, la fila no se mueve
        (la regla del usuario); solo una cancion nueva entra al tope. La
        base es incremental: cada cancion que suena llama aca."""
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

    def leer_historial(
        self, limite: int = MAX_HISTORY, directo: Optional[int] = None
    ) -> list[dict]:
        """El historial guardado, lo mas reciente primero (fuente del hub).
        Con `directo` trae SOLO emisoras (1) o SOLO canciones (0)."""
        if self._store is None or self._solo_lectura:
            return []
        try:
            return self._store.leer_historial(limite, directo=directo)
        except Exception as exc:  # noqa: BLE001 - no tumbar el render
            logger.error("No se pudo leer el historial: %s", exc)
            return []

    def borrar_historial(self, video_id: str) -> bool:
        """Quita UNA cancion del historial. False si no estaba o no se puede."""
        if self._solo_lectura or self._store is None or not video_id:
            return False
        try:
            return self._store.borrar_historial(video_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudo borrar del historial: %s", exc)
            return False

    def vaciar_historial(self, directo: Optional[int] = None) -> None:
        """Borra la lista del historial; con `directo`, SOLO ese tipo."""
        if self._solo_lectura or self._store is None:
            return
        try:
            self._store.vaciar_historial(directo=directo)
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudo vaciar el historial: %s", exc)

    def contar_historial(self) -> tuple[int, int]:
        """(emisoras, canciones) guardadas, para el selector del hub."""
        if self._store is None or self._solo_lectura:
            return (0, 0)
        try:
            return self._store.contar_historial()
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudo contar el historial: %s", exc)
            return (0, 0)

    def top_paises_uso(self, limite: int = 3) -> list[dict]:
        """Los paises MAS sonados de emisoras (chips del listado /radio)."""
        if self._store is None or self._solo_lectura:
            return []
        try:
            return self._store.top_paises_uso(limite)
        except Exception as exc:  # noqa: BLE001 - un conteo no tumba el render
            logger.error("No se pudo contar el uso por pais: %s", exc)
            return []

    def top_generos_uso(self, limite: int = 3) -> list[dict]:
        """Los tags MAS sonados de emisoras (chips de genero de /radio)."""
        if self._store is None or self._solo_lectura:
            return []
        try:
            return self._store.top_generos_uso(limite)
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudo contar el uso por genero: %s", exc)
            return []

    def actualizar_tags(self, video_id: str, tags: str) -> None:
        """Escribe SOLO la columna tags de una cancion existente (el
        backfill del rep de emisoras viejas); falla callada, como los conteos."""
        if self._store is None or self._solo_lectura:
            return
        try:
            self._store.actualizar_tags(video_id, tags)
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudo actualizar los tags de %s: %s", video_id, exc)

    def top_artistas_uso(
        self, limite: int = 6, directo: Optional[int] = 0
    ) -> list[dict]:
        """Los artistas MAS sonados de canciones (chips del home /buscar)."""
        if self._store is None or self._solo_lectura:
            return []
        try:
            return self._store.top_artistas_uso(limite, directo=directo)
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudo contar el uso por artista: %s", exc)
            return []

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