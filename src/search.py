"""Busqueda en YouTube con yt-dlp.

- Detecta si un input es un link directo de YouTube.
- Busca videos por nombre y devuelve resultados con titulo, url,
  duracion y thumbnail.
"""

import re
from dataclasses import dataclass
from typing import Any

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/(watch|shorts|live|embed)?"
)
_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)/"
)


@dataclass
class SearchResult:
    """Un resultado de busqueda de YouTube."""

    url: str
    title: str
    duration: str
    thumbnail: str


def is_youtube_link(text: str) -> bool:
    """Devuelve True si el texto es un link directo de YouTube."""
    return bool(_LINK_RE.match(text.strip()))


def search(query: str, max_results: int = 5) -> list[SearchResult]:
    """Busca videos en YouTube y devuelve los resultados."""
    import yt_dlp

    ydl_opts: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": True,
    }

    results: list[SearchResult] = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
        except Exception:
            return results

        entries = info.get("entries") or []
        for entry in entries:
            if not entry:
                continue
            url = entry.get("url") or entry.get("webpage_url") or ""
            if not url:
                continue
            duration = entry.get("duration") or 0
            results.append(
                SearchResult(
                    url=url,
                    title=entry.get("title") or "(sin titulo)",
                    duration=_fmt_duration(duration),
                    thumbnail=entry.get("thumbnail") or "",
                )
            )
    return results


def _fmt_duration(seconds: int | float) -> str:
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
