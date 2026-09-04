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
    video_id: str = ""


def _build_thumbnail(video_id: str) -> str:
    """Construye la URL de la miniatura a partir del ID del video.

    Con extract_flat yt-dlp no devuelve las URLs de las miniaturas,
    pero la miniatura de YouTube siempre esta disponible en esta URL
    estable a partir del video ID.
    """
    if not video_id:
        return ""
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def is_youtube_link(text: str) -> bool:
    """Devuelve True si el texto es un link directo de YouTube."""
    return bool(_LINK_RE.match(text.strip()))


def search(query: str, max_results: int = 5) -> list[SearchResult]:
    """Busca videos en YouTube y devuelve los resultados."""
    import yt_dlp

    ydl_opts: "dict[str, Any]" = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": True,
    }

    results: list[SearchResult] = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
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
            video_id = entry.get("id") or ""
            duration = entry.get("duration") or 0
            results.append(
                SearchResult(
                    url=url,
                    title=entry.get("title") or "(sin titulo)",
                    duration=_fmt_duration(duration),
                    thumbnail=_build_thumbnail(video_id),
                    video_id=video_id,
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


def resolve_stream_url(youtube_url: str) -> tuple[str, str | None] | None:
    """Resuelve un link de YouTube a URLs de stream que mpv puede reproducir.

    Devuelve (video_url, audio_url) o (url, None) si es un stream combinado.
    Devuelve None si no se pudo resolver (video no disponible, rate limit, etc).
    """
    import yt_dlp

    opts: "dict[str, Any]" = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            if not info:
                return None

            # Stream directo (un solo URL con audio+video)
            if info.get("url"):
                return (str(info["url"]), None)

            # Streams separados (DASH: video + audio)
            requested = info.get("requested_formats")
            if requested and isinstance(requested, list):
                video_url = None
                audio_url = None
                for fmt in requested:
                    vcodec = fmt.get("vcodec", "none")
                    acodec = fmt.get("acodec", "none")
                    furl = fmt.get("url")
                    if not furl:
                        continue
                    if vcodec != "none" and not video_url:
                        video_url = str(furl)
                    elif acodec != "none" and not audio_url:
                        audio_url = str(furl)
                if video_url:
                    return (video_url, audio_url)

            # Fallback: webpage URL (mpv intentara resolver con su ytdl hook)
            if info.get("webpage_url"):
                return (str(info["webpage_url"]), None)
    except Exception:
        pass
    return None
