"""Cola de reproduccion de YT-Remote.

Guarda una lista de items (URL + titulo) como PLAYLIST FIJA con un cursor:
los N items quedan siempre visibles (la lista no se encoge al reproducir) y
la reproduccion va avanzando el cursor, dando la vuelta (bucle) al llegar al
final. Un /play nuevo (playlist o cancion suelta) reemplaza la lista.

Tambien mantiene el modo radio: sin playlist, un item suelto es el "actual"
y la radio por semilla decide el siguiente (nunca repite URLs recientes).
"""

from collections import deque
from dataclasses import dataclass


@dataclass
class QueueItem:
    """Un video en la cola."""

    url: str
    title: str
    duration: str = ""
    duration_seconds: int = 0
    thumbnail: str = ""
    channel: str = ""
    artist: str = ""


class QueueManager:
    """Playlist fija con cursor (bucle) + modo radio con historico.

    - Con playlist: current = items[cursor]; next() avanza con wrap.
    - Sin playlist (radio): current es un item suelto y next() devuelve None
      (el bot pregunta a la radio por semilla cual sigue).
    - El historico (acotado) guarda URLs ya reproducidas para que la radio
      por semilla no rebote entre los mismos 2 temas.
    """

    # Cuantas URLs reproducidas recordamos (radio: no repetir recientes).
    _MAX_HISTORY = 20

    def __init__(self) -> None:
        self._items: list[QueueItem] = []
        self._cursor: int = 0
        self._current: QueueItem | None = None
        self._history: deque[str] = deque(maxlen=self._MAX_HISTORY)

    @property
    def has_playlist(self) -> bool:
        return bool(self._items)

    @property
    def current(self) -> QueueItem | None:
        if self._items:
            return self._items[self._cursor]
        return self._current

    def set_playlist(self, items: list[QueueItem]) -> None:
        """Reemplaza toda la reproduccion por una playlist fija."""
        self._items = list(items)
        self._cursor = 0
        self._current = None

    def add(self, item: QueueItem) -> None:
        """Agrega un item al final de la playlist activa."""
        if self._items:
            self._items.append(item)
        else:
            self._items = [item]

    def set_current(self, item: QueueItem | None) -> None:
        """Modo radio: marca el item suelto como actual (descarta la playlist)."""
        self._note_played(self._current)
        self._current = item
        self._items = []
        self._cursor = 0

    def is_recent(self, url: str) -> bool:
        """True si el url ya se reprodujo hace poco (radio no lo repite)."""
        return url in self._history

    def _note_played(self, item: QueueItem | None) -> None:
        if item is None:
            return
        if not self._history or self._history[-1] != item.url:
            self._history.append(item.url)

    def next(self) -> QueueItem | None:
        """Avanza al siguiente item.

        Con playlist: avanza el cursor con wrap (bucle al llegar al final).
        Sin playlist (radio): devuelve None (el bot decide por semilla).
        """
        if self._items:
            self._note_played(self._items[self._cursor])
            self._cursor = (self._cursor + 1) % len(self._items)
            return self._items[self._cursor]
        self._note_played(self._current)
        self._current = None
        return None

    def peek(self, index: int = 0) -> QueueItem | None:
        """El item que sigue al actual a partir de `index` saltos (con wrap)."""
        if not self._items:
            return None
        return self._items[(self._cursor + 1 + index) % len(self._items)]

    def jump_to(self, position: int = 1) -> QueueItem | None:
        """Salta al item de la posicion (1-based) SIN descartar la lista.

        La posicion 1 es el item actual. Devuelve None si esta fuera de rango.
        """
        if position < 1:
            return None
        if self._items:
            if position > len(self._items):
                return None
            if position == 1:
                return self._items[self._cursor]
            self._cursor = position - 1
            return self._items[self._cursor]
        if position == 1:
            return self._current
        return None

    def all(self) -> list[QueueItem]:
        """La playlist completa (siempre todos los items, sin consumir)."""
        return list(self._items)

    def clear(self) -> None:
        self._items = []
        self._cursor = 0
        self._current = None
        self._history.clear()