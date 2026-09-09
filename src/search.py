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

# Tope maximo de resolucion (altura en pixeles) para los streams resueltos.
# El bot lo ajusta en caliente con set_max_height (persistido en state.json).
MAX_HEIGHT = 1080


def set_max_height(height: int) -> None:
    """Cambia el tope maximo de resolucion usado por resolve_stream_url.

    El formato de yt-dlp se arma en el momento de resolver, asi que basta
    actualizar este valor global para que los proximos streams respeten el
    nuevo limite (el cache de streams ya resueltos mantiene los anteriores).
    """
    global MAX_HEIGHT  # noqa: PLW0603
    MAX_HEIGHT = height


@dataclass
class SearchResult:
    """Un resultado de busqueda de YouTube."""

    url: str
    title: str
    duration: str
    thumbnail: str
    video_id: str = ""
    duration_seconds: int = 0
    channel: str = ""


def _build_thumbnail(video_id: str) -> str:
    """Construye la URL de la miniatura a partir del ID del video.

    Con extract_flat yt-dlp no devuelve las URLs de las miniaturas,
    pero la miniatura de YouTube siempre esta disponible en esta URL
    estable a partir del video ID.
    """
    if not video_id:
        return ""
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


def thumbnail_from_url(url: str) -> str:
    """Deriva la miniatura de YouTube desde un link directo (watch/shorts/youtu.be).

    Sirve para items sin thumbnail cargado (links directos de /play, /now);
    si no se puede extraer el ID devuelve "".
    """
    m = _VIDEO_ID_RE.search(url or "")
    if not m:
        return ""
    return _build_thumbnail(m.group(1))


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
                    duration_seconds=int(duration),
                    channel=entry.get("channel") or entry.get("uploader") or "",
                )
            )
    return results


def _fmt_duration(seconds: "int | float | None") -> str:
    """Formatea segundos a m:ss o h:mm:ss.

    Una duracion 0/desconocida (tipico de un video EN VIVO, que no tiene
    duracion fija) se muestra como '--:--' en vez de un engañoso '0:00'.
    """
    if not seconds or int(seconds) <= 0:
        return "--:--"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def is_playlist_url(url: str) -> bool:
    """Devuelve True si el link de YouTube apunta a una playlist o mix."""
    text = url.strip()
    if "/playlist?list=" in text:
        return True
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", text)
    if not m:
        return False
    list_id = m.group(1)
    # Un mix (radio) tiene list id tipo RD..., RDCLAK..., RDAMVM..., etc;
    # una playlist normal es un list id plano. En ambos casos hay que
    # expandir, salvo list=WL (watch later) de sesion que no aplica aqui.
    return list_id != "WL"


def expand_playlist(url: str, max_results: int = 50) -> list[SearchResult]:
    """Expande una playlist o mix de YouTube a sus tracks.

    Usa yt-dlp con extract_flat para no descargar nada: solo los metadatos
    (id, titulo, url, duracion) de cada track. Devuelve [] si falla o si la
    URL no es una playlist.
    """
    import yt_dlp

    if not is_playlist_url(url):
        return []

    ydl_opts: "dict[str, Any]" = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": False,
    }

    tracks: list[SearchResult] = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
        try:
            info = ydl.extract_info(url, download=False)
        except Exception:
            return tracks

        entries = info.get("entries") or []
        for entry in entries:
            if not entry or len(tracks) >= max_results:
                continue
            video_url = entry.get("url") or entry.get("webpage_url") or ""
            if not video_url:
                continue
            video_id = entry.get("id") or ""
            duration = entry.get("duration") or 0
            tracks.append(
                SearchResult(
                    url=video_url,
                    title=entry.get("title") or "(sin titulo)",
                    duration=_fmt_duration(duration),
                    thumbnail=_build_thumbnail(video_id),
                    video_id=video_id,
                    duration_seconds=int(duration),
                    channel=entry.get("channel") or entry.get("uploader") or "",
                )
            )
    return tracks


def quick_playlist(url: str, max_results: int = 15) -> tuple[list[SearchResult], int]:
    """Trae los PRIMEROS tracks de una playlist sin expandirla completa.

    Pensado para el arranque al toque: yt-dlp con playlistend trae solo los
    primeros N metadatos (una sola llamada, sin tocar YouTube salvo lo
    minimo) y playlist_count reporta el total real de la lista, asi el bot
    puede decir "Playlist (15/1882)". Devuelve (tracks, total); ante fallo
    devuelve ([], 0) igual que expand_playlist devuelve [].
    """
    import yt_dlp

    if not is_playlist_url(url):
        return [], 0

    ydl_opts: "dict[str, Any]" = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": False,
        "playlistend": max_results,
    }

    tracks: list[SearchResult] = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
        try:
            info = ydl.extract_info(url, download=False)
        except Exception:
            return tracks, 0

        entries = info.get("entries") or []
        for entry in entries:
            if not entry:
                continue
            video_url = entry.get("url") or entry.get("webpage_url") or ""
            if not video_url:
                continue
            video_id = entry.get("id") or ""
            duration = entry.get("duration") or 0
            tracks.append(
                SearchResult(
                    url=video_url,
                    title=entry.get("title") or "(sin titulo)",
                    duration=_fmt_duration(duration),
                    thumbnail=_build_thumbnail(video_id),
                    video_id=video_id,
                    duration_seconds=int(duration),
                    channel=entry.get("channel") or entry.get("uploader") or "",
                )
            )

    total = int(info.get("playlist_count") or 0)
    if total <= 0:
        total = len(tracks)
    return tracks, total


