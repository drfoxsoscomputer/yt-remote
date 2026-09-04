"""Cola de reproduccion de YT-Remote.

Guarda una lista de items (URL + titulo) en orden FIFO. El siguiente
item de la cola se reproduce cuando termina el actual.
"""

from collections import deque
from dataclasses import dataclass


@dataclass
class QueueItem:
    """Un video en la cola."""

    url: str
    title: str
    duration: str = ""


class QueueManager:
    """Cola FIFO de videos a reproducir."""

    def __init__(self) -> None:
        self._queue: deque[QueueItem] = deque()
        self._current: QueueItem | None = None

    @property
    def current(self) -> QueueItem | None:
        return self._current

    @property
    def is_empty(self) -> bool:
        return len(self._queue) == 0

    def add(self, item: QueueItem) -> None:
        self._queue.append(item)

    def next(self) -> QueueItem | None:
        """Avanza al siguiente item o devuelve None si la cola esta vacia."""
        if self._queue:
            self._current = self._queue.popleft()
            return self._current
        self._current = None
        return None

    def peek(self, index: int = 0) -> QueueItem | None:
        if 0 <= index < len(self._queue):
            return self._queue[index]
        return None

    def all(self) -> list[QueueItem]:
        return list(self._queue)

    def clear(self) -> None:
        self._queue.clear()
        self._current = None