def resolve_stream_url(youtube_url: str) -> tuple[str, str | None] | None:
    """Resuelve un link de YouTube a una URL de stream directo que mpv puede reproducir.

    Usa el player client 'visionos' de yt-dlp: no le aplican el bloqueo de
    "Sign in to confirm you're not a bot" (a diferencia del client web) y
    expone los formatos de alta resolucion. Con 'android' el bot quedaba
    limitado a 360p.

    Si 'visionos' falla (hay redes/ISP que lo rechazan), se reintenta UNA vez
    con el client por defecto de yt-dlp. El ultimo error real queda expuesto
    en last_resolve_error para que el bot pueda diagnosticar por Telegram.

    La resolucion se limita al tope MAX_HEIGHT (configurable con
    set_max_height, por defecto 1080: 'bestvideo[height<=MAX]'+bestaudio);
    si el video no tiene esa resolucion se toma la mayor que no la supere.
    El fallback combinado ('best[height<=MAX]') tambien respeta el tope.

    VIDEOS EN VIVO (directo): se resuelven igual que el resto, con
    video+audio separados. Antes se forzaba el formato 'best' combinado,
    que en directos NO existe (YouTube live entrega el HLS como video y
    audio en playlists separadas), asi que esa re-resolucion fallaba y
    dejaba un error falso en last_resolve_error. El audio HLS del directo
    llega sin la key 'acodec' en yt-dlp y _stream_from_info lo detecta por
    el vcodec 'none'.

    Devuelve (video_url, audio_url) o (url, None) si es un stream combinado.
    Devuelve None si no se pudo resolver.
    """
    global _RESOLVE_LAST_ERROR  # noqa: PLW0603
    _RESOLVE_LAST_ERROR = ""

    # (nombre, opts extra). Primer intento: visionos. Fallback: client default.
    strategies: "list[tuple[str, dict]]" = [
        ("visionos", {"extractor_args": {"youtube": {"player_client": ["visionos"]}}}),
        ("default", {}),
    ]

    for client_name, extra in strategies:
        info = _extract_info(client_name, extra, youtube_url)
        if info is None:
            # _extract_info dejo el motivo real en _RESOLVE_LAST_ERROR.
            continue

        stream = _stream_from_info(info)
        if stream is not None:
            return stream
    return None


def _extract_info(client_name: str, extra: dict, youtube_url: str) -> "dict[str, Any] | None":
    """Extrae la metadata del video con una config de yt-dlp dada.

    Registra el motivo del fallo en _RESOLVE_LAST_ERROR (diagnostico).
    """
    global _RESOLVE_LAST_ERROR  # noqa: PLW0603
    import yt_dlp

    opts: "dict[str, Any]" = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": f"bestvideo[height<={MAX_HEIGHT}]+bestaudio/best[height<={MAX_HEIGHT}]",
    }
    opts.update(extra)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            info: "dict[str, Any]" = ydl.extract_info(youtube_url, download=False) or {}  # type: ignore[assignment]
        if not info:
            return None
        return info
    except Exception as exc:  # noqa: BLE001 - el motivo se reporta por Telegram
        _RESOLVE_LAST_ERROR = str(exc)
        return None


def _stream_from_info(info: "dict[str, Any]") -> tuple[str, str | None] | None:
    """Convierte la metadata extraida en (video_url, audio_url) o (url, None) combinado."""
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
            if vcodec not in (None, "none") and not video_url:
                video_url = str(furl)
            elif acodec not in (None, "none") and not audio_url:
                audio_url = str(furl)
            elif vcodec in (None, "none") and not audio_url:
                # HLS de directos de YouTube: el audio llega SIN la key
                # 'acodec' (solo trae vcodec 'none'), asi que no se lo
                # detectaba con el get(acodec, 'none') y quedaba mute.
                audio_url = str(furl)
        if video_url or audio_url:
            if video_url is None and audio_url is not None:
                # Solo hay audio (raro): se devuelve como stream único.
                return (audio_url, None)
            assert video_url is not None  # noqa: S101 - garantizado por el if
            return (video_url, audio_url)

    # Fallback: webpage URL (mpv intentara resolver con su ytdl hook)
    if info.get("webpage_url"):
        return (str(info["webpage_url"]), None)
    return None


# Ultimo error observado al resolver streams (diagnostico remoto por Telegram).
_RESOLVE_LAST_ERROR: str = ""


def last_resolve_error() -> str:
    """Devuelve el motivo del ultimo fallo de resolucion, o vacio si no hubo."""
    return _RESOLVE_LAST_ERROR
