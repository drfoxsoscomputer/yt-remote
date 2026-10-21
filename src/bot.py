"""Bot de Telegram de YT-Remote.

Controla un reproductor mpv local reproduciendo videos de YouTube.
Los comandos se enrutan por roles (admin > dj > user).
"""

from collections import deque

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Optional

from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import artist_match
import lyrics
import radio
from card import QUALITY_LEVELS, SLEEP_PRESETS, CardManager
from playback import PlaybackEngine
from config import Config
from player import Player
from persistence import StateStore
from queue_manager import QueueItem, QueueManager
from roles import RoleManager
from search import SearchResult, is_playlist_url, expand_playlist, quick_playlist, is_youtube_link, search, resolve_stream_url, thumbnail_from_url, last_resolve_error, last_search_error, set_max_height
from security import set_allowed_chat_id

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Timeout para cada resolucion de stream (yt-dlp en thread): si tarda mas,
# se corta y se trata como fallo de resolucion. Sin esto, un yt-dlp colgado
# congelaria el polling entero (los handlers de python-telegram-bot corren
# en secuencia) y el bot dejaria de responder botones y comandos.
_RESOLVE_TIMEOUT = 20.0

# Timeout para cada busqueda (yt-dlp en thread): si tarda mas, se corta y se
# avisa; sin esto, un yt-dlp colgado dejaria el "Buscando..." eterno.
_SEARCH_TIMEOUT = 30.0

# Arranque al toque de playlists: los primeros tracks se cargan al instante
# (una sola llamada rapida de yt-dlp con playlistend) y el RESTO se expande
# en segundo plano. El techo tecnico de seguridad: YouTube no permite
# playlists de mas de 5000 items, asi que no hay tope de usuario.
_QUICK_TRACKS = 15
_QUICK_TIMEOUT = 15.0
_MAX_PLAYLIST = 5000
_EXPAND_TIMEOUT = 300.0

# La playlist se carga de a VENTANAS, no entera: se mantienen estos temas
# cargados por delante de lo que esta sonando y se pide la tanda siguiente
# cuando la reproduccion se acerca al final de lo cargado. Con una lista de
# 1.882 temas, traerla entera era descargar 1.882 metadatos para mostrar 10
# por pagina.
_PLAYLIST_WINDOW = 30
# Cuantas vueltas de espera (1 s cada una) se le dan a una tanda antes de
# seguir con lo que hay. Es el tope de seguridad por si la carga se traba.
_PLAYLIST_ESPERAS = 15


# Mensaje de lista (boton 📋): temas visibles por pagina.
_LIST_PAGE_SIZE = 10

# Editor de usuarios (boton 👥): usuarios conocidos visibles por pagina.
_MEMBERS_PAGE_SIZE = 10

# Listado de resultados de /buscar: cuantos resultados se traen por tanda y
# cuantos se ven por pagina. El listado se pagina, asi que traer 10 de una no
# hace esperar mas: es la MISMA busqueda con mas resultados.
_SEARCH_BATCH = 10
_SEARCH_PAGE_SIZE = 5
# Cuando el usuario nombra un artista se piden el doble de resultados: el filtro
# se queda solo con los cuyo canal es el artista, y con 10 resultados puede no
# entrar ni uno. Es la unica forma de darle al filtro una oportunidad real sin
# inventar un orden.
_SEARCH_BATCH_CON_ARTISTA = _SEARCH_BATCH * 2
# Los limites viven en constants.py (10/11). Se reexportan con los mismos
# nombres para no cambiar las 40 referencias internas ni las pruebas.
from constants import (  # noqa: E402
    EXPAND_TIMEOUT,
    GITHUB_META_TIMEOUT,
    GITHUB_RELEASE_JSON,
    GITHUB_ZIP_TIMEOUT,
    HISTORY_PAGE_SIZE,
    LIST_PAGE_SIZE,
    MAX_PLAYLIST,
    MEMBERS_PAGE_SIZE,
    PLAYLIST_ESPERAS,
    PLAYLIST_WINDOW,
    PYPI_META_TIMEOUT,
    PYPI_WHEEL_TIMEOUT,
    PYPI_YTDLP_JSON,
    QUICK_TIMEOUT,
    QUICK_TRACKS,
    RADIO_BATCH,
    RADIO_DIRECTORIO_PAGINA,
    RADIO_PAGE_SIZE,
    RELEASE_ZIP_PATRON,
    RESOLVE_TIMEOUT,
    SEARCH_BATCH,
    SEARCH_BATCH_CON_ARTISTA,
    SEARCH_PAGE_SIZE,
    SEARCH_TIMEOUT,
    TOPE_LETRA,
    UPDATE_DIR,
    VERSION,
)

_RESOLVE_TIMEOUT = RESOLVE_TIMEOUT
_SEARCH_TIMEOUT = SEARCH_TIMEOUT
_QUICK_TRACKS = QUICK_TRACKS
_QUICK_TIMEOUT = QUICK_TIMEOUT
_MAX_PLAYLIST = MAX_PLAYLIST
_EXPAND_TIMEOUT = EXPAND_TIMEOUT
_PLAYLIST_WINDOW = PLAYLIST_WINDOW
_PLAYLIST_ESPERAS = PLAYLIST_ESPERAS
_LIST_PAGE_SIZE = LIST_PAGE_SIZE
_HISTORY_PAGE_SIZE = HISTORY_PAGE_SIZE
_MEMBERS_PAGE_SIZE = MEMBERS_PAGE_SIZE
_SEARCH_BATCH = SEARCH_BATCH
_SEARCH_PAGE_SIZE = SEARCH_PAGE_SIZE
_SEARCH_BATCH_CON_ARTISTA = SEARCH_BATCH_CON_ARTISTA
_RADIO_BATCH = RADIO_BATCH
_RADIO_PAGE_SIZE = RADIO_PAGE_SIZE
_RADIO_DIRECTORIO = RADIO_DIRECTORIO_PAGINA
_TOPE_LETRA = TOPE_LETRA

# Menú de comandos ("/") por audiencia. Los comandos de DJ (/buscar, /play,
# /radio, /letra) NO van en el menú abierto: se aplican con el scope
# BotCommandScopeChatMember (visible SOLO para ese miembro del grupo), uno
# por dj/admin, al arrancar y tras cada cambio de rol (ver
# _sincronizar_menu_dj). Telegram no tiene scope por rol del bot — el dj es
# un rol nuestro, no un admin de Telegram — pero el ChatMember scope es por
# usuario, que es justo lo que hacía falta. El default queda con /start
# (un invitado no ve atajos que no puede correr).
# La cobertura la garantiza test_menu_cubre_todos_los_comandos.
_COMANDOS_PUBLICOS = [
    BotCommand("start", "Info del bot (dj+)"),
]
_COMANDOS_DJ = [
    BotCommand("start", "Info del bot (dj+)"),
    BotCommand("buscar", "Buscar artista o link para reproducir"),
    BotCommand("play", "Buscar y reproducir (alias de /buscar)"),
    BotCommand("radio", "Elegir emisora o favoritas"),
    BotCommand("letra", "Letra de lo que esta sonando"),
]
_COMANDOS_ADMIN = [
    BotCommand("reglas", "Re-envia y fija (pin) las reglas del grupo"),
    BotCommand("salud", "Version y estado del bot"),
    BotCommand("actualizar", "Buscar y aplicar actualizaciones del bot"),
]


class ActualizacionError(Exception):
    """Motivo honesto por el que /actualizar no pudo renovar yt-dlp."""


def _version_num(version: str) -> tuple[int, ...]:
    """2026.08.19 y 2026.8.19 son la MISMA version: la comparacion es por
    numeros, no por texto (PyPI publica sin el cero a la izquierda)."""
    partes: list[int] = []
    for pedazo in version.split("."):
        m = re.match(r"\d+", pedazo)
        if m is None:
            break
        partes.append(int(m.group()))
    return tuple(partes) or (0,)


def _cancion_y_artista(titulo: str) -> tuple[str, str]:
    """Titulo de YouTube -> (cancion, artista) para preguntarle a lrclib.

    Los titulos de YouTube vienen casi siempre como "Artista - Cancion
    (Video Oficial)": si hay " - ", lo de antes es el artista y lo de
    despues la cancion (el parentesis de ruido lo aguanta la busqueda
    difusa de lrclib). Sin " - " no se adivina: la cancion es el titulo
    entero y el artista queda vacio (se busca por texto libre).
    """
    if " - " in titulo:
        izq, _, der = titulo.partition(" - ")
        artista, cancion = izq.strip(), der.strip()
        if artista and cancion:
            return cancion, artista
    return titulo.strip(), ""


def _verificar_sha256(datos: bytes, esperado: str) -> None:
    """El wheel recien bajado tiene que ser EXACTAMENTE el archivo publicado."""
    if hashlib.sha256(datos).hexdigest() != esperado.lower():
        raise ActualizacionError("el archivo bajado no coincide con el sha256 publicado")


def _leer_version_paquete(destino: Path) -> str | None:
    """__version__ del version.py del paquete EN DISCO.

    Es la verdad post-instalacion: el yt_dlp ya importado en memoria sigue
    siendo el viejo hasta que el bot se reinicie.
    """
    try:
        texto = (destino / "version.py").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', texto)
    return m.group(1) if m else None


def _instalar_wheel_ytdlp(wheel: bytes, destino: Path) -> None:
    """Reemplaza la carpeta yt_dlp/ instalada (`destino`) por la del wheel,
    con rollback: si algo falla a mitad, la carpeta vieja vuelve a su lugar.

    Solo se cambia EL PAQUETE (lo que importa search.py). El dist-info se
    ignora a proposito: el portable no lleva pip y la version de verdad
    vive en yt_dlp/version.py. La temporal se crea DENTRO del mismo
    site-packages para que el swap sea un rename del mismo disco.
    """
    import io
    import shutil
    import tempfile
    import zipfile

    destino = destino.resolve()
    if destino.name != "yt_dlp":
        raise ActualizacionError(f"destino inesperado: {destino}")
    raiz = destino.parent
    with tempfile.TemporaryDirectory(prefix="ytdlp-", dir=str(raiz)) as tmp:
        try:
            with zipfile.ZipFile(io.BytesIO(wheel)) as zf:
                zf.extractall(tmp)
        except zipfile.BadZipFile as exc:
            raise ActualizacionError(
                f"el archivo descargado no es un wheel valido ({exc})"
            ) from exc
        nuevo = Path(tmp) / "yt_dlp"
        if not (nuevo / "__init__.py").is_file() or not (nuevo / "version.py").is_file():
            raise ActualizacionError("el archivo descargado no trae la pieza esperada")
        viejo = raiz / "yt_dlp.viejo"
        shutil.rmtree(viejo, ignore_errors=True)
        if destino.exists():
            destino.rename(viejo)
        try:
            nuevo.rename(destino)
        except OSError:
            if viejo.exists() and not destino.exists():
                viejo.rename(destino)
            raise
        shutil.rmtree(viejo, ignore_errors=True)


async def _pedir_meta_ytdlp() -> tuple[str, str, str]:
    """(version, url_del_wheel, sha256) de la ultima version en PyPI."""
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=PYPI_META_TIMEOUT, follow_redirects=True
        ) as cli:
            r = await cli.get(PYPI_YTDLP_JSON)
            r.raise_for_status()
            datos = r.json()
    except Exception as exc:  # noqa: BLE001 - el motivo honesto va al admin
        raise ActualizacionError(f"PyPI no responde ({exc})") from exc
    version = (datos.get("info") or {}).get("version")
    wheel = next(
        (
            u
            for u in datos.get("urls", [])
            if str(u.get("filename", "")).endswith("-py3-none-any.whl")
        ),
        None,
    )
    if not version or wheel is None:
        raise ActualizacionError("PyPI no devolvio version ni archivo instalable")
    return str(version), wheel["url"], wheel["digests"]["sha256"]


async def _bajar_wheel_ytdlp(url: str, sha256: str) -> bytes:
    """Baja el wheel y exige que su sha256 sea el publicado."""
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=PYPI_WHEEL_TIMEOUT, follow_redirects=True
        ) as cli:
            r = await cli.get(url)
            r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise ActualizacionError(f"no se pudo bajar el archivo nuevo ({exc})") from exc
    datos = r.content
    _verificar_sha256(datos, sha256)
    return datos


# Carpeta de componentes internos DENTRO del ZIP portable: el prefijo que
# /actualizar salta cuando lo instalado ya es tan nuevo como el release.
_RUTA_COMPONENTES_EN_ZIP = "runtime/python/Lib/site-packages/yt_dlp"


def _base_instalacion() -> Path:
    """Carpeta de la instalacion: la RAIZ portable, padre de src\\.

    En el deploy es dist\\ytremote (donde estan el exe y runtime\\); en
    desarrollo es la raiz del repo. De ahi se derivan el disco del runtime
    y donde queda el ZIP de la actualizacion.
    """
    return Path(__file__).resolve().parent.parent


def _ruta_zip_instalacion(version_rel: str) -> Path:
    """Donde queda el ZIP del release: data\\update\\ (misma letra que la
    instalacion y FUERA de todo lo que se reemplaza)."""
    return _base_instalacion() / "data" / UPDATE_DIR / f"yt-remote-{version_rel}-portable.zip"


def _version_interna_instalada() -> str | None:
    """Version de los componentes internos instalados, o None si el paquete
    no existe/esta roto (se critica en el flujo, no aca)."""
    try:
        import yt_dlp.version

        return yt_dlp.version.__version__
    except Exception:  # noqa: BLE001 - sin pieza no hay version que comparar
        return None


def _leer_interna_del_zip(ruta_zip: Path, prefijo: str) -> str | None:
    """Version de los componentes internos que TRAE el release.

    Sirve para no retrocederlos con el reemplazo: None = el ZIP no la
    expone y el flujo decide sin ella (cmd_actualizar manda
    `interna_puesta` para ese caso).
    """
    import re
    import zipfile

    miembro = prefijo + _RUTA_COMPONENTES_EN_ZIP + "/version.py"
    try:
        with zipfile.ZipFile(ruta_zip) as zf:
            with zf.open(miembro) as f:
                texto = f.read().decode("utf-8", errors="replace")
    except (OSError, KeyError):
        return None
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', texto)
    return m.group(1) if m else None


def _validar_zip_paquete(ruta_zip: Path) -> str:
    """Abre el ZIP y verifica que SEA el paquete de YT-Remote de verdad
    (ytremote.exe, runtime\\python\\python.exe y src\\main.py). Devuelve el
    prefijo de carpeta contenedora cuando el release empaqueta todo dentro
    de una ("yt-remote-1.2.0/..."); "" si va sin ella. Un ZIP que no sea
    el paquete NUNCA llega al ayudante: se extraeria basura sobre la
    instalacion viva."""
    import zipfile

    import updater

    try:
        with zipfile.ZipFile(ruta_zip) as zf:
            nombres = [n.replace("\\", "/") for n in zf.namelist()]
    except zipfile.BadZipFile as exc:
        raise ActualizacionError(
            f"el archivo bajado no es un ZIP valido ({exc})"
        ) from exc
    prefijo = updater.detectar_prefijo(nombres)
    if not updater.es_paquete(nombres, prefijo):
        raise ActualizacionError(
            "el ZIP no es el paquete de YT-Remote (le faltan piezas clave)"
        )
    return prefijo


async def _pedir_meta_release() -> tuple[str, str, int, str | None]:
    """(version, url_del_zip, bytes_del_zip, sha256 publicado) del ULTIMO
    release estable en GitHub.

    La version sale del tag (v1.2.0 -> 1.2.0). El sha256 es el `digest`
    del asset cuando GitHub lo publica; si no viene, la defensa es el
    tamanio publicado + validar que el ZIP sea el paquete (nunca se
    descomprime algo desconocido sobre la instalacion).
    """
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=GITHUB_META_TIMEOUT, follow_redirects=True
        ) as cli:
            r = await cli.get(
                GITHUB_RELEASE_JSON,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "yt-remote-updater",
                },
            )
            r.raise_for_status()
            datos = r.json()
    except Exception as exc:  # noqa: BLE001 - el motivo honesto va al admin
        raise ActualizacionError(f"GitHub no respondio ({exc})") from exc
    return _parsear_release_json(datos)


def _parsear_release_json(datos: object) -> tuple[str, str, int, str | None]:
    """Del JSON de releases/latest: (version, url, bytes, sha256).

    Funcion PURA, probada sin red. Rechaza releases sin tag legible, sin
    assets, sin ZIP portable o con asset sin URL/tamanio: cada caso con
    su motivo honesto para el admin.
    """
    import fnmatch

    if not isinstance(datos, dict):
        raise ActualizacionError("GitHub devolvio un release con formato raro")
    tag = str(datos.get("tag_name") or "").strip()
    version = tag.lstrip("vV")
    if not version or _version_num(version) == (0,):
        raise ActualizacionError(f"el tag {tag!r} no trae una version legible")
    assets = datos.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ActualizacionError(f"el release {version} no publico archivos")
    asset = None
    for pieza in assets:
        if not isinstance(pieza, dict):
            continue
        if fnmatch.fnmatch(str(pieza.get("name", "")), RELEASE_ZIP_PATRON):
            asset = pieza
            break
    if asset is None:
        raise ActualizacionError(f"el release {version} no publico el ZIP portable")
    url = str(asset.get("browser_download_url") or "")
    size = int(asset.get("size") or 0)
    if not url or size <= 0:
        raise ActualizacionError(
            "el ZIP del release esta incompleto (sin URL o sin tamanio)"
        )
    sha: str | None = None
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        sha = digest.split(":", 1)[1].strip().lower()
    return version, url, size, sha


def _verificar_sha256_archivo(ruta: Path, esperado: str) -> None:
    """El ZIP recien bajado tiene que ser EXACTAMENTE el asset publicado."""
    import hashlib

    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    if h.hexdigest() != esperado.lower():
        raise ActualizacionError(
            "el archivo bajado no coincide con el sha256 publicado"
        )


async def _bajar_zip_release(
    url: str, size: int, sha256: str | None, ruta_destino: Path
) -> tuple[Path, str]:
    """Baja el ZIP del release al destino y lo valida TODO ahi antes de
    confirmarlo: tamanio publicado, sha256 si GitHub lo expone y
    estructura real del paquete. Va a un .parcial y se confirma con
    rename: nunca queda un ZIP a medias con nombre de verdad.
    """
    import httpx

    ruta_destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = ruta_destino.with_suffix(ruta_destino.suffix + ".parcial")
    try:
        async with httpx.AsyncClient(
            timeout=GITHUB_ZIP_TIMEOUT, follow_redirects=True
        ) as cli:
            async with cli.stream("GET", url) as r:
                r.raise_for_status()
                with parcial.open("wb") as f:
                    async for trozo in r.aiter_bytes():
                        f.write(trozo)
    except Exception as exc:  # noqa: BLE001
        raise ActualizacionError(f"no se pudo bajar el archivo ({exc})") from exc
    bajado = parcial.stat().st_size
    if size > 0 and bajado != size:
        raise ActualizacionError(
            f"el archivo bajo incompleto ({bajado} de {size} bytes)"
        )
    if sha256:
        _verificar_sha256_archivo(parcial, sha256)
    prefijo = _validar_zip_paquete(parcial)
    parcial.replace(ruta_destino)
    return ruta_destino, prefijo


def _radio_parsear(texto: str) -> tuple[str, str]:
    """(vista, termino) de los argumentos de /radio.

    "genero:"/"género:"/"g:" buscan por genero (tag del API); "pais:"/
    "país:"/"p:" por pais; "favoritas"/"fav"/"❤️" abren la lista propia; y
    cualquier otra cosa es una busqueda por nombre. Sin argumentos:
    destacadas.
    """
    t = texto.strip()
    if not t:
        return "top", ""
    if t.lower() in ("favoritas", "favorita", "fav", "❤", "❤️"):
        return "fav", ""
    m = re.match(r"^(?:genero|género|g)\s*[:=]\s*(.+)$", t, re.IGNORECASE)
    if m:
        return "tag", m.group(1).strip()
    m = re.match(r"^(?:pais|país|p)\s*[:=]\s*(.+)$", t, re.IGNORECASE)
    if m:
        return "pais", m.group(1).strip()
    return "name", t


class YTRemoteBot:
    """Ensambla bot, player, cola y roles."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.roles = RoleManager()
        self.player = Player(config.mpv_path)
        self.player._on_track_ended = self._mpv_track_ended_callback
        # El motor de reproduccion (playback.PlaybackEngine) es dueno del cache
        # de streams, del prefetch y de la bandera de fin de cancion. Se crea
        # PRIMERO: los atributos de mas abajo pasan por las propiedades que
        # delegan en el.
        self.playback = PlaybackEngine(self)
        self.queue = QueueManager()
        # Ancla de la radio: el ARTISTA fijado UNA sola vez, en el /buscar
        # (todo lo que va antes del guion). /next y el auto-advance lo reusan
        # tal cual, sin re-derivar en cada salto.
        self._radio_artist: str = ""
        # Artista del LISTADO de busqueda que esta en pantalla. Es a donde
        # va el ancla de la radio SOLO si el usuario elige un resultado y
        # arranca; buscar, buscar mal o cancelar no cambia lo que suena.
        self._search_artist: str = ""
        # Vigilante de conexion: True mientras el polling esta caido por red
        # (sin internet / Telegram inalcanzable). Al volver, se descarta el
        # backlog acumulado y se avisa al usuario que esos comandos se perdieron.
        self._net_offline: bool = False
        # Tarea que deja de pedir updates en cuanto se cae la red. Existe para
        # que el vigilante sea el UNICO lector de la tanda que se acumulo.
        self._net_stop_task: "asyncio.Task | None" = None
        # cache: callback_data -> SearchResult para los botones de busqueda
        self._search_cache: dict[str, SearchResult] = {}
        # True mientras el listado de resultados de /buscar sigue en pantalla.
        # En ese lapso la card NO se reposiciona: queda arriba del listado.
        # Se limpia al elegir, al cancelar o ante un /buscar o /play nuevo.
        self._search_list_pending: bool = False
        # Listado de resultados de /buscar: se acumulan entre tandas para poder
        # paginar sin pegarle a YouTube en cada pagina, con la consulta, la
        # pagina a la vista y la identidad del mensaje del listado.
        self._search_resultados: list[SearchResult] = []
        self._search_consulta: str = ""
        self._search_pagina: int = 0
        self._search_nota: str = ""
        self._search_chat_id: int | None = None
        self._search_message_id: int | None = None
        self._search_sin_mas: bool = False
        # Espejo del flag de /buscar para el listado de /radio: mientras el
        # listado de emisoras sigue en pantalla la card tampoco se mueve
        # (misma norma: la card se mueve solo cuando el estado de
        # reproduccion cambia). Se apaga al cerrar el listado (elegir, X,
        # close del mesonero o del bot).
        self._radio_list_pending: bool = False
        # Ancla de rotacion de emisoras: el listado de la vista de /radio
        # desde la que SAELIO la emisora. ⏭/⏮ y el auto-avance rotan ahi
        # (siguiente/anterior emisora, wrap) en vez de caer en la radio de
        # YouTube por artista. Se re-coloca en cada rad:play y se aborta
        # cuando suena algo que no es emisora (una cancion de /buscar).
        self._radio_ancla: "list | None" = None
        # True la UNA vez que la rotacion da la vuelta (p. ej. del final
        # de la lista vuelve a la primera): un solo aviso "empezo de nuevo".
        self._radio_ancla_wrap: bool = False
        # Ancla del historial: la fila que el dj elegio del hub 🕘 (emisora
        # o cancion). Define DONDE entra la busqueda cuando el caminar por
        # el historial se acaba: ⏭ en la primera posicion (la mas nueva)
        # busca con esa fila como semilla (emisora: por su pais; cancion:
        # por su artista). Repetir del historial la RE-COLOCA; sonar una
        # cancion de otro sitio (cola, /buscar, link) la mata, igual que
        # la regla de siempre: el ancla vive entre picks.
        self._hist_ancla: dict | None = None
        # Candado anti-colision de botones: una sola accion de control a la
        # vez. Si ya hay una en curso (p.ej. resolviendo el stream de un
        # salto), los toques extra se descartan al instante en vez de
        # acumularse en la cola del bot (eran los saltos encolados lentos).
        self._control_lock = asyncio.Lock()
        # Cache de la lista de radio: la busqueda de 50 temas se hace UNA vez
        # por ancla de sesion (lo que escribió el usuario en /buscar) y cada
        # /next elige de la lista cacheada en vez de volver a golpear yt-dlp
        # (era el delay real de los botones ⏭/⏮: ~2s de busqueda por salto).
        self._radio_search_cache: dict[str, list] = {}
        self._anticipate_queue: asyncio.Queue[str] | None = None
        self._anticipate_task: asyncio.Task | None = None
        # Mini reproductor persistente: un solo mensaje editable con botones.
        # El bot rastrea pausa y volumen porque el IPC de mpv no devuelve
        # respuestas a comandos (la respuesta se pierde en el handle efimero).
        # La tarjeta es una pieza aparte (card.CardManager) con su propia
        # identidad y su propio ciclo de vida. Las tres propiedades de abajo
        # delegan su estado: no puede haber dos verdades.
        self.card = CardManager(self)

        # Tope de resolucion elegido por el admin (None = 1080 por defecto).
        # Se aplica a search.MAX_HEIGHT y se persiste en state.json.
        self._max_height: int | None = None
        # Mensaje de la lista (boton 📋): referencias efimeras (no se
        # persisten, la lista muere al reiniciar) + pagina visible de 10.
        self._list_chat_id: int | None = None
        self._list_message_id: int | None = None
        self._list_page: int = 0
        # Determina si el mensaje de la lista abierto muestra botones de tema
        # (True si lo abrio un admin/dj). Fijo durante la vida del mensaje:
        # al paginar no cambia aunque lo pages un user.
        self._list_can_select: bool = False
        # Vista del mensaje-lista: "cola" (por defecto) o "historial". El 📋
        # es un hub de contexto: la cola gana un boton 🕘 que convierte el
        # MISMO mensaje en el historial, y 🔙 Cola vuelve. Ephemero como el
        # mensaje mismo: un reinicio reabre en la cola.
        self._list_view: str = "cola"
        self._history_page: int = 0
        self._historial_borrar_armado: bool = False
        # Apagado programado (opcion 💤 de Ajustes): minutos restantes y tarea asyncio que
        # lo dispara. Vive SOLO en la sesion: un reinicio lo borra.
        self._sleep_minutes: int | None = None
        self._sleep_task: asyncio.Task | None = None
        # Hora limite del apagado programado (monotonic): de aca sale el
        # "Se apagara en N min" con los minutos que FALTAN de verdad.
        self._sleep_deadline: float | None = None
        # Instante guardado del tema actual: {"item": item.to_dict(),
        # "pos": segundos}. Lo escribe SOLO el temporizador antes de cerrar
        # todo; el ▶ lo consume al retomar (el ⏹ lo borra: parar = proximo
        # ▶ desde 0). Se persiste con el estado comun.
        self._posicion_guardada: dict | None = None
        # /radio: la vista abierta (home/paises/generos/buscada/favoritas)
        # vive SOLO en la sesion (un reinicio reabre el home); las FAVORITAS
        # si se persisten con el estado comun (clave radio_favs).
        self._radio_view: str = "home"
        self._radio_page: int = 0
        self._radio_query: str = ""
        self._radio_items: list = []
        self._radio_favs: list = []
        self._radio_chat_id: int | None = None
        self._radio_message_id: int | None = None
        # Directorios del menu de /radio: listas COMPLETAS (no solo la
        # pagina) para que rad:pais:N / rad:gen:N sepa a que fila toco.
        self._radio_paises: list = []  # [(nombre_api, iso, emisoras)]
        self._radio_generos: list = []  # [(tag, emisoras)]
        # El LUGAR del salon: la zona horaria de la PC (la maquina donde
        # corre el bot), leida UNA vez al arrancar (stdlib, sin preguntar).
        self._radio_equipo = radio.pais_del_equipo()
        # Home de /buscar (chips de artistas ya sonados, callback bsm:*):
        # UN solo mensaje en pantalla, igual que los demas listados.
        self._buscar_home_chat_id: int | None = None
        self._buscar_home_message_id: int | None = None
        # Menu "/" de dj ya aplicado (uids con scope ChatMember): sirve para
        # BORRAR el menu de quien pierde el rol, no solo acumular scopes.
        self._comandos_dj_aplicados: set[int] = set()
        # Editor de usuarios (boton 👥): la lista vive en el chat PRIVADO del
        # admin. Los cambios quedan "staged" (sin persistir en roles.json)
        # hasta el boton ❌ (commit); tocar un usuario dos veces lo revierte.
        self._members_chat_id: int | None = None
        self._members_message_id: int | None = None
        self._members_user_id: int | None = None
        self._members_page: int = 0
        self._members_staged: dict[int, str] = {}
        # True mientras la expansion de fondo de una playlist esta activa y
        # False apenas termina. Sirve para no cerrar el listado con wrap
        # prematuro mientras la cola sigue creciendo. Tambien sujeta el total
        # real de la lista (playlist_count) para el feedback de la card.
        self._expanding_playlist: bool = False
        self._expanding_playlist_url: str | None = None
        self._expanding_total: int = 0
        self._expand_task: asyncio.Task | None = None
        self._paused: bool = False
        self._volume: int = 100
        # True mientras se ejecuta una accion disparada por un boton de la
        # tarjeta: los reply/errores van como toast (answer) y no se envian
        # fotos de confirmacion, porque la tarjeta se re-renderiza al final.
        self._from_card: bool = False
        # Pila de navegación (estándar en reproductores): _nav_back guarda
        # los temas que dejaron de sonar (para /prev), _nav_forward guarda los
        # temas que se saltaron con /next (para /next tras un /prev). Se
        # actualizan en _play_item y se limpian al cambiar de fuente.
        self._nav_back: deque[QueueItem] = deque(maxlen=100)
        self._nav_forward: deque[QueueItem] = deque(maxlen=100)
        # Persistencia: volumen, pausa, cola, historial y radio artist
        # sobreviven a reinicios. Por defecto arranca en pausa si habia
        # algo sonando (el usuario decide reanudar con /play).
        self._state = StateStore()
        # True si se restauro estado previo (hay una cancion que retomar),
        # para el texto "retoma" en la tarjeta.
        self._restored: bool = False
        self._load_persisted_state()
        self._register_owner()

    def _persist_dirty(self) -> None:
        """Construye el estado actual y lo marca como dirty para guardado."""
        self._state._current = {
            "version": 1,
            "volume": self._volume,
            "paused": self._paused,
            "current": self.queue.current.to_dict() if self.queue.current else None,
            "playlist": [i.to_dict() for i in self.queue._items],
            "cursor": self.queue._cursor if self.queue.has_playlist else 0,
            "list_page": self._list_page,
            "radio_artist": self._radio_artist,
            "max_height": self._max_height,
            "card": {
                "chat_id": self._card_chat_id,
                "message_id": self._card_message_id,
                "is_photo": self._card_is_photo,
            },
            "posicion_guardada": self._posicion_guardada,
            "radio_favs": [f.to_dict() for f in self._radio_favs],
        }
        self._state.mark_dirty()

    def _registrar_historial(self, item) -> None:
        """Una cancion que SONO queda en el historial guardado.

        La base guarda UNA sola fila por cancion: si ya sonó, la reubica
        arriba (queda la ultima reproduccion). Si la base esta en solo
        lectura, no escribe y la reproduccion sigue igual: el historial
        nunca tumba un play.
        """
        self._state.marcar_historial(item)

    def _persist_flush(self) -> None:
        """Guardado inmediato: llama save() sin esperar el debounce."""
        self._persist_dirty()
        self._state.flush()

    def _load_persisted_state(self) -> None:
        """Carga el estado persistente y aplica valores al bot.

        Si no hay archivo o esta corrupto, usa defaults:
        - volumen 100, sin pausa, sin cola.
        Al arrancar, la cancion queda en pausa para que el usuario confirme.
        """
        loaded = self._state.load()
        self._volume = loaded.get("volume", 100)
        self._radio_artist = loaded.get("radio_artist", "")
        self._posicion_guardada = loaded.get("posicion_guardada") or None
        try:
            self._radio_favs = [
                radio.Emisora.desde_dict(d) for d in loaded.get("radio_favs") or []
            ]
        except Exception:  # noqa: BLE001 - un state corrupto no tumba el arranque
            self._radio_favs = []
        has_current = bool(loaded.get("current"))
        self._restored = has_current
        # Si se restauro una cancion, arranca en PAUSA aunque el estado
        # guardado dijera "sonando": el autoadvance no corre y el usuario
        # decide reanudar con /play (o el boton ▶ de la tarjeta).
        self._paused = True if has_current else loaded.get("paused", False)
        if loaded.get("current"):
            try:
                self.queue._current = QueueItem.from_dict(loaded["current"])
            except Exception:
                pass
        if loaded.get("playlist"):
            try:
                items = [QueueItem.from_dict(i) for i in loaded["playlist"]]
                self.queue._items = items
                # Restaurar la posicion real dentro de la playlist (con
                # validacion de rango: un cursor corrupto cae a 0).
                try:
                    cursor = int(loaded.get("cursor", 0) or 0)
                except (TypeError, ValueError):
                    cursor = 0
                self.queue._cursor = max(0, min(cursor, len(items) - 1))
            except Exception:
                pass
        if loaded.get("history"):
            try:
                self.queue._history_items = deque(
                    [QueueItem.from_dict(i) for i in loaded["history"][-100:]],
                    maxlen=100,
                )
            except Exception:
                pass
        card = loaded.get("card") or {}
        self._card_chat_id = card.get("chat_id")
        self._card_message_id = card.get("message_id")
        self._card_is_photo = bool(card.get("is_photo"))

        # Pagina del listado 📋 en la que iba el usuario: se restaura para
        # no volver siempre al inicio tras un reinicio (validada >= 0).
        try:
            pagina = int(loaded.get("list_page", 0) or 0)
        except (TypeError, ValueError):
            pagina = 0
        self._list_page = max(0, pagina)

        # Tope de resolucion: un valor persistido invalido (>1080, caida del
        # tope antiguo) se ignora y se vuelve a 1080 por defecto.
        persistido = loaded.get("max_height")
        if persistido in QUALITY_LEVELS:
            self._max_height = persistido
            set_max_height(persistido)
        else:
            self._max_height = None
            set_max_height(1080)

    async def _secuencia_arranque(self, bot) -> None:
        """Lo que hace el bot en el instante de arrancar (post_init).

        Vive como metodo con nombre, y no como un closure suelto dentro de
        build(), por una razon concreta: al ser anonimo NO SE PODIA PROBAR. Un
        bug de arranque (la tarjeta que se borraba y nunca volvia) convivio con
        la suite entera en verde porque las pruebas arman el estado a mano en
        vez de arrancar el bot. Con el arranque en un metodo, se puede arrancar
        de verdad en una prueba.

        Orden a proposito: primero la tarjeta, despues el backlog (que se lee
        antes de que el polling lo consuma), y al final lo que puede fallar sin
        tumbar el arranque.
        """
        # La tarjeta persistida de una sesion anterior se borra al arrancar: era
        # la causa de las tarjetas huerfanas apiladas (cada reinicio dejaba una
        # foto vieja muerta; el bot nuevo la editaba sin saber si era foto o
        # texto).
        #
        # El chat se toma ANTES de borrar: _remove_card() pone _card_chat_id en
        # None y la condicion siguiente jamas podia ser cierta, asi que la
        # tarjeta se borraba y NO volvia a existir. Sin tarjeta no hay botones y
        # el usuario se queda sin forma de reanudar lo que estaba sonando.
        chat_de_la_card = self._card_chat_id or self.config.allowed_chat_id
        if self._card_message_id is not None:
            await self._remove_card()
            self._persist_dirty()
        # Si venia una cancion restaurada se crea una tarjeta FRESCA al final
        # del chat con el aviso de "retoma". No se re-edita la vieja: esta
        # borrada. _track_status_text() consume el flag _restored, asi que el
        # aviso de reanudacion aparece UNA sola vez y no se repite despues.
        if self._restored and chat_de_la_card is not None:
            await self._send_card(chat_de_la_card)
        # Lo que llego mientras el bot estuvo apagado se descarta, sin aviso:
        # el usuario ya se entero en su PC de que no habia red.
        await self._descartar_backlog(bot)
        self._marcar_conexion(True, "arranque")
        # Reglas del grupo fijadas al arranque (si el kick esta activado):
        # el mensaje de bienvenida apunta a ellas como referencia ↑.
        await self._ensure_pinned_rules(bot)
        # Menú de comandos: al escribir "/" Telegram muestra esta lista.
        # Dos listas por audiencia: los solo-admin van en el scope que ven
        # solo los admins del grupo; el resto en el default. Ningun comando
        # se queda invisible (lo garantiza la prueba de cobertura).
        try:
            await bot.set_my_commands(_COMANDOS_PUBLICOS)
            await bot.set_my_commands(
                _COMANDOS_ADMIN, scope=BotCommandScopeAllChatAdministrators()
            )
        except Exception as exc:  # noqa: BLE001 - no debe tumbar el arranque
            logger.warning("No se pudo registrar los comandos: %s", exc)
        await self._sincronizar_menu_dj(bot)

    async def _sincronizar_menu_dj(self, bot) -> None:
        """El menu "/" de los comandas de DJ se aplica POR USUARIO con el
        scope `BotCommandScopeChatMember` (visible SOLO para ese miembro del
        grupo) a cada dj/admin conocido. Quien pierde el rol conserva el
        menu viejo hasta que se le borre explicitamente: por eso el helper
        tambien tira `delete_my_commands` de los djs que se quedaron fuera.
        Al arrancar y DESPUES de cada commit de roles (👥)."""
        chat = self.config.allowed_chat_id
        if not chat:
            return
        dj_y_admin = set(self.roles.get_all_with_role("dj")) | set(
            self.roles.get_all_with_role("admin")
        )
        quitados = self._comandos_dj_aplicados - dj_y_admin
        self._comandos_dj_aplicados = set(dj_y_admin)
        for uid in quitados:
            try:
                await bot.delete_my_commands(
                    scope=BotCommandScopeChatMember(chat_id=chat, user_id=uid)
                )
            except Exception as exc:  # noqa: BLE001 - el toast/persistencia sigue
                logger.warning(
                    "No se pudo esconder el menu de dj para %s: %s", uid, exc
                )
        for uid in sorted(dj_y_admin):
            try:
                await bot.set_my_commands(
                    _COMANDOS_DJ,
                    scope=BotCommandScopeChatMember(chat_id=chat, user_id=uid),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "No se pudo aplicar el menu de dj para %s: %s", uid, exc
                )

    def _push_to_nav_back(self) -> None:
        """Guarda el item actual en la pila de navegación hacia atrás.

        Se llama antes de cambiar a otro tema (via /next, /prev, /buscar, etc.).
        Siempre limpia la pila forward porque una nueva elección rompe la
        posibilidad de re‑avanzar.
        """
        if self.queue.current is not None:
            self._nav_back.append(self.queue.current)
        self._nav_forward.clear()

    def _pop_from_nav_back(self) -> QueueItem | None:
        """Poppea el último item guardado en la pila hacia atrás."""
        if self._nav_back:
            return self._nav_back.pop()
        return None

    def _pop_from_nav_forward(self) -> QueueItem | None:
        """Poppea el último item guardado en la pila hacia adelante."""
        if self._nav_forward:
            return self._nav_forward.pop()
        return None

    def _clear_nav_stacks(self) -> None:
        """Limpia ambas pilas. Se llama al cambiar de fuente (nueva búsqueda,
        nuevo /play, detener, etc.) para que el historial no se mezcle."""
        self._nav_back.clear()
        self._nav_forward.clear()

    def _snapshot_reproduccion(self) -> dict:
        """Copia de todo lo que un arranque nuevo va a reemplazar.

        La usa /buscar y /play con link: si el tema elegido no arranca, con
        restore() la lista, el historial, el artista de la radio y la tarjeta
        quedan exactamente como estaban.
        """
        return {
            "queue": self.queue.snapshot(),
            "radio_artist": self._radio_artist,
            "search_artist": self._search_artist,
            "nav_back": list(self._nav_back),
            "nav_forward": list(self._nav_forward),
            "card_chat_id": self._card_chat_id,
            "card_message_id": self._card_message_id,
            "card_is_photo": self._card_is_photo,
        }

    def _restore_reproduccion(self, snap: dict) -> None:
        """Vuelve al estado guardado por _snapshot_reproduccion()."""
        self.queue.restore(snap["queue"])
        self._radio_artist = snap["radio_artist"]
        self._search_artist = snap["search_artist"]
        self._nav_back.clear()
        self._nav_back.extend(snap["nav_back"])
        self._nav_forward.clear()
        self._nav_forward.extend(snap["nav_forward"])
        self._card_chat_id = snap["card_chat_id"]
        self._card_message_id = snap["card_message_id"]
        self._card_is_photo = snap["card_is_photo"]
        self._persist_dirty()

    def _register_owner(self) -> None:
        """El dueno (OWNER_ID) queda como admin automaticamente SOLO en el
        arranque en frio (tabla `usuarios` sin nadie).

        El auto-registro en TODO arranque anulaba en silencio cualquier
        cambio de rol hecho despues (con dev_rol o el editor \U0001f465): se
        bajaba al dueno a dj y al reiniciar volvia admin solo."""
        if self.config.owner_id is None:
            logger.warning("OWNER_ID no configurado en .env: no hay admin inicial.")
            return
        if self.roles.has_role(self.config.owner_id, "admin"):
            return
        if self.roles.vacio():
            self.roles.set_role(self.config.owner_id, "admin")
            logger.info(
                "Dueno %s registrado como admin (primer arranque).",
                self.config.owner_id,
            )
        else:
            logger.info(
                "Dueno %s entro sin admin: ya hay usuarios, no se fuerza su rol.",
                self.config.owner_id,
            )

    @property
    def app(self) -> Application:
        return self._app

    def build(self) -> Application:
        app = Application.builder().token(self.config.token).build()

        # Comandos principales (el menu "/" se registra en post_init).
        # _with_card_reposition: al terminar cualquier comando, si la tarjeta
        # ya existia, se re-envia al final del chat (la card queda como ultima
        # visualizacion; los mensajes del comando quedan arriba).
        app.add_handler(CommandHandler("start", self._with_card_reposition(self._require_chat(self._require("dj", self.cmd_start)))))
        app.add_handler(CommandHandler("buscar", self._with_card_reposition(self._require_chat(self._require("dj", self.cmd_play)))))
        app.add_handler(CommandHandler("play", self._with_card_reposition(self._require_chat(self._require("dj", self.cmd_play)))))
        app.add_handler(
            CommandHandler(
                "reglas",
                self._with_card_reposition(self._require_chat(self._require("admin", self.cmd_reglas))),
            )
        )
        app.add_handler(
            CommandHandler(
                "salud",
                self._with_card_reposition(self._require_chat(self._require("admin", self.cmd_salud))),
            )
        )
        app.add_handler(
            CommandHandler(
                "actualizar",
                self._with_card_reposition(self._require_chat(self._require("admin", self.cmd_actualizar))),
            )
        )
        # /radio y /letra tambien son de dj+: quien no tiene rol no tiene
        # ningun derecho (regla del usuario); el listado del radio sigue
        # exigiendo dj DENTRO del handler de callbacks para elegir.
        app.add_handler(
            CommandHandler(
                "radio",
                self._with_card_reposition(self._require_chat(self._require("dj", self.cmd_radio))),
            )
        )
        app.add_handler(
            CommandHandler(
                "letra",
                self._with_card_reposition(self._require_chat(self._require("dj", self.cmd_letra))),
            )
        )
        # Editor de usuarios (boton 👥): los callbacks mem:* viven en el chat
        # PRIVADO del admin. El handler corre con guard propio (ver
        # _on_members_callback) y DEBE registrarse antes del callback generico
        # para no ser tragado por el filtro de chat permitido del grupo.
        app.add_handler(CallbackQueryHandler(self._on_members_callback, pattern="^mem:"))
        app.add_handler(CallbackQueryHandler(self._require_chat(self.on_callback)))
        # Alguien se unio al grupo: queda registrado (timestamp del conteo 24h)
        # y recibe la bienvenida que apunta a las reglas fijadas.
        app.add_handler(
            MessageHandler(
                filters.StatusUpdate.NEW_CHAT_MEMBERS,
                self._require_chat(self._on_new_members),
            )
        )
        # Cualquier mensaje de texto en el chat (alguien escribio algo) re-posiciona
        # la tarjeta como ultimo mensaje. Va despues de los comandos para no
        # interceptarlos; el handler no responde nada.
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._require_chat(self._passive_card_reposition),
            )
        )

        self._app = app

        async def post_init(_app: Application) -> None:
            # Marca de arranque: si ves DOS de estas sin haber reiniciado, hay
            # mas de una instancia del bot con el mismo token (una segunda
            # instancia pierde mensajes en silencio).
            logger.info(
                "BOT ARRANCADO (pid=%s)",
                os.getpid(),
            )
            await self._secuencia_arranque(_app.bot)

        app.post_init = post_init

        # Job periódico para verificar si hay que avanzar la cola
        # (se ejecuta cada 5 segundos empezando a los 10s).
        if app.job_queue:
            app.job_queue.run_repeating(
                self._check_queue_advance_job,
                interval=5,
                first=10,
            )
            # Vigilante de conexion: detecta la caida de red del polling y,
            # al volver, descarta el backlog acumulado con aviso al usuario.
            app.job_queue.run_repeating(self._net_watch_job, interval=15, first=15)
            # Auto-expulsion de invitados (config.kick_after_hours): revisa
            # cada 5 minutos si algun usuario con rol 'user' supero el plazo.
            app.job_queue.run_repeating(self._auto_kick_job, interval=300, first=60)
        # Errores de red del polling marcan el bot como offline.
        app.add_error_handler(self._on_bot_error)
        app.run_polling  # noqa: B018  (validar metodo disponible)
        return app

    async def _descartar_backlog(self, bot) -> None:
        """Descarta lo que llego mientras el bot estuvo apagado, sin avisar.

        Los comandos NO se ejecutan (un /stop viejo no debe cortar la musica
        minutos despues), pero ya no se manda el reporte de "se descartaron
        estos comandos": es un post-mortem. Al usuario le sirve saber si puede
        o no ANTES de enviar, no una lista de lo que se perdio despues. Ese
        aviso lo tiene ya en su PC, en el momento en que se cae la red.

        Se llama en post_init, antes de que el polling consuma el backlog.
        Si falla, el bot arranca igual (nadie queda bloqueado).
        """
        try:
            await bot.get_updates(offset=-1, timeout=0)
        except Exception as exc:  # noqa: BLE001 - no debe tumbar el arranque
            logger.warning("No se pudo descartar el backlog: %s", exc)

    async def _on_bot_error(self, update: object, context) -> None:
        """Marca el bot como OFFLINE cuando el polling falla por red.

        Errores de red REALES (sin internet, Telegram inalcanzable) ponen la
        bandera que el vigilante (_net_watch_job) usa para avisar al volver.
        Los errores de API (BadRequest y similares) NO cuentan como caida de
        red: en este PTB BadRequest hereda de NetworkError, asi que hay que
        filtrar por la clase exacta para no apagar el bot ante un rechazo
        puntual de Telegram.
        """
        error = context.error
        is_real_network = (
            isinstance(error, (NetworkError, TimedOut))
            and type(error) in (NetworkError, TimedOut)
        )
        if is_real_network:
            self._net_offline = True
            self._marcar_conexion(False, type(error).__name__)
            self._detener_polling(context)
            logger.warning("Red del bot caida (%s). Se avisara al volver.", type(error).__name__)
        else:
            logger.error("Error del bot: %r", error)

    def _detener_polling(self, context) -> None:
        """Deja de pedirle updates a Telegram en cuanto se cae la red.

        ESTE es el arreglo de la carrera que el usuario vio: el bot le decia
        "tu comando se perdio, mandalo de nuevo" y un segundo despues aparecian
        los resultados de ese mismo comando. No era que el aviso mintiera por
        descuido: DOS partes leian la misma tanda. El polling de Telegram se
        recupera solo y ejecutaba lo que habia llegado durante la caida, y hasta
        15 segundos despues el vigilante volvia a leer ESA MISMA tanda (todavia
        sin confirmar, porque confirmar es un_offset- del proximo_) y la
        declaraba descartada. Uno ejecutaba y el otro tiraba.

        Por eso el polling se detiene al CAER la red, no al volver. Mientras no
        hay red no se puede traer nada, asi que darle tiempo de sobra para que
        el bucle se cierre antes de que vuelva la senal elimina la ventana.

        Va como tarea agendada y no como await: detener el polling desde dentro
        del handler de errores esperaria al propio bucle que nos esta llamando.
        """
        if self._net_stop_task is not None:
            return
        updater = getattr(context.application, "updater", None)
        if updater is None:
            return
        self._net_stop_task = asyncio.ensure_future(self._cerrar_polling(updater))

    async def _cerrar_polling(self, updater) -> None:
        """Cierre real del polling. Aislado para poder esperar por el."""
        try:
            await updater.stop()
        except Exception as exc:  # noqa: BLE001 - no debe tumbar nada
            logger.warning("No se pudo pausar el polling al caer la red: %s", exc)

    async def _net_watch_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Vigilante de conexion: unico lector de lo que se acumulo mientras no
        habia red.

        Mientras esta offline solo sondea. Al volver, ESPERA a que el polling este
        realmente cerrado, lee la tanda una sola vez y la descarta. Los comandos
        se descartan SIEMPRE: un /stop viejo no debe cortar la musica minutos
        despues de la caida. Como ya no hay otro lector, el aviso que sale
        despues es cierto.
        """
        if not self._net_offline:
            return
        try:
            await context.bot.get_me()
        except Exception:  # noqa: BLE001 - todavia sin conexion
            return
        # La conexion volvio. Primero se ESPERA a que el polling este cerrado:
        # si el vigilante leyera la tanda antes, el polling todavia podria
        #.dispatcharla y volveriamos a tener dos lectores. Ese era el defecto.
        updater = getattr(context.application, "updater", None)
        cerrado = False
        if self._net_stop_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._net_stop_task), timeout=20)
                cerrado = True
            except Exception as exc:  # noqa: BLE001 - igual se intenta el cierre
                logger.warning("El polling no confirmo el cierre a tiempo: %s", exc)
            self._net_stop_task = None
        elif updater is not None:
            await self._cerrar_polling(updater)
            cerrado = True
        try:
            self._net_offline = False
            self._marcar_conexion(True, "volvio la red")
            # Unica lectura de la tanda: todavia sin confirmar, asi que esto es
            # exactamente lo que se va a descartar.
            try:
                # offset=-1 confirma y descarta TODO el backlog sin ejecutarlo.
                # NO se manda ningun aviso: el usuario ya se entero AL INSTANTE
                # de la caida, en su propia PC (ventana e icono del reloj). Un
                # "tu comando se perdio" llegado despues no le sirve para nada
                # y solo lo confunde si el comando si llego a ejecutarse.
                await context.bot.get_updates(offset=-1, timeout=0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo descartar el backlog: %s", exc)
        finally:
            if cerrado and updater is not None:
                try:
                    await updater.start_polling(
                        drop_pending_updates=False,
                        allowed_updates=["message", "callback_query"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("No se pudo reanudar el updater: %s", exc)

    def _require(self, role: str, handler):
        """Envuelve un handler exigiendo un rol minimo (dj+ en toda la
        superficie de control: la regla del usuario es que quien no es
        dj/admin NO tiene ningun derecho en el bot)."""

        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            if user is None:
                return
            if not self.roles.has_role(user.id, role):
                await self._reply(update, "🔒 El bot es solo para dj/admin.")
                return
            await handler(update, context)

        wrapper.__name__ = handler.__name__
        return wrapper

    def _chat_allowed(self, update: Update) -> bool:
        """Restringe el bot al chat permitido.

        - Si ALLOWED_CHAT_ID esta definido: solo ese chat.
        - Si no esta definido (primera vez): solo el dueno, hasta que
          el propio /start configure el grupo permitido.
        """
        if self.config.allowed_chat_id is None:
            user = update.effective_user
            return user is not None and user.id == self.config.owner_id
        chat_id = update.effective_chat.id if update.effective_chat else None
        return chat_id == self.config.allowed_chat_id

    def _require_chat(self, handler):
        """Envuelve un handler exigiendo que el chat este permitido."""

        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._chat_allowed(update):
                chat_id = update.effective_chat.id if update.effective_chat else None
                logger.info("Rechazado mensaje de chat no permitido (chat_id=%s).", chat_id)
                return
            await handler(update, context)

        wrapper.__name__ = getattr(handler, "__name__", "wrapper")
        return wrapper

    async def _card_exists_in(self, chat_id: int | None) -> bool:
        return await self.card.exists_in(chat_id)

    def _with_card_reposition(self, handler):
        return self.card.with_reposition(handler)


    async def _passive_card_reposition(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Cualquier mensaje de texto de alguien en el chat re-posiciona la
        tarjeta como ultimo mensaje (se desvanece la vieja y aparece abajo).
        No responde nada: el bot solo mantiene la tarjeta a la vista."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        self._register_known_user(update.effective_user)
        if await self._card_exists_in(chat_id):
            await self._reposition_card(chat_id)  # type: ignore[arg-type]

    def _register_known_user(self, user) -> None:
        """Registra al autor de un update en el registro de usuarios conocidos
        (si ya existe, solo refresca el nombre; nunca rearma su fecha)."""
        if user is None or user.id is None:
            return
        name = (
            getattr(user, "full_name", None)
            or getattr(user, "first_name", None)
            or getattr(user, "username", None)
            or str(user.id)
        )
        self.roles.register_user(user.id, name)

    async def _on_new_members(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Alguien se unio al grupo: registrado con su timestamp (origen del
        conteo de las 24h) y recibe la bienvenida por el grupo. El propio bot
        (cuando lo agregan) no genera bienvenida."""
        message = update.message
        if message is None or message.chat is None:
            return
        bot_id: int | None = None
        try:
            bot_id = context.bot.id
        except Exception:  # noqa: BLE001 - el id es solo para auto-exclusion
            bot_id = None
        for member in message.new_chat_members or []:
            if member.id == bot_id:
                continue
            self._register_known_user(member)
            name = (
                getattr(member, "full_name", None)
                or getattr(member, "first_name", None)
                or getattr(member, "username", None)
                or str(member.id)
            )
            mention = f"@{member.username}" if getattr(member, "username", None) else name
            text = (
                f"👋 ¡Bienvenido {mention}!\n"
                "Las reglas del grupo están fijadas arriba ↑.\n"
                "🔒 El bot es solo para dj/admin: entra como invitado y quien\n"
                "no queda habilitado no puede usarlo. Para ser dj, pida a un\n"
                "admin que lo haga con el botón 👥 Usuarios de la tarjeta."
            )
            if (self.config.kick_after_hours or 0) > 0:
                horas = int(self.config.kick_after_hours)
                text += f"\nAcceso de invitado: {horas} h. Pasado el plazo se lo retira del grupo si no tiene rol dj/admin."
            try:
                await self._enviar_al_chat(message.chat.id, text)
            except Exception as exc:  # noqa: BLE001 - no romper la entrada
                logger.warning("No se pudo enviar la bienvenida: %s", exc)

    def rules_text(self) -> str:
        """Mensaje de reglas del grupo (se fija como referencia de bienvenida).

        El plazo invitado aparece solo si la auto-expulsion esta activada
        (kick_after_hours > 0 en config.json).
        """
        lines = [
            "📌 YT-Remote — Reglas del grupo",
            "🎧 Bot que reproduce música (YouTube) en la PC de casa.",
            "",
            "• Roles: dj (controla la reproducción) y admin. El bot es solo para dj.",
            "• Quien no tiene rol no puede usar el bot: ni comandos, ni listados, ni botones.",
            "• Para ser DJ: un admin debe habilitarlo con el botón 👥 Usuarios de la tarjeta.",
        ]
        horas = int(self.config.kick_after_hours or 0)
        if horas > 0:
            lines.append(
                f"• Acceso de invitado: {horas} h. Pasado el plazo se lo retira del grupo; puede volver con el enlace de invitación."
            )
        lines += [
            "• /buscar es solo para dj: elija un resultado y se reproduce.",
            "• La tarjeta del reproductor (con 📋, ⚙️ y 👥) es de dj/admin.",
        ]
        return "\n".join(lines)

    async def _ensure_pinned_rules(self, bot) -> None:
        """Fija las reglas del grupo si la auto-expulsion esta activa y el
        mensaje fijado no es nuestro. No tumba el arranque si falla (el bot
        necesita ser admin del grupo con permiso de fijar mensajes)."""
        chat = self.config.allowed_chat_id
        if chat is None or (self.config.kick_after_hours or 0) <= 0:
            return
        try:
            info = await bot.get_chat(chat)
            pinned = getattr(info, "pinned_message", None)
            me = await bot.get_me()
            if pinned is not None and getattr(pinned.from_user, "id", None) == me.id:
                return
            msg = await bot.send_message(chat, self.rules_text())
            await bot.pin_chat_message(chat, msg.message_id)
            logger.info("Reglas del grupo fijadas.")
        except Exception as exc:  # noqa: BLE001 - no debe tumbar el arranque
            logger.warning(
                "No se pudo fijar las reglas: %s (¿el bot es admin del grupo?)", exc
            )

    async def cmd_reglas(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Admin: envía y fija (pin) el mensaje de reglas del grupo."""
        chat = update.effective_chat
        chat_id = chat.id if chat else None
        if chat_id is None:
            return
        try:
            msg = await context.bot.send_message(chat_id, self.rules_text())
            await context.bot.pin_chat_message(chat_id, msg.message_id)
            await self._reply(update, "Reglas fijadas como mensaje del grupo.")
        except Exception as exc:  # noqa: BLE001 - no romper el flujo
            logger.warning("No se pudo fijar las reglas: %s", exc)
            await self._reply(
                update,
                "No pude fijar las reglas. El bot debe ser admin del grupo con permiso de fijar mensajes.",
            )

    async def cmd_salud(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Admin: reproductor, version de YT-Remote y estado en una pantalla.

        Nada se consulta afuera: es lo que el propio proceso ve. La linea
        de estado es honesta a proposito — si este mensaje te llego, hay
        conexion; un ping extra solo podria mentir. La version es la de
        YT-Remote (constants.VERSION), la misma del release de GitHub:
        las piezas internas no son asunto del usuario.
        """
        lineas = ["🩺 YT-Remote — estado:"]

        if self.player.is_running:
            lineas.append("✅ Reproductor: corriendo")
        else:
            lineas.append("⚠️ Reproductor: apagado (arranca al reproducir)")

        lineas.append(f"✅ Version: {VERSION}")

        lineas.append("✅ Estado: conectado")

        await self._reply(update, "\n".join(lineas))

    async def cmd_actualizar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Admin: trae YT-Remote y sus componentes internos a la version
        publicada mas nueva, en UNA pasada.

        Revisa el ULTIMO release del repo en GitHub (la version del bot es
        constants.VERSION) y los componentes internos; actualiza lo que
        este atrasado. Los mensajes NUNCA nombran piezas internas (regla
        del usuario): el admin solo ve YT-Remote, las versiones publicas y
        el motivo honesto si algo fallo. La app no puede pisar su propio
        exe: baja el ZIP, deja el ayudante corriendo (src/updater.py con el
        runtime incluido), deja la marca salir_total.flag y se apaga; el
        ayudante espera el cierre, cambia los archivos conservando la
        carpeta data y abre YT-Remote otra vez.
        """
        avisos: list[str] = []

        # 1) Hay release nuevo de YT-Remote? (GitHub = la verdad publica)
        release = None
        try:
            release = await _pedir_meta_release()
        except ActualizacionError as exc:
            avisos.append(f"No se pudo revisar la version de YT-Remote ({exc})")

        # 2) Hay componentes internos nuevos? (regla del usuario: ni su
        # nombre ni su existencia se ven en los mensajes del chat)
        interna = None
        try:
            interna = await _pedir_meta_ytdlp()
        except ActualizacionError as exc:
            avisos.append(f"No se pudieron revisar los componentes internos ({exc})")
        nueva_interna: tuple[str, str, str] | None = None
        instalada_interna = _version_interna_instalada()
        if (
            interna is not None
            and instalada_interna is not None
            and _version_num(instalada_interna) < _version_num(interna[0])
        ):
            nueva_interna = interna

        hay_app = release is not None and _version_num(VERSION) < _version_num(
            release[0]
        )

        if not hay_app and nueva_interna is None:
            if avisos:
                await self._reply(update, f"⚠️ {' '.join(avisos)}")
                return
            await self._reply(update, f"✅ YT-Remote {VERSION} ya está al día.")
            return

        # Se aplica la pieza interna PRIMERO: si sale bien queda mas nueva
        # que la que el release trae dentro y el reemplazo la preserva (los
        # saltos de _actualizar_aplicacion). Si falla, el aviso va honesto
        # y el release se trae la suya.
        interna_hecha: tuple[str, str] | None = None
        if nueva_interna is not None:
            try:
                antes, despues = await self._actualizar_ytdlp()
                interna_hecha = (antes, despues or nueva_interna[0])
            except ActualizacionError as exc:
                avisos.append(f"No se pudo actualizar los componentes internos ({exc})")

        if hay_app:
            if not self._ayudante_disponible():
                lineas_dev = [f"🚀 YT-Remote {VERSION} → {release[0]} disponible."]
                if interna_hecha is not None:
                    lineas_dev.append(
                        f"✅ Componentes internos: {interna_hecha[0]} → {interna_hecha[1]}."
                    )
                    lineas_dev.append(
                        "Reinicia YT-Remote para tomar los componentes nuevos."
                    )
                lineas_dev.append(
                    "Esta instalación no permite el cambio automático: se hace desde la carpeta portable de YT-Remote."
                )
                await self._reply(update, "\n".join(lineas_dev))
                return
            anuncio = [f"🚀 Actualizando YT-Remote {VERSION} → {release[0]}."]
            if interna_hecha is not None:
                anuncio.append(
                    f"✅ Componentes internos: {interna_hecha[0]} → {interna_hecha[1]}."
                )
            anuncio.append("YT-Remote se cierra solo y vuelve al rato.")
            try:
                ruta_zip, saltos = await self._actualizar_aplicacion(
                    release, interna_hecha is not None
                )
            except ActualizacionError as exc:
                final = f"⚠️ No se pudo bajar la versión {release[0]}: {exc}"
                if interna_hecha is not None:
                    final += (
                        "\n✅ Componentes internos: "
                        f"{interna_hecha[0]} → {interna_hecha[1]}.\n"
                        "Van a servir al reiniciar YT-Remote."
                    )
                await self._reply(update, final)
                return
            # El aviso es lo ULTIMO que se manda: despues se deja el
            # ayudante corriendo, se marca la salida y el proceso se apaga.
            await self._reply(update, "\n".join(anuncio))
            self._lanzar_ayudante(ruta_zip, saltos)
            await self._apagar_y_salir("actualizacion")
            return

        # Solo componentes internos (no hay YT-Remote nuevo).
        if interna_hecha is None:
            await self._reply(update, f"⚠️ {' '.join(avisos)}")
            return
        texto = (
            f"✅ Componentes internos actualizados: {interna_hecha[0]} → {interna_hecha[1]}."
        )
        if self._ayudante_disponible():
            await self._reply(update, texto + "\nYT-Remote se reinicia solo.")
            self._lanzar_ayudante(None, [])
            await self._apagar_y_salir("componentes")
            return
        await self._reply(update, texto + "\nReinicia YT-Remote para aplicarlos.")

    async def _actualizar_ytdlp(self) -> tuple[str, str | None]:
        """(version actual, version nueva o None si ya estaba al dia).

        Metodo de la instancia a proposito: las pruebas lo stubbean y no
        hay red. Lanza ActualizacionError con el motivo honesto.
        """
        try:
            import yt_dlp.version

            actual = yt_dlp.version.__version__
            destino = Path(yt_dlp.__file__).resolve().parent
        except Exception as exc:  # noqa: BLE001
            raise ActualizacionError(
                f"no se pudo leer la version instalada ({exc})"
            ) from exc
        nueva, url, sha256 = await _pedir_meta_ytdlp()
        if _version_num(actual) >= _version_num(nueva):
            return actual, None
        wheel = await _bajar_wheel_ytdlp(url, sha256)
        _instalar_wheel_ytdlp(wheel, destino)
        leido = _leer_version_paquete(destino)
        if leido is None:
            raise ActualizacionError("el paquete quedo sin version legible")
        return actual, leido

    async def _actualizar_aplicacion(
        self,
        release: tuple[str, str, int, str | None],
        interna_puesta: bool,
    ) -> tuple[Path, list[str]]:
        """Baja el ZIP del release, verificado pieza por pieza, listo para
        el ayudante; devuelve (ruta_del_zip, prefijos_que_NO_se_extraen).

        Los prefijos son para preservar los componentes internos: si lo
        instalado ya es tan nuevo como lo maximo publicado, extraer el ZIP
        encima los haria RETROCEDER. `interna_puesta` (el comando recien
        los instalo) tambien pide preservarlos aunque el ZIP no exponga
        version legible.
        """
        import updater

        version_rel, url, size, sha = release
        ruta_zip, prefijo = await _bajar_zip_release(
            url, size, sha, _ruta_zip_instalacion(version_rel)
        )
        zip_interna = _leer_interna_del_zip(ruta_zip, prefijo)
        saltos: list[str] = []
        instalada = _version_interna_instalada()
        if interna_puesta or (
            zip_interna is not None
            and instalada is not None
            and _version_num(instalada) >= _version_num(zip_interna)
        ):
            saltos = [_RUTA_COMPONENTES_EN_ZIP]
        return ruta_zip, saltos

    def _ayudante_disponible(self) -> bool:
        """El ayudante del reinicio existe SOLO en la instalacion portable:
        runtime\\python\\python.exe y ytremote.exe viven al lado de src\\.
        En desarrollo no hay exe que reabrir: los mensajes explicitan la
        salida manual. Sin esta puerta, /actualizar en desarrollo podria
        dejar la PC sin bot y sin forma de volverlo a abrir."""
        base = _base_instalacion()
        return (base / "runtime" / "python" / "python.exe").is_file() and (
            base / "ytremote.exe"
        ).is_file()

    def _lanzar_ayudante(self, ruta_zip: str | None, saltos: list[str]) -> None:
        """Deja el ayudante corriendo APARTE, sin consola y sin depender de
        este proceso: espera a que TODO YT-Remote (bot + launcher) termine,
        aplica los cambios y reabre. Por que aparte: el proceso que corre
        no puede reemplazarse a si mismo."""
        import subprocess

        base = _base_instalacion()
        args = [
            "-X",
            "utf8",
            str(Path(__file__).resolve().parent / "updater.py"),
            "--dest",
            str(base),
            "--pid",
            str(os.getpid()),
            "--ppid",
            str(os.getppid()),
            "--exe",
            str(base / "ytremote.exe"),
            "--log",
            str(base / "data" / "actualizacion.log"),
        ]
        if ruta_zip:
            args += ["--zip", str(ruta_zip)]
        for salto in saltos:
            args += ["--skip", salto]
        subprocess.Popen(
            [str(base / "runtime" / "python" / "python.exe")] + args,
            cwd=str(base),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    async def cmd_radio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Emisoras de internet (radio-browser): el 📻 del salon.

        Sin argumentos abre el HOME del menu: dos listados, Paises (el del
        salon primero, luego los cercanos) y Generos (los que de verdad se
        usan en ese pais). Con argumentos, el texto es la busqueda (ver
        _radio_parsear): por nombre, con "genero:<texto>" por genero y
        "pais:<texto>" por pais; y "/radio favoritas" abre la lista propia.

        El listado es un mensaje aparte (teclas 🔥 ❤️ ◀ ▶ ❌) que cualquiera
        puede abrir; ELEGIR una emisora o tocar favoritas es de dj, igual
        que el resto de la musica. Si radio-browser no responde, el motivo
        llega honesto en un solo mensaje.
        """
        vista, termino = _radio_parsear(" ".join(context.args))
        try:
            if vista == "fav":
                items = list(self._radio_favs)
            elif vista == "tag":
                items = await radio.buscar_genero(termino, _RADIO_BATCH)
            elif vista == "pais":
                items = await radio.buscar_pais(termino, _RADIO_BATCH)
            elif vista == "name":
                items = await radio.buscar_nombre(termino, _RADIO_BATCH)
            else:
                # El ARRANQUE sin argumentos (_radio_parsear lo trae como
                # "top") es el HOME del menu: ni una consulta al API ni
                # ninguna suposicion de idioma o pais de antemano.
                vista = "home"
                items = []
        except radio.RadioError as exc:
            await self._reply(update, f"⚠️ No se pudo buscar emisoras: {exc}")
            return
        if not items and vista != "home":
            if vista == "fav":
                await self._reply(update, "Todavía no guardas emisoras favoritas.")
            elif termino:
                await self._reply(update, f"No encontré emisoras para «{termino}».")
            else:
                await self._reply(update, "No encontré emisoras destacadas.")
            return
        self._radio_view = {"top": "home", "fav": "fav", "home": "home"}.get(
            vista, "busqueda"
        )
        self._radio_query = termino
        self._radio_items = items
        self._radio_page = 0
        await self._open_radio_list(update.effective_chat.id)

    async def _open_radio_list(self, chat_id: int) -> None:
        """Manda el mensaje-lista de /radio (la tarjeta nunca se pisa).

        Si ya habia un listado de radio abierto se borra y se manda uno
        fresco, igual que decide el hub del 📋: nunca editar una lista
        existente.
        """
        await self._close_radio_message()
        try:
            msg = await self._app.bot.send_message(
                chat_id, self._radio_text(), reply_markup=self._radio_keyboard()
            )
        except Exception as exc:  # noqa: BLE001 - no romper el flujo
            logger.warning("No se pudo enviar el listado de radio: %s", exc)
            return
        self._radio_chat_id = chat_id
        self._radio_message_id = msg.message_id
        # Listado en pantalla: la card no se reposiciona mientras se
        # navega (estra norma de /buscar; con el API caido y el comando
        # fallido la card TAMPOCO se mueve, porque no paso nada).
        self._radio_list_pending = True

    def _radio_pagina(self) -> list:
        """La pagina activa de la vista de /radio (emisoras del lote)."""
        inicio = self._radio_page * _RADIO_PAGE_SIZE
        return self._radio_items[inicio : inicio + _RADIO_PAGE_SIZE]

    def _radio_tam_pagina(self) -> int:
        """Botones por pagina segun la vista: las emisoras van 5 por pagina
        con su texto; los directorios (paises/generos) son solo botones y
        caben 10."""
        return (
            _RADIO_DIRECTORIO
            if self._radio_view in ("paises", "generos")
            else _RADIO_PAGE_SIZE
        )

    def _radio_clamp_page(self) -> None:
        """La pagina nunca se sale del lote cargado (si se vacio la vista,
        vuelve a la primera)."""
        total = (
            len(self._radio_paises)
            if self._radio_view == "paises"
            else len(self._radio_generos)
            if self._radio_view == "generos"
            else len(self._radio_items)
        )
        if total:
            ultimo = (total - 1) // self._radio_tam_pagina()
            self._radio_page = max(0, min(self._radio_page, ultimo))
        else:
            self._radio_page = 0

    def _radio_text(self) -> str:
        """Encabezado segun la vista activa y la pagina de emisoras. El pais
        va aqui al lado del nombre, no en el teclado: es donde cabe."""
        if self._radio_view == "fav":
            encabezado = "❤️ Tus emisoras favoritas:"
        elif self._radio_view == "busqueda":
            encabezado = f"🔎 Emisoras para «{self._radio_query}»:"
        elif self._radio_view == "home":
            encabezado = "📻 ¿Qué escuchamos?"
        elif self._radio_view == "paises":
            pais_salon = radio.pais_visible(
                self._radio_equipo[0] or ""
            ) if self._radio_equipo[0] else ""
            pista = f" (el de aquí primero: {pais_salon})" if pais_salon else ""
            encabezado = f"🌍 Países{pista}:"
        elif self._radio_view == "generos":
            iso = self._radio_equipo[0] or ""
            pais_salon = radio.pais_visible(iso) if iso else ""
            de = f" de {pais_salon}" if pais_salon else ""
            encabezado = f"🎵 Géneros{de} (los de tu sala pesan primero):"
        else:
            encabezado = "🔥 Emisoras destacadas:"
        lineas = [encabezado]
        if self._radio_view in ("home", "paises", "generos"):
            return "\n".join(lineas)
        inicio = self._radio_page * _RADIO_PAGE_SIZE
        for n, e in enumerate(self._radio_pagina(), start=inicio + 1):
            pais = f" — {radio.pais_visible(e.pais)}" if e.pais else ""
            lineas.append(f"{n}. {e.nombre}{pais}")
        if len(lineas) == 1:
            lineas.append("(sin emisoras en esta vista)")
        return "\n".join(lineas)

    def _radio_es_fav(self, emisora) -> bool:
        """La identidad de una emisora es su STREAM: el nombre del API cambia
        (le agregan sucursales, emojis) y el stream es lo que suena."""
        return any(f.stream == emisora.stream for f in self._radio_favs)

    def _radio_keyboard(self) -> InlineKeyboardMarkup:
        """Teclado del listado de /radio.

        - home: los DOS listados del menu (🌍 Paises y 🎵 Generos) con el
          pie de siempre (⭐ ❤️) para quien quiere lo directo.
        - paises/generos: botones del DIRECTORIO (2 por fila, 10 por
          pagina), su navegacion y una vuelta al 📻 Inicio.
        - emisoras (top/fav/busqueda): cada fila es una emisora (elegir)
          con su boton de favorita.

        Los indices de emisora son GLOBALES sobre el lote cargado (rad:play:7
        = la 8a emisora de la vista), asi que paginar no renumeran los
        botones; los del directorio son la posicion en la lista COMPLETA.
        """
        self._radio_clamp_page()
        filas: list[list[InlineKeyboardButton]] = []
        if self._radio_view == "home":
            filas.append(
                [
                    InlineKeyboardButton("🌍 Países", callback_data="rad:paises"),
                    InlineKeyboardButton("🎵 Géneros", callback_data="rad:generos"),
                ]
            )
            filas.append([InlineKeyboardButton("❌", callback_data="rad:close")])
        elif self._radio_view in ("paises", "generos"):
            lista = self._radio_paises if self._radio_view == "paises" else self._radio_generos
            tono = "🌍" if self._radio_view == "paises" else "🎵"
            tam = _RADIO_DIRECTORIO
            base = self._radio_page * tam
            ventana = lista[base : base + tam]
            for par in range(0, len(ventana), 2):
                fila_b: list[InlineKeyboardButton] = []
                for desplaz, dato in enumerate(ventana[par : par + 2]):
                    idx = base + par + desplaz
                    if self._radio_view == "paises":
                        etiqueta = f"{tono} {radio.pais_visible(dato[0])} ({dato[2]})"
                        acc = f"rad:pais:{idx}"
                    else:
                        etiqueta = f"{tono} {dato[0]} ({dato[1]})"
                        acc = f"rad:gen:{idx}"
                    fila_b.append(InlineKeyboardButton(etiqueta, callback_data=acc))
                filas.append(fila_b)
            ultima = (len(lista) - 1) // tam if lista else 0
            filas.append(
                [
                    InlineKeyboardButton(
                        "◀" if self._radio_page > 0 else "·",
                        callback_data="rad:prev" if self._radio_page > 0 else "rad:noop",
                    ),
                    InlineKeyboardButton("❌", callback_data="rad:close"),
                    InlineKeyboardButton(
                        "▶" if self._radio_page < ultima else "·",
                        callback_data="rad:next" if self._radio_page < ultima else "rad:noop",
                    ),
                ]
            )
        else:
            base = self._radio_page * _RADIO_PAGE_SIZE
            for n, e in enumerate(self._radio_pagina(), start=base):
                accion = "❤️ Quitar" if self._radio_es_fav(e) else "❤️ Guardar"
                filas.append(
                    [
                        InlineKeyboardButton(
                            f"📻 {e.nombre}", callback_data=f"rad:play:{n}"
                        ),
                        InlineKeyboardButton(accion, callback_data=f"rad:fav:{n}"),
                    ]
                )
            ultima = (len(self._radio_items) - 1) // _RADIO_PAGE_SIZE if self._radio_items else 0
            filas.append(
                [
                    InlineKeyboardButton(
                        "◀" if self._radio_page > 0 else "·",
                        callback_data="rad:prev" if self._radio_page > 0 else "rad:noop",
                    ),
                    InlineKeyboardButton("❌", callback_data="rad:close"),
                    InlineKeyboardButton(
                        "▶" if self._radio_page < ultima else "·",
                        callback_data="rad:next" if self._radio_page < ultima else "rad:noop",
                    ),
                ]
            )
        filas.append(
            [
                InlineKeyboardButton("📻 Inicio", callback_data="rad:home"),
                InlineKeyboardButton("🔥 Destacadas", callback_data="rad:top"),
                InlineKeyboardButton("❤️ Favoritas", callback_data="rad:fav"),
            ]
        )
        return InlineKeyboardMarkup(filas)

    async def _radio_armar_paises(self) -> None:
        """La lista completa de paises del API ordenada por CERCANIA al
        salon: primero el pais del equipo (si la zona horaria lo dice),
        despues el resto por distancia desde el punto del salon; los paises
        sin coordenadas conocidas van al final por cantidad de emisoras.
        Una consulta al API la primera vez, cacheada por proceso."""
        catalogo = await radio.paises()
        filas = [
            (str(fila.get("name") or ""), str(fila.get("iso_3166_1") or ""), int(fila.get("stationcount") or 0))
            for fila in catalogo.values()
            if fila.get("iso_3166_1")
        ]
        iso_salon, lat_salon, lon_salon = self._radio_equipo
        con_dato: list[tuple[float, int, tuple]] = []
        sin_dato: list[tuple] = []
        for fila in filas:
            coord = radio._PAIS_COORDENADAS.get(fila[1])
            if coord is None:
                sin_dato.append(fila)
            elif fila[1] == iso_salon:
                con_dato.insert(0, (0.0, -1, fila))
            elif lat_salon is not None:
                con_dato.append(
                    (radio._distancia(lat_salon, lon_salon, *coord), 0, fila)
                )
            else:
                sin_dato.append(fila)
        con_dato.sort(key=lambda par: (par[0], par[1]))
        self._radio_paises = [fila for _, _, fila in con_dato] + sorted(
            sin_dato, key=lambda fila: -fila[2]
        )

    async def _radio_armar_generos(self) -> None:
        """La lista de generos del menu: PRIMERO lo que la sala ya sono
        (uso real, la base) y despues los tags con que las emisoras del
        pais del salon se marcan de verdad en el API (suma directa del
        listado, nada inventado). Sin pais y sin sala: lista vacia y el
        aviso honesto lo dice."""
        lista: list[tuple[str, int]] = []
        ya: set[str] = set()
        for dato in self._state.top_generos_uso(10):
            clave = str(dato["clave"]).lower()
            if clave not in ya:
                lista.append((clave, int(dato["usos"])))
                ya.add(clave)
        iso = self._radio_equipo[0]
        if iso:
            try:
                for tag, emisoras in await radio.tags_de_pais(
                    iso, cantidad=300, tope=60
                ):
                    if tag not in ya:
                        lista.append((tag, emisoras))
                        ya.add(tag)
            except radio.RadioError:
                # El API no contesto: el menu de generos queda con lo de la
                # sala (o avisa vacio) sin tumbar el home.
                pass
        self._radio_generos = lista

    async def _radio_render(self) -> None:
        """Repinta el listado abierto (texto + teclado de la vista/pagina
        nueva). Si el mensaje ya no existe, se ignora."""
        if self._radio_chat_id is None or self._radio_message_id is None:
            return
        try:
            await self._app.bot.edit_message_text(
                self._radio_text(),
                chat_id=self._radio_chat_id,
                message_id=self._radio_message_id,
                reply_markup=self._radio_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001 - no romper el flujo
            logger.warning("No se pudo repintar el listado de radio: %s", exc)

    async def _close_radio_message(self) -> None:
        """Borra el mensaje-lista de /radio y limpia las referencias.
        Tolera que el mensaje ya no exista."""
        chat = self._radio_chat_id
        msg = self._radio_message_id
        self._radio_chat_id = None
        self._radio_message_id = None
        # El listado ya no esta en pantalla: la card vuelve a poder
        # reposicionarse con el wrapper de comandos.
        self._radio_list_pending = False
        if msg is not None and chat is not None:
            try:
                await self._app.bot.delete_message(chat, msg)
            except Exception as exc:  # noqa: BLE001 - no romper el flujo
                logger.warning("No se pudo borrar el listado de radio: %s", exc)

    async def _on_radio_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Botones del mensaje-lista de /radio (callback `rad:*`).

        - `rad:home`/`rad:paises`/`rad:generos`: el menu del salon (el
          callback de paises/generos consulta el API la primera vez).
        - `rad:pais:N`/`rad:gen:N` (dj): abre el API con el pais o genero N
          del directorio.
        - `rad:top`/`rad:fav`: cambia la vista sobre el MISMO mensaje (top
          vuelve al API; favoritas lee la lista persistida).
        - `rad:prev`/`rad:next`: pagina; `rad:close` borra el mensaje.
        - `rad:play:N` (dj) suena la emisora N de la vista activa y cierra
          el listado; si no arranca, el listado queda en pie para elegir
          otra (misma garantia que /buscar: nada se destruye antes).
        - `rad:fav:N` (dj) guarda o quita la emisora N de favoritas.
        - `rad:noop`: placeholder inerte.

        Cada callback se responde UNA sola vez (un answer por query), con el
        mismo helper mudo que usa la lista del 📋.
        """
        query = update.callback_query
        data = query.data or ""
        accion = data.split(":", 1)[1] if data.startswith("rad:") else ""
        user_id = query.from_user.id if query.from_user else None

        if accion in ("", "noop"):
            await self._list_answer(query)
            return
        # Regla del usuario: sin rol nada de nada en el listado del radio
        # (ni paginar, ni ver favoritas, ni cerrar el mensaje); un solo aviso.
        if not (user_id is not None and self.roles.has_role(user_id, "dj")):
            await self._list_answer(
                query, "🔒 El bot es solo para dj/admin.", show_alert=True
            )
            return
        if accion == "close":
            await self._list_answer(query)
            chat_fin = self._radio_chat_id
            await self._close_radio_message()
            if chat_fin is not None:
                await self._dejar_card_al_final(chat_fin)
            return
        if accion in ("prev", "next"):
            self._radio_page += -1 if accion == "prev" else 1
            await self._radio_render()
            await self._list_answer(query)
            return
        if accion == "home":
            self._radio_view = "home"
            self._radio_query = ""
            self._radio_items = []
            self._radio_page = 0
            await self._radio_render()
            await self._list_answer(query)
            return
        if accion == "paises":
            try:
                await self._radio_armar_paises()
            except radio.RadioError as exc:
                await self._list_answer(query, str(exc), show_alert=True)
                return
            self._radio_view = "paises"
            self._radio_page = 0
            await self._radio_render()
            await self._list_answer(query)
            return
        if accion == "generos":
            await self._radio_armar_generos()
            if not self._radio_generos:
                await self._list_answer(
                    query,
                    "Sin géneros todavía: escucha algo primero o elige el país"
                    " en 🌍 Países.",
                    show_alert=True,
                )
                return
            self._radio_view = "generos"
            self._radio_page = 0
            await self._radio_render()
            await self._list_answer(query)
            return
        if accion == "top":
            try:
                self._radio_items = await radio.destacadas(_RADIO_BATCH)
            except radio.RadioError as exc:
                await self._list_answer(query, str(exc), show_alert=True)
                return
            self._radio_view = "top"
            self._radio_query = ""
            self._radio_page = 0
            await self._radio_render()
            await self._list_answer(query)
            return
        if accion == "fav":
            self._radio_view = "fav"
            self._radio_query = ""
            self._radio_items = list(self._radio_favs)
            self._radio_page = 0
            await self._radio_render()
            await self._list_answer(query)
            return
        if accion.startswith(("pais:", "gen:")):
            tipo, _, crudo = accion.partition(":")
            try:
                posicion = int(crudo)
            except ValueError:
                await self._list_answer(query)
                return
            lista = self._radio_paises if tipo == "pais" else self._radio_generos
            if not (0 <= posicion < len(lista)):
                await self._list_answer(query)
                return
            if tipo == "pais":
                nombre_api, iso, _ = lista[posicion]
                clave_busqueda = iso or nombre_api
                clave_visible = radio.pais_visible(nombre_api)
                consulta = radio.buscar_pais
            else:
                clave_busqueda = clave_visible = lista[posicion][0]
                consulta = radio.buscar_genero
            try:
                self._radio_items = await consulta(clave_busqueda, _RADIO_BATCH)
            except radio.RadioError as exc:
                await self._list_answer(query, str(exc), show_alert=True)
                return
            if not self._radio_items:
                await self._list_answer(
                    query, f"No encontré emisoras para «{clave_visible}».", show_alert=True
                )
                return
            self._radio_view = "busqueda"
            self._radio_query = clave_visible
            self._radio_page = 0
            await self._radio_render()
            await self._list_answer(query)
            return
        if accion.startswith("play:"):
            await self._radio_play(query, update, user_id, accion)
            return
        if accion.startswith("fav:"):
            await self._radio_favorita(query, user_id, accion)
            return
        await self._list_answer(query)

    def _radio_emisora_de(self, accion: str):
        """La emisora N de la vista activa para un callback rad:play:N /
        rad:fav:N; None si el indice ya no existe (el lote cambio)."""
        try:
            return self._radio_items[int(accion.split(":", 1)[1])]
        except (ValueError, IndexError):
            return None

    def _radio_ancla_indice(self) -> int | None:
        """Posicion de la emisora ACTUAL dentro del ancla de rotacion.
        None si no hay ancla, si lo que suena no es emisora, o si la
        emisora ya no esta en el listado."""
        actual = self.queue.current
        if actual is None or not actual.directo or not self._radio_ancla:
            return None
        for i, e in enumerate(self._radio_ancla):
            if e.stream == actual.url:
                return i
        return None

    def _radio_vecina(self, delta: int):
        """La emisora siguiente (delta=1) o anterior (delta=-1) del listado
        ancla: es como ⏭/⏮ y el auto-avance rotan en modo emisora. None si
        no hay ancla o la emisora actual no sale de ella. Marca
        `_radio_ancla_wrap` cuando la rotacion da la vuelta, para que el
        aviso de "empezo de nuevo" se mande UNA vez."""
        indice = self._radio_ancla_indice()
        if indice is None:
            return None
        total = len(self._radio_ancla)
        self._radio_ancla_wrap = (
            delta > 0 and indice + 1 >= total
        ) or (delta < 0 and indice == 0)
        return self._radio_ancla[(indice + delta) % total]

    def _radio_item_de(self, emisora) -> QueueItem:
        """La emisora como QueueItem directo (lo que rad:play arma)."""
        return QueueItem(
            url=emisora.stream,
            title=emisora.nombre,
            thumbnail=emisora.logo,
            channel=emisora.pais,
            tags=emisora.tags,
            directo=True,
        )

    async def _radio_candidata_vecina(self, delta: int) -> QueueItem | None:
        """La vecina del ancla como item para el AUTO-AVANCE del motor;
        None si no hay ancla (el motor avisa 'se detuvo' con el chat de la
        card). `_radio_ancla_wrap` queda prendida para que el CONSUMIDOR
        mande el aviso de 'empezo de nuevo' y la apague (aqui no hay chat
        disponible: el update es None)."""
        vecina = self._radio_vecina(delta)
        if vecina is None:
            return None
        return self._radio_item_de(vecina)

    async def _radio_sonar_vecina(self, update, delta: int) -> None:
        """⏭/⏮ en modo emisora: reproduce la vecina del listado ancla
        (y la card anuncia cual entra). Sin ancla o con la emisora fuera
        del listado: aviso honesto en vez de improvisar."""
        vecina = self._radio_vecina(delta)
        if vecina is None:
            if self._radio_ancla is None:
                mensaje = (
                    "📻 Para rotar emisoras con "
                    + ("⏭" if delta > 0 else "⏮")
                    + " abrí /radio y elegí una desde un listado."
                )
            else:
                mensaje = (
                    "📻 Esa emisora ya no está en el listado con el que la "
                    "elegiste: abrí /radio para seguir rotando."
                )
            await self._reply(update, mensaje)
            return
        item = self._radio_item_de(vecina)
        ok = await self._play_item(update, item)
        if not ok:
            return
        if self._radio_ancla_wrap:
            self._radio_ancla_wrap = False
            await self._reply(
                update,
                "🔁 La lista de emisoras volvió a empezar desde el principio.",
            )
        aviso = (
            ("📻 Siguiente emisora: " if delta > 0 else "📻 Emisora anterior: ")
            + vecina.nombre
        )
        await self._send_track_card(update, aviso, item, nota=aviso)

    async def _hist_sonar(self, update, fila: dict, etiqueta: str) -> None:
        """Reproducir UNA entrada del historial del hub (el caminar de
        ⏭/⏮). El ancla NO cambia: segun la regla del usuario, el ancla
        sigue siendo la fila que ELIGIO del listado, no cada vecina que
        va entrando."""
        item = QueueItem.from_dict(fila)
        ok = await self._play_item(update, item)
        if ok:
            aviso = etiqueta + str(fila.get("title") or "")
            await self._send_track_card(update, aviso, item, nota=aviso)

    async def _hist_next_emisora(self, update) -> None:
        """⏭ sobre una emisora CON sesion del hub 🕘: hacia la MAS NUEVA
        (la 1 son indices menores).

        La regla del usuario (relevo 10/2026): el ancla es la fila que ELIGIO
        y DEBE conservar lo que sonaba. Prioridad de rotacion:
        1) GÉNERO (los tags de la fila, del API de radios) si la fila los
           trae (o se los pudo recuperar por stream);
        2) el caminar del historial queda para filas sin tags (y su salto
           al tope va por pais del ancla).
        """
        ancla = self._hist_ancla or {}
        if ancla.get("tipo") == "emisora":
            if await self._hist_radio_genero(update):
                return
        filas = self._historial(1)
        indice = self._hist_indice_actual(filas)
        if indice is None or indice in (0, -1):
            await self._hist_radio_pais(update)
            return
        await self._hist_sonar(update, filas[indice - 1], "📻 Siguiente emisora: ")

    async def _hist_prev_emisora(self, update) -> None:
        """⏮ sobre una emisora CON sesion del hub: hacia la MAS VIEJA
        (indices mayores). En la ultima del historial: aviso y nada."""
        filas = self._historial(1)
        indice = self._hist_indice_actual(filas)
        if indice is not None and (indice == -1 or indice + 1 < len(filas)):
            await self._hist_sonar(update, filas[indice + 1], "📻 Emisora anterior: ")
            return
        await self._reply(
            update, "Esa es la ultima del historial: no hay anterior."
        )

    async def _hist_radio_genero(self, update) -> bool:
        """La rotacion por GÉNERO con el ancla del hub.

        Usa los tags de la fila elegida (del API de radios, letra chica:
        'bible,cristiana,...'). Entra la emisora que sigue en el listado
        del genero, con la del actual descartada, y el listado pasa a ser
        el ancla de rotacion (reglas de /radio, wrap incluido). Devuelve
        False si la fila no trae tags con que trabajar: el camino del
        llamador cae al caminar por pais. Un listado vacio para los 3
        primeros tags del ancla y una caida del API SE AVISAN: True."""
        ancla = self._hist_ancla or {}
        tags = [
            t.strip()
            for t in str(ancla.get("tags", "") or "").split(",")
            if t.strip()
        ]
        if not tags:
            return False
        for tag in tags[:3]:
            try:
                emisoras = [
                    e
                    for e in await radio.buscar_genero(tag, _RADIO_BATCH)
                    if e.stream
                ]
            except radio.RadioError as exc:
                logger.warning("Historial-emisoras: el API no respondio: %s", exc)
                await self._reply(
                    update,
                    "📻 El servicio de radios no responde ahora: abri /radio mas tarde.",
                )
                return True
            if emisoras:
                await self._hist_saltar_listado(update, emisoras, f"género {tag}")
                return True
            # Tag sin emisoras hoy: prueba el siguiente del ancla.
        return False

    async def _hist_saltar_listado(self, update, emisoras: list, criterio: str) -> bool:
        """El salto a la siguiente de UN listado (genero o pais) con la
        mecanica de /radio: entra la emisora que sigue en ese listado (con
        la del actual descartada; si no sale en el listado, la primera
        distinta), y el listado PASA a ser el ancla de rotacion: de aca en
        mas, ⏭/⏮ y el auto-avance giran con las reglas de /radio (wrap
        incluido) y la sesion del hub muere. True si el paso se hizo."""
        actual_url = getattr(self.queue.current, "url", "")
        posicion = next(
            (i for i, e in enumerate(emisoras) if e.stream == actual_url), None
        )
        if posicion is not None and posicion + 1 < len(emisoras):
            elegida = emisoras[posicion + 1]
        else:
            # La emisora que suena no sale hoy en el listado (o es la ultima
            # de el): entra la primera DISTINTA de la que suena.
            elegida = next((e for e in emisoras if e.stream != actual_url), None)
            if elegida is None and posicion is not None:
                elegida = emisoras[posicion]
        if elegida is None:
            await self._reply(
                update, "El listado no trae otra emisora distinta."
            )
            return True
        item = self._radio_item_de(elegida)
        ok = await self._play_item(update, item)
        if not ok:
            return True
        # Fruto del ancla: el listado del criterio gobierna desde ahora.
        self._radio_ancla = emisoras
        self._radio_ancla_wrap = False
        self._hist_ancla = None
        aviso = "📻 Siguiente por " + criterio + ": " + elegida.nombre
        await self._send_track_card(update, aviso, item, nota=aviso)
        return True

    async def _hist_radio_pais(self, update) -> None:
        """El ⏭ llego al tope del historial de emisoras (o la fila del ancla
        no trae tags): la busqueda usa el PAIS del ancla (la fila elegida
        del hub guarda el pais en su channel) con la MISMA mecanica de
        /radio. El aviso dice EL PAIS usada: antes el salto callaba y el
        usuario no sabia de donde salio la emisora (relevo 10/2026)."""
        pais = str((self._hist_ancla or {}).get("pais") or "").strip()
        if not pais:
            self._hist_ancla = None
            await self._radio_sonar_vecina(update, +1)
            return
        try:
            emisoras = [
                e for e in await radio.buscar_pais(pais, _RADIO_BATCH) if e.stream
            ]
        except radio.RadioError as exc:
            logger.warning("Historial-emisoras: el API no respondio: %s", exc)
            self._hist_ancla = None
            await self._reply(
                update,
                "📻 El servicio de radios no responde ahora: abri /radio mas tarde.",
            )
            return
        if not emisoras:
            await self._reply(
                update,
                f"No hay emisoras de {pais} en el listado de radios.",
            )
            return
        await self._hist_saltar_listado(
            update, emisoras, "país " + radio.pais_visible(pais)
        )

    async def _radio_play(self, query, update, user_id, accion: str) -> None:
        """Elegir emisora (dj): suena ya con el stream DIRECTO (sin yt-dlp)
        y el listado se cierra. Si no arranca, el listado queda en pie."""
        if user_id is None or not self.roles.has_role(user_id, "dj"):
            await self._list_answer(
                query, "Solo el admin o un dj pueden elegir.", show_alert=True
            )
            return
        emisora = self._radio_emisora_de(accion)
        if emisora is None:
            await self._list_answer(query, "Esa emisora ya no está en la lista.")
            return
        async with self._control_lock:
            self._from_card = True
            try:
                item = QueueItem(
                    url=emisora.stream,
                    title=emisora.nombre,
                    thumbnail=emisora.logo,
                    channel=emisora.pais,
                    directo=True,
                )
                await self._list_answer(query)
                ok = await self._play_item(update, item)
                if ok:
                    # Ancla de rotacion: desde ACA ⏭/⏮ y el auto-avance
                    # giran dentro de ESTE listado. El current-item no-
                    # emisora (una cancion de /buscar) aborta el ancla.
                    # La sesion del hub 🕘 tambien termina: el pick de
                    # /radio es un ancla nueva y manda sobre la vieja.
                    self._hist_ancla = None
                    self._radio_ancla = [
                        e for e in self._radio_items if e.stream
                    ] or None
                    chat_fin = self._radio_chat_id
                    await self._close_radio_message()
                    if chat_fin is not None:
                        await self._dejar_card_al_final(chat_fin)
            finally:
                self._from_card = False

    async def _radio_favorita(self, query, user_id, accion: str) -> None:
        """Guarda o quita la emisora N de favoritas (dj): la lista se
        persiste con el estado comun y el listado se repinta al toque."""
        if user_id is None or not self.roles.has_role(user_id, "dj"):
            await self._list_answer(
                query, "Solo el admin o un dj pueden elegir.", show_alert=True
            )
            return
        emisora = self._radio_emisora_de(accion)
        if emisora is None:
            await self._list_answer(query, "Esa emisora ya no está en la lista.")
            return
        if self._radio_es_fav(emisora):
            self._radio_favs = [f for f in self._radio_favs if f.stream != emisora.stream]
            aviso = "Quitada de favoritas."
        else:
            self._radio_favs.append(emisora)
            aviso = "Guardada en favoritas."
        if self._radio_view == "fav":
            self._radio_items = list(self._radio_favs)
        self._persist_dirty()
        await self._radio_render()
        await self._list_answer(query, aviso)

    async def _dm_or_group(self, target_user_id: int, text: str) -> None:
        """Envia un mensaje privado a un usuario; si falla (nunca inicio un
        chat con el bot), cae al grupo permitido. Nunca rompe el flujo."""
        try:
            await self._app.bot.send_message(target_user_id, text)
            return
        except Exception:  # noqa: BLE001 - el DM es un intento, no una garantia
            logger.info(
                "No se pudo notificar por privado al usuario %s; cae al grupo.",
                target_user_id,
            )
        chat = self.config.allowed_chat_id
        if chat is None:
            return
        try:
            await self._enviar_al_chat(chat, text)
        except Exception as exc:  # noqa: BLE001 - no romper el flujo
            logger.warning("No se pudo notificar en el grupo: %s", exc)

    async def _notify_role_change(self, user_id: int, role: str) -> None:
        """Avisa a un usuario que su rol cambio (DM con fallback al grupo)."""
        name = self.roles.get_name(user_id) or str(user_id)
        if role == "dj":
            text = (
                f"🎧 ¡Listo {name}! Ahora eres DJ.\n"
                "Puedes usar: /buscar artista - canción · botones ▶ ⏸ ⏭ ⏹ · "
                "🔊 volumen · 📋 lista de la cola."
            )
        else:
            text = (
                f"👤 {name}, ya no eres DJ.\n"
                "Tu acceso queda de invitado; un admin puede darte el rol de\n"
                "nuevo desde el botón 👥 Usuarios de la tarjeta."
            )
        await self._dm_or_group(user_id, text)

    async def _reply(self, update, text: str, **kwargs):
        """Envia un mensaje de respuesta, manejando tanto CallbackQuery como Message.

        - Si update es None (auto-advance sin usuario): no responde.
        - Si update.callback_query: responde (answer) y edita el mensaje original.
        - Si update.message: responde normalmente (reply_text), pasando los kwargs.
        """
        if update is None:
            return
        if update.callback_query:
            query = update.callback_query
            if self._from_card:
                # Desde un boton de la tarjeta el mensaje ya va a ser
                # re-renderizado por el handler del control: el texto de
                # estado/error se muestra como toast (response) y el bot
                # no edita el mensaje. Los mensajes de fallo suben como
                # alerta visible (popup) para que el error no pase de largo.
                es_fallo = (
                    text.startswith("No se pudo")
                    or text.startswith("Problema")
                    or text.startswith("mpv")
                    or text.startswith("Error")
                )
                try:
                    await query.answer(text, show_alert=es_fallo)
                except Exception:
                    pass
                return
            try:
                await query.answer()
            except Exception:
                pass
            parse_mode = kwargs.get("parse_mode")
            if parse_mode:
                await query.edit_message_text(text, parse_mode=parse_mode)
            else:
                await query.edit_message_text(text)
        else:
            await update.message.reply_text(text, **kwargs)

    async def _notify_admin(self, text: str, store_log: bool = True) -> None:
        """Escribe un error/aviso en el log y lo manda al dueno por Telegram.
        Sirve como "log de errores" del admin: el propio bot le copia todo
        fallo real (resolucion, arranque de mpv, reproduccion, red) a su chat.
        No requiere update: se puede llamar desde auto-advance sin usuario.
        """
        logger.warning("ADMIN_LOG: %s", text)
        if self.config.owner_id is None or self._app is None:
            return
        try:
            await self._app.bot.send_message(self.config.owner_id, f"⚠️ {text}")
        except Exception as exc:  # noqa: BLE001 - reportar el fallo es best-effort
            logger.warning("ADMIN_LOG no pudo notificar: %s", exc)

    def _thumbnail_for(self, item: QueueItem) -> str:
        """URL de la miniatura del item; la deriva del link si no la trae."""
        if item.thumbnail:
            return item.thumbnail
        return thumbnail_from_url(item.url)

    # --- La tarjeta vive en card.CardManager ------------------------------
    # Estas 16 lineas son TODO el contrato entre el bot y la tarjeta. Los
    # handlers (y las 119 referencias en las pruebas) siguen hablando con
    # `self._render_card(...)`; quien sabe de verdad es card.py.
    #
    # Los tres atributos tambien delegan, en lectura y escritura, porque la
    # identidad del mensaje-vivo se persiste y se restaura a traves de ellos:
    # si el estado viviera dos veces, el snapshot guardaria una cosa y el
    # render usaria otra.

    @property
    def _card_chat_id(self) -> int | None:
        return self.card.chat_id

    @_card_chat_id.setter
    def _card_chat_id(self, valor: int | None) -> None:
        self.card.chat_id = valor

    @property
    def _card_message_id(self) -> int | None:
        return self.card.message_id

    @_card_message_id.setter
    def _card_message_id(self, valor: int | None) -> None:
        self.card.message_id = valor

    @property
    def _card_is_photo(self) -> bool:
        return self.card.is_photo

    @_card_is_photo.setter
    def _card_is_photo(self, valor: bool) -> None:
        self.card.is_photo = valor

    async def _card_exists_in(self, chat_id: int | None) -> bool:
        return await self.card.exists_in(chat_id)

    def _with_card_reposition(self, handler):
        return self.card.with_reposition(handler)

    async def _send_track_card(
        self, update, caption: str, item, nota: str | None = None
    ) -> None:
        await self.card.send_track_card(update, caption, item, nota=nota)

    def _truncate(self, text: str, limit: int = 80) -> str:
        return self.card.truncate(text, limit)

    def _track_status_text(self) -> str:
        return self.card.status_text()

    async def _render_pending(self, title: str, item=None) -> None:
        await self.card.render_pending(title, item)

    def _control_keyboard(self) -> InlineKeyboardMarkup:
        return self.card.control_keyboard()

    def _quality_keyboard(self) -> InlineKeyboardMarkup:
        return self.card.quality_keyboard()

    def _sleep_keyboard(self) -> InlineKeyboardMarkup:
        return self.card.sleep_keyboard()

    def _ajustes_keyboard(self, user_id: int | None) -> InlineKeyboardMarkup:
        """Menu de Ajustes filtrado por rol del que toco el boton."""
        return self.card.ajustes_keyboard(
            es_dj=user_id is not None and self.roles.has_role(user_id, "dj"),
            es_admin=user_id is not None and self.roles.has_role(user_id, "admin"),
        )

    async def _render_card(self, text: str, chat_id=None, item=None) -> None:
        await self.card.render_card(text, chat_id, item)

    async def _swap_card_keyboard(self, keyboard: InlineKeyboardMarkup) -> None:
        await self.card.swap_keyboard(keyboard)

    async def _send_card(self, chat_id: int) -> None:
        await self.card.send_card(chat_id)

    async def _show_card(self, chat_id: int) -> None:
        await self.card.show_card(chat_id)

    async def _remove_card(self) -> None:
        await self.card.remove()

    async def _reposition_card(self, chat_id: int) -> None:
        await self.card.reposition(chat_id)

    async def _dejar_card_al_final(self, chat_id: int) -> None:
        await self.card.dejar_al_final(chat_id)

    async def _enviar_al_chat(self, chat_id, text, **kwargs):
        return await self.card.enviar_al_chat(chat_id, text, **kwargs)


    @staticmethod
    def _normalizar(s: str) -> str:
        """Delegado a artist_match.normalizar (10/11)."""
        return artist_match.normalizar(s)

    def _marcar_conexion(self, online: bool, motivo: str = "") -> None:
        """Deja en data/conexion.json si hay conexion con Telegram, AL INSTANTE.

        El launcher (ventana e icono del reloj) lo lee para avisarte en tu PC en
        el momento en que se cae la red. Es el unico canal posible: sin internet
        el bot no puede escribirte por Telegram, porque para hablar con Telegram
        necesita internet.

        Se escribe a archivo porque es LOCAL: aunque no haya red, el archivo se
        escribe igual, que es justo el caso que importa.
        """
        try:
            from bot_process import data_dir

            path = data_dir() / "conexion.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "online": online,
                        "motivo": motivo,
                        "ts": time.time(),
                    },
                    f,
                    ensure_ascii=False,
                )
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001 - el aviso nunca debe tumbar el bot
            logger.warning("No se pudo marcar el estado de conexion: %s", exc)

    def _search_tanda(self) -> int:
        """Cuantos resultados se piden a YouTube en esta busqueda.

        Con artista se piden el doble, porque despues se filtran solo los que
        tienen ese canal y con 10 puede no entrar ninguno.
        """
        return _SEARCH_BATCH_CON_ARTISTA if self._search_artist else _SEARCH_BATCH

    def _clave_texto(self, valor: str) -> str:
        """Delegado a artist_match.clave_texto (10/11)."""
        return artist_match.clave_texto(valor)

    def _coincide_artista(self, artist: str, r: SearchResult) -> bool:
        """Delegado a artist_match.coincide_artista (10/11).

        SOLO el canal decide; el por que esta en artist_match.
        """
        return artist_match.coincide_artista(artist, r)

    def _filtrar_por_artista(
        self, resultados: list[SearchResult]
    ) -> tuple[list[SearchResult], str]:
        """Delegado a artist_match.filtrar_por_artista (10/11).

        El unico estado que necesita es el artista pedido de esta sesion.
        """
        return artist_match.filtrar_por_artista(self._search_artist, resultados)

    def help_for_role(self, role: str) -> str:
        """Devuelve el listado de comandos permitidos para un rol."""
        rank = {"user": 0, "dj": 1, "admin": 2}
        level = rank.get(role, 0)

        lines: list[str] = []

        if level >= 1:  # dj o superior
            lines.append("• /buscar — buscar y reproducir un artista o link")

        if level >= 2:  # admin
            lines.append("• 👥 Usuarios — gestionar roles (la lista llega a su chat privado)")
            lines.append("• /reglas — re-fijar el mensaje de reglas del grupo")

        return "\n".join(lines)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        chat_id = chat.id if chat else "?"
        chat_title = chat.title if chat and chat.title else "(chat privado)"
        user = update.effective_user
        user_id = user.id if user else "?"
        user_id_int = user.id if user else None
        self._register_known_user(user)
        user_role = self.roles.get_role(user_id_int) if user_id_int is not None else "user"

        # Imprime el ID del chat en la consola para que el usuario pueda copiarlo.
        logger.info(
            "Mensaje /start | chat_id=%s | chat_title=%s | usuario_id=%s | rol=%s",
            chat_id,
            chat_title,
            user_id,
            user_role,
        )
        print(f"[YT-Remote] /start recibido | chat_id={chat_id} | chat={chat_title} | usuario={user_id} | rol={user_role}")

        # Primera configuracion: el dueno configura el grupo permitido.
        user = update.effective_user
        is_owner = user is not None and user.id == self.config.owner_id
        if is_owner and self.config.allowed_chat_id is None:
            allowed = chat.id if chat else None
            if allowed is not None:
                set_allowed_chat_id(allowed)
                self.config.allowed_chat_id = allowed
                logger.info("Grupo permitido configurado: %s", allowed)
                await self._reply(update, "Configurado: este chat quedo habilitado para el bot.\n\n" + "Comandos disponibles para su rol (admin):\n" + self.help_for_role("admin"))
                return

        if not self._chat_allowed(update):
            await self._reply(update, "Este bot no esta habilitado en este chat.")
            return

        if user_role == "user":
            # Regla del usuario: sin rol NO hay ni bienvenida ni nada, un
            # solo aviso; la unica puerta a dj es que un admin lo habilite
            # con el boton 👥 Usuarios de la tarjeta.
            await self._reply(update, "🔒 El bot es solo para dj/admin.")
            return
        await self._reply(
            update,
            "YT-Remote activo.\n\n"
            + f"Comandos disponibles para su rol ({user_role}):\n"
            + self.help_for_role(user_role),
        )

    async def health_check(self) -> bool:
        """Verifica que el bot esté saludable (polling activo, token válido)."""
        try:
            # Verificar que el bot puede hacer una llamada simple a la API
            me = await self._app.bot.get_me()
            return me is not None and me.id == self.config.owner_id
        except Exception:
            return False

    async def cmd_play(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Busca y reproduce en un solo paso desde /buscar.

        /buscar <artista> - <cancion>: lo que va ANTES del primer guion es el
        ARTISTA (cantante, banda, grupo, canal). Queda fijado como ancla de la
        radio (_radio_artist) ANTES de buscar, asi /next y el auto-advance
        siguen de ese artista (el mesonero no espera nadа).
        - Sin texto: muestra el uso.
        - Sin guion: todo el texto ES el artista (se busca tal cual y la
          radio se ancla a eso).
        - Link de YouTube: reproduce directo (video o playlist/mix).
        """
        query = " ".join(context.args or []).strip()
        if not query:
            # El home de /buscar: artistas que YA sonaron (uso real del
            # historial) como chips de un toque, mas el recordatorio del
            # uso escrito. Sin datos guardados se queda el uso de siempre
            # (nada de chips inventados). Cero preguntas, cero tecleo.
            await self._buscar_home_abrir(update)
            return
        # Todo /buscar o /play nuevo reemplaza al intento anterior: la card
        # vuelve a poder reposicionarse (un listado nuevo re-setea el flag).
        self._search_list_pending = False
        # Usar el buscador ES cambiar de sesion: el ancla del hub 🕘 muere
        # (la regla del usuario: la radio se re-ancla con las elecciones del
        # buscador, no con la fila vieja del historial).
        self._hist_ancla = None
        if is_youtube_link(query):
            await self._play_link_or_playlist(update, context, query)
            return

        artist, _, song = query.partition("-")
        artist = artist.strip()
        song = song.strip()
        if not artist:
            artist = query
            song = ""
        # El artista es EXACTAMENTE lo que escribió el usuario, nunca se
        # deriva de títulos. Queda anotado para el LISTADO: el ancla de la
        # radio solo se mueve si el usuario elige un resultado y arranca, así
        # que una búsqueda que no va a ningún lado no cambia lo que suena.
        self._search_artist = artist
        search_query = f"{artist} {song}".strip()
        await self._run_search(update, context, search_query)

    async def _buscar_home_abrir(self, update) -> None:
        """La lista del home de /buscar: chips de artistas con uso REAL.

        Cada chip de un toque lanza la busqueda del artista (las mismas
        garantias que escribirlo: muere el ancla del hub y el listado
        nuevo reemplaza al anterior). Igual que el listado del radio: si
        ya habia uno abierto se borra y se manda uno fresco.
        """
        chat_id = update.effective_chat.id
        await self._buscar_home_cerrar()
        artistas = self._state.top_artistas_uso(6, directo=0)
        if not artistas:
            await self._reply(
                update,
                "Uso: /buscar <artista> - <cancion>.\n"
                "Ej: /buscar GP Band - Inexplicable\n"
                "Tambien puedes pegar un link de YouTube.",
            )
            return
        lineas = [
            "👥 Artistas que ya sonaron en esta sala:",
            "✍️ O escribe: /buscar artista - cancion",
        ]
        filas: list[list[InlineKeyboardButton]] = []
        for i, fila in enumerate(artistas):
            clave = str(fila["clave"])
            lineas.append(f"{i + 1}. {clave} ({fila['usos']})")
            filas.append(
                [
                    InlineKeyboardButton(
                        f"👥 {self._truncate(clave, 32)} ({fila['usos']})",
                        callback_data=f"bsm:art:{i}",
                    )
                ]
            )
        filas.append([InlineKeyboardButton("❌", callback_data="bsm:close")])
        try:
            msg = await self._app.bot.send_message(
                chat_id,
                "\n".join(lineas),
                reply_markup=InlineKeyboardMarkup(filas),
            )
        except Exception as exc:  # noqa: BLE001 - no romper el flujo
            logger.warning("No se pudo enviar el home de /buscar: %s", exc)
            return
        self._buscar_home_chat_id = chat_id
        self._buscar_home_message_id = msg.message_id

    async def _buscar_home_cerrar(self) -> None:
        """Borra el home abierto de /buscar (si lo hay) y limpia sus ids."""
        if self._buscar_home_chat_id is None or self._buscar_home_message_id is None:
            return
        try:
            await self._app.bot.delete_message(
                self._buscar_home_chat_id, self._buscar_home_message_id
            )
        except Exception:  # noqa: BLE001 - ya no esta o la API fallo: da igual
            pass
        self._buscar_home_chat_id = None
        self._buscar_home_message_id = None

    async def _on_buscar_home_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Botones del home de /buscar (callback `bsm:`).

        - `bsm:art:N` (dj): lanza la busqueda del artista N del historial
          con EXACTAMENTE las garantias de escribir `/buscar artista`.
        - `bsm:close`: borra el home.
        Regla del usuario: sin rol nada de nada (mismo candado de /buscar).
        """
        query = update.callback_query
        data = query.data or ""
        accion = data.split(":", 1)[1] if data.startswith("bsm:") else ""
        user_id = query.from_user.id if query.from_user else None
        if not (user_id is not None and self.roles.has_role(user_id, "dj")):
            await self._list_answer(
                query, "🔒 /buscar es solo para dj/admin.", show_alert=True
            )
            return
        if accion in ("", "noop"):
            await self._list_answer(query)
            return
        if accion == "close":
            await self._list_answer(query)
            await self._buscar_home_cerrar()
            return
        if accion.startswith("art:"):
            crudo = accion.split(":", 1)[1]
            try:
                posicion = int(crudo)
            except ValueError:
                await self._list_answer(query)
                return
            artistas = self._state.top_artistas_uso(6, directo=0)
            if not (0 <= posicion < len(artistas)):
                # Reordenado por uso entre render y toque: expiro, se cierra.
                await self._list_answer(query, "Esa lista expiro, abre /buscar otra vez.")
                await self._buscar_home_cerrar()
                return
            artista = str(artistas[posicion]["clave"])
            await self._list_answer(query)
            await self._buscar_home_cerrar()
            # Las MISMAS garantias que /buscar con texto (cmd_play):
            # un listado nuevo reemplaza al anterior y el ancla del hub
            # muere con el cambio de sesion.
            self._search_list_pending = False
            self._hist_ancla = None
            self._search_artist = artista
            await self._run_search(update, context, artista)
            return
        await self._list_answer(query)

    async def _play_link_or_playlist(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
    ) -> None:
        """Reproduce un link de YouTube (video directo o playlist/mix).

        El estado de antes (lista, historial, ancla de la radio) se guarda al
        entrar y se restaura si el tema NO arranca: pegar un link que no se
        reproduce no puede dejar al usuario sin lo que estaba escuchando. En
        una playlist, la radio se ancla conservadoramente al canal del primer
        track (quien sube suele ser el artista o su sello); nunca se inventa
        un artista a partir del titulo.
        """
        snap = self._snapshot_reproduccion()
        self._radio_artist = ""
        self._clear_nav_stacks()
        if is_playlist_url(query):
            chat_id = update.effective_chat.id if update.effective_chat else None
            # Feedback en la tarjeta si existe: no se envia un mensaje que
            # quede clavado en el chat. Si no hay tarjeta, un mensaje efimero
            # que se borra al terminar (exito o fallo) para no ensuciar.
            pending_id = None
            if await self._card_exists_in(chat_id):
                await self._render_card("⏳ Arrancando playlist…")
            else:
                try:
                    pending = await context.bot.send_message(
                        chat_id, "⏳ Arrancando playlist, un momento..."
                    )
                    pending_id = pending.message_id
                except Exception:
                    pending_id = None
            try:
                try:
                    first_tracks, total = await asyncio.wait_for(
                        asyncio.to_thread(quick_playlist, query, _QUICK_TRACKS),
                        timeout=_QUICK_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Arranque de playlist agotado (%ss): %s", _QUICK_TIMEOUT, query
                    )
                    first_tracks, total = [], 0
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error arrancando playlist: %s", exc)
                first_tracks, total = [], 0
            if not first_tracks:
                # Sin arranque rapido no hay playlist: no molestar al usuario
                # con datos parciales, se avisa el fallo como antes.
                if pending_id is not None:
                    try:
                        await context.bot.delete_message(chat_id, pending_id)
                    except Exception:
                        pass
                await self._reply(update, "No se pudo expandir esa lista.")
                self._restore_reproduccion(snap)
                if await self._card_exists_in(chat_id):
                    await self._render_card(self._track_status_text())
                return
            if pending_id is not None:
                try:
                    await context.bot.delete_message(chat_id, pending_id)
                except Exception:
                    pass
            items = [
                QueueItem(
                    url=t.url,
                    title=t.title,
                    duration_seconds=t.duration_seconds,
                    thumbnail=t.thumbnail,
                    channel=t.channel,
                )
                for t in first_tracks
            ]
            self.queue.set_playlist(items)
            self._persist_dirty()
            first = items[0]
            self._radio_artist = first.channel or ""
            # El resto de la lista (hasta el techo tecnico) se trae en segundo
            # plano: el usuario ya escucha el primer track mientras carga.
            self._start_playlist_expansion(query, total)
            started = await self._play_item(update, first, preserve_current=True)
            if not started:
                # El primer tema no arranca: se corta la expansion en segundo
                # plano (anexaria temas a la lista restaurada) y se devuelve
                # todo al estado anterior.
                self._cancel_playlist_expansion()
                self._restore_reproduccion(snap)
                return
            await self._reply(
                update,
                f"▶️ Playlist ({len(items)}/{total or '…'}): {first.title}\n"
                f"Tocando YA; el resto se carga en segundo plano (boton 📋 para verla).",
            )
            return

        await context.bot.send_message(
            update.effective_chat.id, "Reproduciendo link directo..."
        )
        item = QueueItem(url=query, title=query, thumbnail=thumbnail_from_url(query))
        started = await self._play_item(update, item)
        if not started:
            # Link directo que no arranca: la lista, el historial y el ancla
            # de la radio quedan como estaban, y la cancion anterior sigue
            # sonando con su tarjeta.
            self._restore_reproduccion(snap)
            return

    def _start_playlist_expansion(self, url: str, total: int) -> None:
        """Lanza la expansion de fondo de una playlist.

        Si ya hay una expansion activa de OTRA playlist, se cancela: el
        anexo de la lista vieja al final de la nueva ensuciaria la cola.
        """
        if self._expanding_playlist and self._expanding_playlist_url != url:
            self._cancel_playlist_expansion()
        self._expanding_playlist_url = url
        self._expanding_total = total
        if self._expanding_playlist:
            return
        self._expanding_playlist = True
        self._expand_task = asyncio.create_task(self._expand_playlist_background(url))

    def _cancel_playlist_expansion(self) -> None:
        """Cancela la expansion en curso (si existe): se usa al arrancar
        otra playlist para que la lista vieja no se anexe a la nueva."""
        if self._expand_task is not None:
            try:
                self._expand_task.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._expand_task = None
        self._expanding_playlist = False
        self._expanding_playlist_url = None
        self._expanding_total = 0

    def _playlist_asegurar_ventana(self) -> bool:
        """Pide la siguiente tanda de la playlist si la cola se esta quedando
        sin camino por delante.

        Es lo que garantiza que el avance automatico nunca se quede sin tema
        siguiente: cuando el cursor llega al final de lo cargado, se pide una
        ventana mas y se espera a que llegue. Si ya hay una tanda en camino no
        se pide otra (no se solapan).

        Devuelve True si hay una ventana en camino (recien pedida o ya en
        vuelo) y False si no queda nada que cargar: en ese caso no hay que
        esperar a nada.
        """
        url = self._expanding_playlist_url
        if not url or not self.queue.has_playlist or not self.queue._items:
            return False
        if self._expand_task is not None and not self._expand_task.done():
            return True
        cargados = len(self.queue._items)
        if self._expanding_total and cargados >= self._expanding_total:
            return False
        self._expanding_playlist = True
        self._expand_task = asyncio.create_task(self._expand_playlist_background(url))
        return True

    async def _expand_playlist_background(self, url: str) -> None:
        """Trae UNA VENTANA de la playlist por detras y la anexa a la cola.

        Corre como tarea aparte (asyncio) tras el arranque al toque: no
        congela los botones ni la resolucion de streams.

        Antes traia la lista COMPLETA (hasta _MAX_PLAYLIST): con una lista de
        1.882 temas eso era descargar 1.882 metadatos y guardarlos en memoria
        para mostrar 10 por pagina. Ahora pide solo los _PLAYLIST_WINDOW
        temas siguientes, y las siguientes tandas se piden cuando la
        reproduccion se acerca al final de lo cargado (ver
        _playlist_asegurar_ventana).
        """
        inicio = len(self.queue._items)
        try:
            extra = await asyncio.wait_for(
                asyncio.to_thread(expand_playlist, url, _PLAYLIST_WINDOW, inicio + 1),
                timeout=_EXPAND_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Expansion de playlist agotada (%ss): %s", _EXPAND_TIMEOUT, url
            )
            extra = []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error expandiendo playlist en segundo plano: %s", exc)
            extra = []
        finally:
            self._expand_task = None

        # Al terminar, la expansion SOLO se deja ver si sigue siendo la misma
        # (misma URL): si el usuario arranco otra playlist (o la cancelo / no
        # trajo nada), la tarea vieja es de datos caducos y no debe tocar la
        # cola nueva. Sea el camino que sea, siempre se cierra el flag de
        # "cargando"; la URL y el total se conservan porque las proximas
        # ventanas los necesitan.
        vigente = self._expanding_playlist and self._expanding_playlist_url == url
        self._expanding_playlist = False
        if not vigente:
            return
        if not extra:
            return

        # Anexar SOLO lo que no tenemos ya (el quick-load cargo las primeras
        # N y la expansion completa vuelve a incluir las mismas, no duplicar).
        existentes: set[str] = {i.url for i in self.queue._items}
        nuevos = []
        for t in extra:
            if t.url in existentes:
                continue
            nuevos.append(
                QueueItem(
                    url=t.url,
                    title=t.title,
                    duration_seconds=t.duration_seconds,
                    thumbnail=t.thumbnail,
                    channel=t.channel,
                )
            )
        if not self.queue.has_playlist:
            # El usuario ya cambio de cola mientras expandiamos: descartar
            # sin tocar la nueva (nunca pisar una lista que el usuario eligio
            # despues).
            self._expanding_playlist = False
            self._expanding_playlist_url = None
            self._expanding_total = 0
            return
        for item in nuevos:
            self.queue.add(item)
        if nuevos:
            self._persist_dirty()
            logger.info(
                "Playlist expandida en segundo plano: +%d temas (total %d)",
                len(nuevos), len(self.queue._items),
            )
            # Aviso efimero en la card (si existe): la playlist quedo completa
            # y el 📋 ya muestra todos los temas.
            if self._card_chat_id is not None:
                total_actual = len(self.queue._items)
                try:
                    await self._render_card(
                        f"✅ Playlist cargada: {total_actual} temas.",
                        self._card_chat_id,
                    )
                except Exception:  # noqa: BLE001 - puramente informativo
                    pass
        self._expanding_playlist = False
        self._expanding_playlist_url = None
        self._expanding_total = 0

    async def _run_search(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
    ) -> None:
        """Busca en YouTube y muestra el listado de resultados con botones.

        Envia un mensaje "Buscando..." inmediato (asi el usuario no piensa
        que su comando se perdio) y lo EDITA con los resultados en lugar de
        mandar un segundo mensaje: queda un solo mensaje visible en el chat.
        """
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None:
            await self._reply(update, "Buscando...")
            return
        # Mensaje inmediato: el usuario ve que el bot recibio el comando.
        try:
            pending = await context.bot.send_message(
                chat_id, "🔎 Buscando...", parse_mode=ParseMode.HTML
            )
            pending_id = pending.message_id
        except Exception:
            pending_id = None

        # yt-dlp es lento y bloqueante: ejecutarlo en un thread para no
        # congelar el bot mientras busca, con tope de tiempo para que el
        # "Buscando..." nunca quede eterno.
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(search, query, self._search_tanda()),
                timeout=_SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            msg = f"La busqueda se agoto (mas de {_SEARCH_TIMEOUT:.0f}s). Intenta de nuevo."
            logger.warning("Busqueda agotada (%ss): %s", _SEARCH_TIMEOUT, query)
            if pending_id is not None:
                try:
                    await context.bot.edit_message_text(
                        msg, chat_id=chat_id, message_id=pending_id
                    )
                except Exception:
                    pass
            else:
                await self._reply(update, msg)
            return
        except Exception as exc:
            if pending_id is not None:
                try:
                    await context.bot.edit_message_text(
                        f"❌ Error al buscar: {exc}",
                        chat_id=chat_id,
                        message_id=pending_id,
                    )
                except Exception:
                    pass
            else:
                await self._reply(update, f"❌ Error al buscar: {exc}")
            return

        if not results:
            causa = last_search_error()
            if causa:
                msg = (
                    "❌ Fallo al buscar (YouTube bloqueo o respondio mal):\n"
                    f"{causa}"
                )
                logger.warning("Busqueda sin resultados por fallo (%s): %s", causa, query)
            else:
                msg = "No encontre resultados."
            if pending_id is not None:
                try:
                    await context.bot.edit_message_text(
                        msg, chat_id=chat_id, message_id=pending_id
                    )
                except Exception:
                    pass
            else:
                await self._reply(update, msg)
            return

        # Nueva busqueda: arranca un listado nuevo. El cache de streams se
        # vacia (los resultados viejos ya no son los que se van a elegir) y la
        # lista de resultados se reemplaza por esta tanda.
        self._clear_stream_cache()
        results, nota = self._filtrar_por_artista(results)
        self._search_nota = nota
        self._search_resultados = list(results)
        self._search_consulta = query
        self._search_pagina = 0
        self._search_sin_mas = False
        # Mientras el listado esta en pantalla la card NO se reposiciona: la
        # proxima edicion del wrapper se salta para dejar la card arriba, al
        # alcance del ojo, con los resultados debajo. Se limpia en el pick.
        self._search_list_pending = True
        self._search_chat_id = chat_id
        self._search_message_id = None
        await self._search_pintar(context.bot, chat_id, pending_id)

    def _search_paginas(self) -> int:
        """Cuantas paginas tiene el listado de resultados."""
        total = len(self._search_resultados)
        return max(1, (total + _SEARCH_PAGE_SIZE - 1) // _SEARCH_PAGE_SIZE)

    def _search_visibles(self) -> list[SearchResult]:
        """Los resultados de la pagina a la vista."""
        inicio = self._search_pagina * _SEARCH_PAGE_SIZE
        return self._search_resultados[inicio : inicio + _SEARCH_PAGE_SIZE]

    def _search_teclado(self) -> InlineKeyboardMarkup:
        """Teclado del listado: un boton por cancion con su nombre y su duracion,
        y al pie la fila de navegacion [◀ ❌ ▶], igual que la lista de canciones.

        En un boton de Telegram NADA salta de linea: lo que no cabe se recorta y
        el usuario no lo ve. Por eso el boton lleva solo lo imprescindible (el
        canal ya viene dentro del titulo del video) y no lleva numeros: el
        contador de pagina va en el texto del mensaje, no aqui.

        En la ultima pagina el boton derecho no se apaga: pasa a ser
        "🔎 Buscar más", que trae otra tanda de la misma consulta sin repetir
        lo ya visto. Asi nunca queda un boton muerto ni se pierde el hilo.
        """
        paginas = self._search_paginas()
        self._search_pagina = max(0, min(self._search_pagina, paginas - 1))
        self._search_cache.clear()
        inicio = self._search_pagina * _SEARCH_PAGE_SIZE

        filas: list[list[InlineKeyboardButton]] = []
        for i in range(inicio, min(inicio + _SEARCH_PAGE_SIZE, len(self._search_resultados))):
            r = self._search_resultados[i]
            self._search_cache[f"pick:{i}"] = r
            # Cada cancion lleva su nombre y su duracion. El canal NO se agrega:
            # YouTube ya lo trae dentro del propio titulo del video, y ponerlo
            # aqui era lo que desbordaba la linea y lo recortaba el celular.
            # "Si la informacion no se ve, no existe": nada de lo que se mande
            # aqui puede depender de que quepa en una sola linea del boton.
            etiqueta = r.title
            if r.duration and r.duration not in ("--:--", "0:00"):
                etiqueta = f"{etiqueta} {r.duration}"
            filas.append([InlineKeyboardButton(etiqueta, callback_data=f"pick:{i}")])

        previo = (
            InlineKeyboardButton("◀", callback_data="pick:prev")
            if self._search_pagina > 0
            else InlineKeyboardButton("·", callback_data="pick:noop")
        )
        if self._search_sin_mas:
            siguiente = InlineKeyboardButton("·", callback_data="pick:noop")
        elif self._search_pagina < paginas - 1:
            siguiente = InlineKeyboardButton("▶", callback_data="pick:next")
        else:
            siguiente = InlineKeyboardButton("🔎 Buscar más", callback_data="pick:mas")
        filas.append(
            [previo, InlineKeyboardButton("❌", callback_data="pick:cancel"), siguiente]
        )
        return InlineKeyboardMarkup(filas)

    def _search_limpiar(self) -> None:
        """Cierra el listado de /buscar: se vacian resultados, paginas, cache
        y la identidad del mensaje. Lo usa tanto elegir como cancelar."""
        self._search_list_pending = False
        self._search_artist = ""
        self._search_cache.clear()
        self._search_resultados = []
        self._search_consulta = ""
        self._search_pagina = 0
        self._search_nota = ""
        self._search_chat_id = None
        self._search_message_id = None
        self._search_sin_mas = False

    async def _search_aviso(self, texto: str) -> None:
        """Escribe un aviso en el listado de /buscar, en el mismo mensaje.

        El listado es siempre de texto (nunca foto), asi que el aviso va en el
        texto. Nunca se manda un mensaje nuevo: el listado se queda donde esta,
        con sus botones, para que el usuario elija otra opcion.
        """
        chat_id = self._search_chat_id
        msg_id = self._search_message_id
        if chat_id is None or msg_id is None:
            return
        teclado = self._search_teclado()
        try:
            await self._app.bot.edit_message_text(
                texto, chat_id=chat_id, message_id=msg_id, reply_markup=teclado
            )
        except Exception as exc:  # noqa: BLE001 - el aviso es informativo
            logger.warning("No se pudo escribir el aviso en el listado: %s", exc)

    async def _search_pintar(
        self, bot, chat_id: int, pendiente_id: int | None = None
    ) -> None:
        """Muestra el listado de /buscar en UN mensaje de texto.

        El texto es solo "Página N" y cada cancion es un boton con su nombre y
        su duracion. SIN miniatura: la foto era la del primer resultado, o sea
        la de la cancion que YouTube rankeo primero, que no era la que el
        usuario pidio y no representaba nada del conjunto. Se pidio una
        miniatura por cada resultado y Telegram no la puede poner en un boton;
        como no se puede, se quita la que habia.
        """
        teclado = self._search_teclado()
        visibles = self._search_visibles()
        if not visibles:
            return
        pie = f"Página {self._search_pagina + 1}"
        if self._search_nota:
            # Solo cuando el filtro del artista hizo algo: si no, el
            # encabezado se queda en "Página N" y corto.
            pie = f"{pie} · {self._search_nota}"

        # 1) Si el listado ya existe, se actualiza en sitio.
        if self._search_message_id is not None:
            try:
                await bot.edit_message_text(
                    pie, chat_id=chat_id, message_id=self._search_message_id, reply_markup=teclado
                )
                self._anticipate_urls([r.url for r in visibles])
                return
            except Exception as exc:  # noqa: BLE001 - se rehace el mensaje
                logger.warning("No se pudo actualizar el listado: %s", exc)

        # 2) Si hay un "Buscando..." se edita ese mismo: queda un solo
        #    mensaje en el chat.
        if pendiente_id is not None:
            try:
                await bot.edit_message_text(
                    pie, chat_id=chat_id, message_id=pendiente_id, reply_markup=teclado
                )
                self._search_message_id = pendiente_id
                self._anticipate_urls([r.url for r in visibles])
                return
            except Exception:  # noqa: BLE001 - se manda el listado aparte
                pass

        # 3) Se limpia lo que hubiera y se manda el listado.
        for viejo in (self._search_message_id, pendiente_id):
            if viejo is not None:
                try:
                    await bot.delete_message(chat_id, viejo)
                except Exception:  # noqa: BLE001 - ya no importa
                    pass
        nuevo = await bot.send_message(chat_id, pie, reply_markup=teclado)
        self._search_message_id = nuevo.message_id
        self._anticipate_urls([r.url for r in visibles])

    async def _search_ampliar(self) -> None:
        """🔎 Buscar más: otra tanda de la MISMA consulta, sin repetir URLs.

        Pide los ya vistos mas los nuevos de una (es la misma busqueda con mas
        resultados, no una busqueda distinta) y se queda con los que faltaban.
        """
        chat_id = self._search_chat_id
        if chat_id is None or not self._search_consulta:
            return
        vistos = {r.url for r in self._search_resultados}
        try:
            más = await asyncio.wait_for(
                asyncio.to_thread(
                    search, self._search_consulta, len(self._search_resultados) + _SEARCH_BATCH
                ),
                timeout=_SEARCH_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - avisar y seguir
            logger.warning("No se pudo ampliar la busqueda: %s", exc)
            más = []
        nuevos = [r for r in más if r.url not in vistos][:_SEARCH_BATCH]
        if nuevos:
            # La tanda nueva tambien pasa por el filtro del artista: si se
            # pidio un artista concreto, "Buscar mas" tiene que traer mas de ESE,
            # no mas resultados que vuelven a ser de otros.
            nuevos, nota = self._filtrar_por_artista(nuevos)
            self._search_nota = nota
        if not nuevos:
            self._search_sin_mas = True
            await self._search_pintar(self._app.bot, chat_id)
            return
        antes = len(self._search_resultados)
        self._search_resultados.extend(nuevos)
        self._search_pagina = antes // _SEARCH_PAGE_SIZE
        await self._search_pintar(self._app.bot, chat_id)

    async def _on_search_nav(self, query) -> None:
        """Botones de navegacion del listado de /buscar (◀, ▶, 🔎 Buscar más)."""
        await query.answer()
        if query.data == "pick:prev":
            self._search_pagina = max(0, self._search_pagina - 1)
        elif query.data == "pick:next":
            self._search_pagina = min(self._search_paginas() - 1, self._search_pagina + 1)
        elif query.data == "pick:mas":
            await self._search_ampliar()
            return
        else:
            return
        if self._search_chat_id is not None:
            await self._search_pintar(self._app.bot, self._search_chat_id)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        self._register_known_user(query.from_user)
        if query.data and query.data.startswith("ctl:"):
            # Un boton de la tarjeta persistente del mini reproductor.
            await self._on_control(update, context, query.data.split(":", 1)[1])
            return
        if query.data and query.data.startswith("cl:calidad:"):
            # Selector de calidad de la tarjeta (cl:calidad:N / cl:calidad:cerrar).
            await self._on_quality_callback(update, context)
            return
        if query.data and query.data.startswith("ap:"):
            # Menu del apagado programado (ap:N / ap:off / ap:cerrar / ap:noop).
            await self._on_sleep_callback(update, context)
            return
        if query.data and query.data.startswith("aj:"):
            # Menu de Ajustes de la tarjeta (por ahora solo aj:cerrar).
            await self._on_ajustes_callback(update, context)
            return
        if query.data and query.data.startswith("lst:"):
            # Botones del mensaje de la lista (temas, paginacion, cerrar).
            await self._on_list_callback(update, context)
            return
        if query.data and query.data.startswith("rad:"):
            # Botones del mensaje-lista de /radio (emisoras radio-browser).
            await self._on_radio_callback(update, context)
            return
        if query.data and query.data.startswith("bsm:"):
            # Botones del home de /buscar (artistas del historial).
            await self._on_buscar_home_callback(update, context)
            return
        # Regla del usuario: sin rol nada de nada (el listado del buscador
        # es de dj+ por completo: navegar, cancelar o elegir).
        if query.data and query.data.startswith("pick:"):
            pid = query.from_user.id if query.from_user else None
            if not (pid is not None and self.roles.has_role(pid, "dj")):
                try:
                    await query.answer(
                        "🔒 El bot es solo para dj/admin.", show_alert=True
                    )
                except Exception:  # noqa: BLE001
                    pass
                return
        if query.data in ("pick:prev", "pick:next", "pick:mas", "pick:noop"):
            await self._on_search_nav(query)
            return
        if query.data == "pick:cancel":
            # Cancelar el listado de /buscar: se borra (con su desvanecimiento)
            # sin reproducir nada y sin mover la card. El listado queda limpio
            # y el cache se descarta; la card que estaba arriba queda visible.
            self._search_limpiar()
            await query.answer("Busqueda cancelada.")
            try:
                await query.message.delete()
            except Exception as exc:  # noqa: BLE001 - no romper el flujo
                logger.warning("No se pudo borrar el listado al cancelar: %s", exc)
            return
        await query.answer()
        if not query.data or not query.data.startswith("pick:"):
            return
        result = self._search_cache.get(query.data)
        if result is None:
            await query.edit_message_text("Esa busqueda ya expiro, busca de nuevo.")
            return

        # El usuario eligio un resultado. NADA se destruye todavia: primero se
        # resuelve el stream. Si no arranca, la lista de antes, el historial, el
        # artista de la radio y la tarjeta quedan intactos, y el listado de
        # resultados sigue en pantalla para que elija otra opcion.
        artist = self._search_artist
        if artist and result.channel and not self._coincide_artista(artist, result):
            # Es una cancion de OTRO artista (no habia coincidencias y se le
            # mostró lo más parecido). La radio tiene que seguir a lo que de
            # verdad esta sonando: si no, estas escuchando a Mafe Restrepo y el
            # /next va a buscar musica de GP Band.
            artist = result.channel
        item = QueueItem(
            url=result.url,
            title=result.title,
            duration_seconds=result.duration_seconds,
            thumbnail=result.thumbnail,
            channel=result.channel,
            artist=artist,
        )
        if await self._stream_for(item.url) is None:
            # No arranca: no se toca lista, historial, ancla ni tarjeta. El
            # motivo se escribe EN EL LISTADO (que sigue en pantalla) para que
            # elija otra opcion, y el admin recibe el detalle por privado.
            motivo = last_resolve_error()
            if motivo and "resuelto con client" in motivo:
                motivo = "todas las variantes de yt-dlp fallaron"
            aviso = f"❌ «{item.title}» no se pudo reproducir."
            if motivo:
                aviso += f"\nMotivo: {motivo}"
            await self._search_aviso(aviso)
            await self._notify_admin(
                f"Fallo de resolucion: {item.title}\n{motivo or 'sin motivo detallado'}"
            )
            return

        # Resuelto: ahora si, la intencion es nueva de verdad.
        snap = self._snapshot_reproduccion()
        self._search_list_pending = False
        # La eleccion del RESULTADO es un ancla nueva de la radio: la sesion
        # del hub 🕘 muere aca (la semilla se re-ancla abajo con su artista).
        self._hist_ancla = None

        # La tarjeta persistente (mini reproductor con miniatura + estado +
        # botones) ES la unica confirmacion; _play_item la crea al reproducir.
        # Se quita el listado de resultados para no dejar el titulo duplicado.
        try:
            await query.message.delete()
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo borrar el listado de resultados: %s", exc)
            try:
                await query.edit_message_text("▶️ Listo, reproduciendo...")
            except Exception as exc2:  # noqa: BLE001
                logger.warning("No se pudo editar mensaje tras fallo de borrado: %s", exc2)
        self._search_limpiar()

        # Tarjeta fresca al final de la conversacion: se borra la previa para
        # que _play_item cree la nueva al final del chat.
        await self._remove_card()
        self._clear_nav_stacks()
        self._radio_artist = artist

        started = await self._play_item(update, item)
        if not started:
            # El stream estaba resuelto pero mpv fallo al cargar. Se deshace
            # todo lo demas; la tarjeta no se puede recuperar (su mensaje ya
            # estaba borrado), asi que se rehace con la cancion que sigue
            # sonando para que el usuario no se quede sin controles.
            self._restore_reproduccion(snap)
            self._card_chat_id = None
            self._card_message_id = None
            self._card_is_photo = False
            chat = getattr(query.message, "chat", None)
            if chat is not None:
                await self._send_card(chat.id)
            return

    # --- El motor de reproduccion vive en playback.PlaybackEngine --------
    # Mismo contrato que la tarjeta: estas 15 lineas delegan, el codigo esta
    # alla. Los nueve atributos tambien delegan en lectura y escritura porque
    # el motor es su unico dueño y la sesion los lee desde las pruebas: dos
    # Proprietarios para un estado es la forma garantizada de que se desincronicen.

    @property
    def _stream_cache(self):
        return self.playback.stream_cache

    @_stream_cache.setter
    def _stream_cache(self, valor):
        self.playback.stream_cache = valor

    @property
    def _resolving(self):
        return self.playback.resolving

    @_resolving.setter
    def _resolving(self, valor):
        self.playback.resolving = valor

    @property
    def _anticipate_queue(self):
        return self.playback.anticipate_queue

    @_anticipate_queue.setter
    def _anticipate_queue(self, valor):
        self.playback.anticipate_queue = valor

    @property
    def _anticipate_task(self):
        return self.playback.anticipate_task

    @_anticipate_task.setter
    def _anticipate_task(self, valor):
        self.playback.anticipate_task = valor

    @property
    def _queue_advance_needed(self) -> bool:
        return self.playback.queue_advance_needed

    @_queue_advance_needed.setter
    def _queue_advance_needed(self, valor: bool) -> None:
        self.playback.queue_advance_needed = valor

    @property
    def _prefetch_task(self):
        return self.playback.prefetch_task

    @_prefetch_task.setter
    def _prefetch_task(self, valor):
        self.playback.prefetch_task = valor

    @property
    def _prefetch_basis(self):
        return self.playback.prefetch_basis

    @_prefetch_basis.setter
    def _prefetch_basis(self, valor):
        self.playback.prefetch_basis = valor

    @property
    def _prefetch_candidate(self):
        return self.playback.prefetch_candidate

    @_prefetch_candidate.setter
    def _prefetch_candidate(self, valor):
        self.playback.prefetch_candidate = valor

    @property
    def _prefetch_resolved(self):
        return self.playback.prefetch_resolved

    @_prefetch_resolved.setter
    def _prefetch_resolved(self, valor):
        self.playback.prefetch_resolved = valor

    def _mpv_track_ended_callback(self) -> None:
        self.playback.track_ended_callback()

    def _check_queue_advance(self) -> bool:
        return self.playback.check_queue_advance()

    def _cancel_prefetch(self) -> None:
        self.playback.cancel_prefetch()

    async def _stream_for(self, url: str):
        return await self.playback.stream_for(url)

    async def _anticipate_worker(self) -> None:
        await self.playback.anticipate_worker()

    def _anticipate_urls(self, urls: list[str]) -> None:
        self.playback.anticipate_urls(urls)

    def _clear_stream_cache(self) -> None:
        self.playback.clear_stream_cache()

    def _artist_seed(self, item: QueueItem) -> str:
        return self.playback.artist_seed(item)

    async def _pick_next_candidate(self, current: QueueItem):
        return await self.playback.pick_next_candidate(current)

    def _radio_over_message(self, por_error: bool = False) -> str:
        return self.playback.radio_over_message(por_error=por_error)

    def _schedule_prefetch(self, current: QueueItem) -> None:
        self.playback.schedule_prefetch(current)

    async def _reproducir_directo_manual(
        self, update, item: QueueItem, origen: str
    ) -> bool:
        """Camino manual (/prev, /next con pila, recarga de calidad) para
        un item DIRECTO (emisora): su URL YA es el stream — sin yt-dlp
        (antes iba por _stream_for y le aplicaba resolver a un stream icy)
        — y la carga va por el helper comun del motor (logo + video-
        unscaled + la imagen que nunca decide). False si no arranco."""
        try:
            await self.player.start()
        except RuntimeError as exc:
            if update is not None:
                await self._reply(
                    update, f"Problema al iniciar el reproductor: {exc}"
                )
            await self._notify_admin(f"mpv no arranco en {origen}: {exc}")
            return False
        fallo = await self.playback.cargar_stream_del_item(item, item.url, None)
        if fallo is not None:
            if update is not None:
                await self._reply(update, f"No se pudo reproducir: {fallo}")
            await self._notify_admin(
                f"Fallo al reproducir {item.title} en {origen}: {fallo}"
            )
            return False
        self._paused = False
        return True

    async def _advance_after_end(self, context) -> None:
        await self.playback.advance_after_end(context)

    async def _check_queue_advance_job(self, context) -> None:
        await self.playback.check_queue_advance_job(context)

    async def _play_item(
        self,
        update: Update,
        item: QueueItem,
        *,
        preserve_current: bool = False,
    ) -> bool:
        return await self.playback.play_item(
            update, item, preserve_current=preserve_current
        )


    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.queue.current is None or not self.player.is_running:
            await self._reply(update, "No hay ninguna reproduccion activa.")
            return
        await self.player.pause()
        self._paused = True
        self._persist_dirty()
        await self._reply(update, "⏸️ Pausado")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.queue.current is None:
            await self._reply(update, "No hay ninguna reproduccion activa.")
            return
        # Si mpv no esta corriendo (tras apagado de la PC o tras un stop),
        # reproducir el item actual por _play_item: prende mpv y carga el stream.
        if not self.player.is_running:
            started = await self._play_item(update, self.queue.current)
            if started:
                await self._send_track_card(
                    update, f"▶️ Reanudando: {self.queue.current.title}", self.queue.current
                )
            return
        await self.player.play()
        self._paused = False
        self._persist_dirty()
        await self._reply(update, "▶️ Reanudando")

    async def _toggle_play_pause(self, update: Update | None = None) -> None:
        """Alterna reproducir/pausar (boton ▶/⏸ de la tarjeta).

        Un solo boton con las tres funciones de cualquier reproductor: sin
        mpv corriendo (primera vez, tras un ⏹ o tras el temporizador) prende
        mpv y carga el tema actual; si mpv corre, pausa o sigue. Ademas, si
        hay un instante guardado de ESE tema (lo deja el temporizador al
        cerrar todo), retoma ahi en vez de arrancar en 0; el ⏹ es el que
        borra ese instante (parar = el proximo ▶ empieza de cero).
        """
        if self.queue.current is None:
            return
        if not self.player.is_running:
            # Con playlist activa, reanudar NO debe descartar la cola: el item
            # actual ya esta posicionado (items[cursor]). Sin playlist (radio /
            # cancion suelta) sigue el camino de set_current de siempre.
            started = await self._play_item(
                update,
                self.queue.current,
                preserve_current=self.queue.has_playlist,
            )
            if started:
                await self._retomar_posicion_guardada()
            return
        if self._paused:
            await self.player.play()
            self._paused = False
        else:
            await self.player.pause()
            self._paused = True
        self._persist_dirty()

    async def _retomar_posicion_guardada(self) -> None:
        """Retoma el instante que dejo el temporizador al cerrar todo.

        Solo aplica si el guardado es del tema actual: si se eligio otro, se
        descarta en silencio (no se revive la posicion de un tema viejo). Se
        consume al usarlo: el proximo arranque en frio empieza en 0, como
        despues de un ⏹.
        """
        guardado = self._posicion_guardada
        if not guardado or not guardado.get("item"):
            return
        actual = self.queue.current
        url_guardada = (guardado["item"] or {}).get("url")
        if actual is None or actual.url != url_guardada:
            self._posicion_guardada = None
            self._persist_dirty()
            return
        pos = max(0, int(guardado.get("pos", 0)))
        self._posicion_guardada = None
        self._persist_dirty()
        if pos > 0:
            await self.player.seek_to(pos)

    async def _on_control(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        action: str,
    ) -> None:
        """Maneja los botones de la tarjeta persistente (callback ctl:*).

        Cada accion reusa la logica de los comandos equivalentes, pero con
        _from_card activo: no se mandan fotos de confirmacion y los mensajes
        de estado/error van como toast. Al final la tarjeta se re-renderiza
        en su lugar para reflejar siempre el estado actual.
        """
        query = update.callback_query
        user = query.from_user
        user_id = user.id if user else None
        role = self.roles.get_role(user_id) if user_id else "user"

        CONTROL_ACTIONS = {
            "pp", "prev", "next", "stop", "vol-10", "vol+10", "vol-info",
            "seek-10", "seek+10", "apagar", "ajustes", "lista",
        }
        # Regla del usuario: sin rol NADA de nada (incluido abrir el 📋):
        # un solo aviso para todo el bot.
        if action in CONTROL_ACTIONS and user_id is not None and not self.roles.has_role(user_id, "dj"):
            await query.answer("🔒 El bot es solo para dj/admin.", show_alert=True)
            return

        self._card_chat_id = update.effective_chat.id
        self._card_message_id = query.message.message_id
        chat_id = self._card_chat_id

        # Candado anti-colision: si una accion de boton ya esta en curso
        # (p.ej. resolviendo el stream de un salto), los toques extra que
        # llegan mientras tanto se DESCARTAN al instante y no se encolan.
        # Esa cola acumulada era la otra mitad del delay percibido: el bot
        # procesaba un salto lento tras otro. Un boton a la vez.
        if self._control_lock.locked():
            try:
                await query.answer("⏳ Un momento, primero termina...")
            except Exception:
                pass
            return

        async with self._control_lock:
            self._from_card = True
            reflect_status = True
            # Respuesta muda al toque: apaga el spinner del boton al instante
            # sin mostrar toast. Todo el feedback de carga vive en la tarjeta
            # (ver _render_pending); el texto aparece solo en los errores.
            try:
                await query.answer()
            except Exception:
                pass
            try:
                if action == "pp":
                    await self._toggle_play_pause(update)
                elif action == "prev":
                    await self.cmd_prev(update, context)
                elif action == "next":
                    await self.cmd_next(update, context)
                elif action == "stop":
                    await self.cmd_stop(update, context)
                elif action in ("seek-10", "seek+10"):
                    if not self.player.is_running:
                        # Sin mpv no hay posicion que mover: aviso y ya.
                        await query.answer("No hay ninguna cancion sonando.")
                        reflect_status = False
                        return
                    await self.player.seek(-10 if action == "seek-10" else 10)
                    reflect_status = False  # la tarjeta no cambia de estado
                elif action in ("vol-10", "vol+10"):
                    delta = -10 if action == "vol-10" else 10
                    self._volume = max(0, min(100, self._volume + delta))
                    if self.player.is_running:
                        await self.player.set_volume(self._volume)
                    self._persist_dirty()
                elif action == "vol-info":
                    # Boton de estado: el volumen ya se lee en su etiqueta.
                    await query.answer(f"Volumen: {self._volume}%")
                    reflect_status = False
                elif action == "lista":
                    # El boton 📋 abre la lista como MENSAJE aparte en el
                    # chat: la card NO se pisa (queda con su estado intacto).
                    if chat_id is None:
                        return
                    await self._open_queue_list(query, user_id, chat_id)
                    reflect_status = False  # la card no se re-renderiza
                elif action == "calidad":
                    # Solo admin puede abrir el selector de calidad.
                    if user_id is None or not self.roles.has_role(user_id, "admin"):
                        await query.answer("Solo el admin puede cambiar la calidad.", show_alert=True)
                        return
                    # Solo cambia el teclado (la grilla reemplaza a los
                    # botones de control); el texto/miniatura no se tocan.
                    await self._swap_card_keyboard(self._quality_keyboard())
                    reflect_status = False  # no pisa el selector abierto
                elif action == "usuarios":
                    # Solo admin: la lista de usuarios se envia a su chat
                    # PRIVADO (en el grupo no aparece nada, solo el toast).
                    if user_id is None or not self.roles.has_role(user_id, "admin"):
                        await query.answer("Solo el admin puede gestionar usuarios.", show_alert=True)
                        return
                    await self._open_members_list(query, user_id)
                    reflect_status = False
                elif action == "apagar":
                    # Menu del apagado programado: solo cambia el teclado
                    # (mismo patron que el selector de calidad); el estado
                    # de la tarjeta (titulo/pausa) no cambia.
                    await self._swap_card_keyboard(self._sleep_keyboard())
                    reflect_status = False
                elif action == "ajustes":
                    # Menu de Ajustes: solo cambia el teclado, con una
                    # opcion por fila y solo las que el rol puede abrir
                    # (dj ve el apagado; admin suma calidad y usuarios).
                    await self._swap_card_keyboard(
                        self._ajustes_keyboard(user_id)
                    )
                    reflect_status = False
                else:
                    return
            finally:
                self._from_card = False

            # Estado final: el texto de la tarjeta refleja el cambio de la
            # accion (nuevo tema, pausa, volumen, detenido, etc.).
            if reflect_status:
                await self._render_card(self._track_status_text(), chat_id)

    async def _on_quality_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Maneja el selector de calidad de la tarjeta (callback `cl:calidad:*`).

        Solo admin. Se serializa con el MISMO `_control_lock` que los botones de
        control: un toque de calidad jamas se pisa con un next/prev. Si otra
        accion corre, se descarta al instante.

        `cl:calidad:N` aplica el nivel (persistido en `state.json`, aplicado a
        `search.MAX_HEIGHT`), invalida el cache de streams/prefetch y, si hay una
        canción sonando, la recarga con la nueva resolución. `cl:calidad:cerrar`
        descarta sin guardar nada.

        Elegir el nivel ya activo equivale a cerrar: solo vuelve los botones de
        control, sin recargar ni invalidar el cache.

        Al aplicar (o cerrar, o repetir), la card NO se borra ni se recrea: se
        restaura el teclado de control sobre el MISMO mensaje (`_swap_card_keyboard`).
        """
        query = update.callback_query
        user_id = query.from_user.id if query.from_user else None
        if user_id is not None and not self.roles.has_role(user_id, "admin"):
            await query.answer("Solo el admin puede cambiar la calidad.", show_alert=True)
            return

        if self._control_lock.locked():
            try:
                await query.answer("⏳ Un momento, primero termina...")
            except Exception:
                pass
            return

        async with self._control_lock:
            self._from_card = True
            try:
                data = query.data or ""
                level_str = (
                    data.split(":", 2)[2] if data.startswith("cl:calidad:") else ""
                )
                self._card_chat_id = update.effective_chat.id
                self._card_message_id = query.message.message_id
                try:
                    await query.answer()
                except Exception:
                    pass

                if level_str == "cerrar":
                    await self._swap_card_keyboard(self._control_keyboard())
                    return

                try:
                    level = int(level_str)
                except ValueError:
                    return
                if level not in QUALITY_LEVELS:
                    return

                # Elegir el nivel VIGENTE equivale a cerrar: no recargar, no
                # invalidar cache, no tocar nada. Solo vuelve los botones.
                if level == (self._max_height or 1080):
                    await self._swap_card_keyboard(self._control_keyboard())
                    return

                self._max_height = level
                set_max_height(level)
                self._persist_dirty()
                try:
                    await query.answer(f"Calidad: {level}p ✓")
                except Exception:
                    pass

                # Cambio YA: invalidar el mesonero y el prefetch para que nada
                # siga resolviendo con la calidad vieja, y recargar el tema
                # actual con la nueva resolucion (si hay algo sonando).
                self._clear_stream_cache()
                self._cancel_prefetch()
                current = self.queue.current
                if current is not None:
                    try:
                        if current.directo:
                            # Emisora: su URL ya es el stream (sin yt-dlp) y
                            # la carga va por el helper comun (logo y
                            # video-unscaled intactos tras el salto).
                            fallo = await self.playback.cargar_stream_del_item(
                                current, current.url, None
                            )
                            if fallo is not None:
                                raise RuntimeError(fallo)
                            self._paused = False
                        else:
                            resolved = await self._stream_for(current.url)
                            if not resolved:
                                await self._notify_admin(
                                    f"No se pudo recargar '{current.title}' con calidad {level}p."
                                )
                            else:
                                stream_url, audio_url = resolved
                                await self.player.start()
                                await self.player.load(stream_url, audio_url)
                                await self.player.play()
                                self._paused = False
                    except Exception as exc:  # noqa: BLE001 - no romper la card
                        logger.warning("Fallo la recarga tras cambiar calidad: %s", exc)
                        await self._notify_admin(
                            f"No se pudo recargar '{current.title}' a {level}p: {exc}"
                        )

                # La card NO se borra ni se recrea: se restaura el MISMO mensaje
                # con el teclado de control estandar (ya mostrando el nuevo nivel
                # en el boton ⚙️), sin desvanecimiento ni efecto de recreacion.
                await self._swap_card_keyboard(self._control_keyboard())
            finally:
                self._from_card = False

    async def cmd_next(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Salta YA al siguiente tema: corta el actual y reproduce el siguiente.

        Modo playlist: avanza el cursor con wrap (bucle).
        Modo radio: si hay algo en la pila forward (porque se hizo /prev),
        reproduce ese item directamente. Si no, decide un candidato nuevo
        (prefetch ya resuelto -> cero silencio; o prefetch candidato; o
        busqueda nueva por semilla).
        """
        if self.queue.current is None:
            await self._reply(update, "No hay ninguna cancion reproduciendose.")
            return

        # ⏭ en modo emisora: primero la sesion del hub 🕘 (el historial de
        # emisoras); si no hay ancla, rota por el listado ancla de /radio.
        if self.queue.current.directo:
            if self._hist_ancla and self._hist_ancla.get("tipo") == "emisora":
                await self._hist_next_emisora(update)
            else:
                await self._radio_sonar_vecina(update, +1)
            return

        # Modo playlist: cursor con wrap (logica previa).
        if self.queue.has_playlist:
            self._push_to_nav_back()  # guardar el actual en la pila back
            if self._prefetch_resolved is not None:
                (candidate, from_queue), (stream_url, audio_url) = self._prefetch_resolved
                self._prefetch_resolved = None
                self._cancel_prefetch()
                try:
                    await self.player.start()
                except RuntimeError as exc:
                    await self._reply(update, f"No se pudo saltar: {exc}")
                    return
                fallo = await self.playback.load_with_retry(
                    candidate.url, stream_url, audio_url
                )
                if fallo is not None:
                    await self._reply(update, f"No se pudo saltar: {fallo}")
                    return
                self._paused = False
                self._registrar_historial(candidate)
                if from_queue:
                    self.queue.next()
                else:
                    self.queue.set_current(candidate)
                self._schedule_prefetch(candidate)
                await self._send_track_card(
                    update, f"⏭️ Siguiente: {candidate.title}", candidate
                )
                return

            current_item = self.queue.current
            candidate, from_queue, error = None, False, False
            if (
                self._prefetch_candidate is not None
                and self._prefetch_basis == current_item.url
            ):
                candidate, from_queue = self._prefetch_candidate
            else:
                candidate, from_queue, error = await self._pick_next_candidate(current_item)
            if candidate is None:
                await self._reply(update, self._radio_over_message(por_error=error))
                return
            if from_queue:
                self.queue.next()
            started = await self._play_item(update, candidate, preserve_current=from_queue)
            if not started:
                await self._reply(update, "No se pudo reproducir el siguiente tema.")
                return
            await self._send_track_card(
                update, f"⏭️ Siguiente: {candidate.title}", candidate
            )
            return

        # Sesion del hub 🕘 (canciones): caminar el historial hacia la MAS
        # NUEVA (la 1 son indices menores). En la primera posicion — o con
        # una cancion fresca del buscador: esa pasa a ser la 1 — cae al
        # flujo estandar de abajo, que ya busca con la semilla del ancla
        # (el artista de la fila elegida).
        filas = self._historial(0)
        posicion = self._hist_indice_actual(filas)
        if posicion is not None and posicion > 0:
            await self._hist_sonar(
                update, filas[posicion - 1], "⏭️ Siguiente: "
            )
            return

        # Modo radio (sin playlist).
        # 1) Si hay algo en el stack forward (por un /prev previo), reproducirlo.
        if self._nav_forward:
            item = self._nav_forward.pop()
            # Navegación pura: no alteramos los stacks de navegación más de
            # lo necesario. Seteamos el queue._current directo para que el
            # anti-ping-pong (history) no se accione aquí (solo al elegir
            # un tema nuevo desde búsqueda). El _note_played se omite aquí
            # intencionalmente: el flujo de navegación no entra al histórico
            # de "ya reproducidos" de la radio.
            self._cancel_prefetch()
            await self._render_pending(item.title, item)
            if item.directo:
                # Emisora que volvia por la pila: sin yt-dlp (su URL YA es
                # el stream) y con el logo/unscaled del helper comun.
                if not await self._reproducir_directo_manual(update, item, "/next (forward)"):
                    return
                self._registrar_historial(item)
                self.queue._current = item
                self.queue._items = []
                self.queue._cursor = 0
                if update is not None:
                    await self._show_card(update.effective_chat.id)
                await self._send_track_card(
                    update, f"⏭️ Siguiente: {item.title}", item
                )
                return
            resolved = await self._stream_for(item.url)
            if not resolved:
                await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
                await self._notify_admin(f"Fallo de resolucion en /next (forward): {item.title}")
                return
            stream_url, audio_url = resolved
            try:
                await self.player.start()
            except RuntimeError as exc:
                await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
                await self._notify_admin(f"mpv no arranco en /next: {exc}")
                return
            fallo = await self.playback.load_with_retry(
                item.url, stream_url, audio_url
            )
            if fallo is not None:
                await self._reply(update, f"No se pudo reproducir: {fallo}")
                await self._notify_admin(f"Fallo al reproducir {item.title}: {fallo}")
                return
            self._paused = False
            self._registrar_historial(item)
            self.queue._current = item
            self.queue._items = []
            self.queue._cursor = 0
            self._schedule_prefetch(item)
            if update is not None:
                await self._show_card(update.effective_chat.id)
            await self._send_track_card(
                update, f"⏭️ Siguiente: {item.title}", item
            )
            return

        # 2) Sin pila forward: comportamiento estandar (prefetch o candidato nuevo).
        self._push_to_nav_back()
        # Prefetch ya resuelto -> salto instantaneo (cero silencio).
        if self._prefetch_resolved is not None:
            (candidate, from_queue), (stream_url, audio_url) = self._prefetch_resolved
            self._prefetch_resolved = None
            self._cancel_prefetch()
            try:
                await self.player.start()
            except RuntimeError as exc:
                await self._reply(update, f"No se pudo saltar: {exc}")
                await self._notify_admin(f"Fallo al saltar en /next: {exc}")
                return
            fallo = await self.playback.load_with_retry(
                candidate.url, stream_url, audio_url
            )
            if fallo is not None:
                await self._reply(update, f"No se pudo saltar: {fallo}")
                await self._notify_admin(f"Fallo al saltar en /next: {fallo}")
                return
            self._paused = False
            self._registrar_historial(candidate)
            # En radio, from_queue siempre es False. Navegacion pura: seteamos
            # _current directo para no duplicar el push al stack (ya lo hicimos).
            self.queue._current = candidate
            self.queue._items = []
            self.queue._cursor = 0
            self._schedule_prefetch(candidate)
            await self._send_track_card(
                update, f"⏭️ Siguiente: {candidate.title}", candidate
            )
            return

        # Sin stream cacheado aun: usar el candidato que la anticipacion decidio,
        # o buscar uno nuevo por semilla.
        current_item = self.queue.current
        candidate, from_queue, error = None, False, False
        if (
            self._prefetch_candidate is not None
            and self._prefetch_basis == current_item.url
        ):
            candidate, from_queue = self._prefetch_candidate
        else:
            candidate, from_queue, error = await self._pick_next_candidate(current_item)
        if candidate is None:
            await self._reply(update, self._radio_over_message(por_error=error))
            return
        # Navegacion pura: seteamos _current directo para evitar el doble push
        # al stack de navegacion (ya se guardo el item en _push_to_nav_back).
        # El commit del estado se hace SOLO tras el load exitoso (abajo):
        # si resolver o reproducir falla, queue._current sigue apuntando al
        # tema anterior. La miniatura del candidato va por _render_pending
        # (item explicito) sin tocar el estado del dominio.
        await self._render_pending(candidate.title, candidate)
        if candidate.directo:
            # Emisora por el salto: sin yt-dlp (su URL YA es el stream) y
            # con el logo/unscaled del helper comun (es EMISORA solo si el
            # candidato lo es — la radio de canciones nunca genera directos).
            if not await self._reproducir_directo_manual(update, candidate, "/next"):
                return
            self._registrar_historial(candidate)
            self.queue._current = candidate
            self.queue._items = []
            self.queue._cursor = 0
            if update is not None:
                await self._show_card(update.effective_chat.id)
            await self._send_track_card(
                update, f"⏭️ Siguiente: {candidate.title}", candidate
            )
            return
        resolved = await self._stream_for(candidate.url)
        if not resolved:
            await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
            await self._notify_admin(f"Fallo de resolucion en /next: {candidate.title}")
            return
        stream_url, audio_url = resolved
        try:
            await self.player.start()
        except RuntimeError as exc:
            await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
            await self._notify_admin(f"mpv no arranco en /next: {exc}")
            return
        for attempt in range(2):
            try:
                await self.player.load(stream_url, audio_url)
                await self.player.play()
                self._paused = False
                break
            except RuntimeError as exc:
                if attempt == 1 or candidate.url not in self._stream_cache:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    await self._notify_admin(f"Fallo al reproducir {candidate.title}: {exc}")
                    return
                logger.info("Stream cacheado expirado, re-resolviendo: %s", candidate.url)
                self._stream_cache.pop(candidate.url, None)
                resolved = await self._stream_for(candidate.url)
                if not resolved:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    await self._notify_admin(f"Fallo al re-resolver {candidate.title}: {exc}")
                    return
                stream_url, audio_url = resolved
        # Audio arrancando: recien aqui se commitea el nuevo tema en la cola.
        self._registrar_historial(candidate)
        self.queue._current = candidate
        self.queue._items = []
        self.queue._cursor = 0
        self._schedule_prefetch(candidate)
        if update is not None:
            await self._show_card(update.effective_chat.id)
        await self._send_track_card(
            update, f"⏭️ Siguiente: {candidate.title}", candidate
        )

    async def cmd_prev(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Retrocede al tema anterior.

        Modo playlist: cursor con wrap (simetrico a /next).
        Modo radio: si hay algo en la pila back, poppea y lo reproduce (el
        actual pasa al forward stack para que /next pueda volver a el).
        Fallback: si la pila back esta vacia, usa el historico de items
        que dejaron de sonar (queue.previous()).
        """
        if self.queue.current is None:
            await self._reply(update, "No hay ninguna cancion reproduciendose.")
            return

        # ⏮ en modo emisora: primero la sesion del hub 🕘 (el historial de
        # emisoras, hacia la MAS VIEJA); si no hay ancla, la anterior del
        # listado ancla de /radio (simetrica al ⏭; el flujo estandar de
        # navegacion es de canciones).
        if self.queue.current.directo:
            if self._hist_ancla and self._hist_ancla.get("tipo") == "emisora":
                await self._hist_prev_emisora(update)
            else:
                await self._radio_sonar_vecina(update, -1)
            return

        # Modo playlist: cursor con wrap (logica previa).
        if self.queue.has_playlist:
            item = self.queue.previous()
            if item is None:
                await self._reply(update, "No hay cancion anterior en la cola.")
                return
            from_playlist = True
            started = await self._play_item(update, item, preserve_current=from_playlist)
            if started:
                await self._send_track_card(
                    update, f"⏮️ Anterior: {item.title}", item
                )
            return

        # Sesion del hub 🕘 (canciones): hacia la MAS VIEJA del historial
        # (indices mayores). En la ultima del historial: aviso y nada. La
        # cancion fresca del buscador cuenta como la 1: su "anterior" es la
        # que era la 1 (la mas nueva de la lista de la base).
        filas = self._historial(0)
        posicion = self._hist_indice_actual(filas)
        if posicion is not None:
            if filas and (posicion == -1 or posicion + 1 < len(filas)):
                await self._hist_sonar(
                    update, filas[posicion + 1], "⏮️ Anterior: "
                )
                return
            await self._reply(
                update, "Esa es la ultima del historial: no hay anterior."
            )
            return

        # Modo radio.
        # Intentar sacar el item anterior de la pila de navegacion (nav_back).
        # Si esta vacia, fallback al historial de items de la cola.
        item: QueueItem | None = self._pop_from_nav_back()
        used_nav_back = item is not None
        if not used_nav_back:
            # Fallback: usar el historico de items que dejaron de sonar.
            # No es navegacion pura: el nav_back queda igual y el nav_forward
            # tampoco se mezcla. _play_item (con set_current) hara un push
            # normal a nav_back por nosotros.
            item = self.queue.previous()
        if item is None:
            await self._reply(update, "No hay cancion anterior en la cola.")
            return
        if used_nav_back:
            # Guardar el actual en forward: si el usuario hace /next despues,
            # debe volver al item que se esta abandonando.
            if self.queue.current is not None:
                self._nav_forward.append(self.queue.current)
            # Reproducir el item sin pasar por _play_item (que haria un push
            # extra a back, duplicando el item). Manejo manual.
            self._cancel_prefetch()
            await self._render_pending(item.title, item)
            if item.directo:
                # Emisora que volvia por la pila: sin yt-dlp (su URL YA es
                # el stream) y con el logo/unscaled del helper comun.
                if not await self._reproducir_directo_manual(update, item, "/prev (back)"):
                    return
                self.queue._current = item
                self.queue._items = []
                self.queue._cursor = 0
                self._registrar_historial(item)
                if update is not None:
                    await self._show_card(update.effective_chat.id)
                await self._send_track_card(
                    update, f"⏮️ Anterior: {item.title}", item
                )
                return
            resolved = await self._stream_for(item.url)
            if not resolved:
                await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
                await self._notify_admin(f"Fallo de resolucion en /prev (back): {item.title}")
                return
            stream_url, audio_url = resolved
            try:
                await self.player.start()
            except RuntimeError as exc:
                await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
                await self._notify_admin(f"mpv no arranco en /prev: {exc}")
                return
            for attempt in range(2):
                try:
                    # Radio (/prev): conserva el logo de la emisora en la
                    # ventana (sin imagen mpv queda en negro).
                    carga_url, carga_audio = stream_url, audio_url
                    if item.directo:
                        logo = await self.playback.logo_para_mpv(item)
                        if logo:
                            carga_url, carga_audio = logo, stream_url
                    await self.player.load(carga_url, carga_audio)
                    await self.player.play()
                    self._paused = False
                    self.queue._current = item
                    self.queue._items = []
                    self.queue._cursor = 0
                    break
                except RuntimeError as exc:
                    if attempt == 1 or item.url not in self._stream_cache:
                        await self._reply(update, f"No se pudo reproducir: {exc}")
                        await self._notify_admin(f"Fallo al reproducir {item.title}: {exc}")
                        return
                    logger.info("Stream cacheado expirado, re-resolviendo: %s", item.url)
                    self._stream_cache.pop(item.url, None)
                    resolved = await self._stream_for(item.url)
                    if not resolved:
                        await self._reply(update, f"No se pudo reproducir: {exc}")
                        await self._notify_admin(f"Fallo al re-resolver {item.title}: {exc}")
                        return
                    stream_url, audio_url = resolved
            self._registrar_historial(item)
            self._schedule_prefetch(item)
            if update is not None:
                await self._show_card(update.effective_chat.id)
            await self._send_track_card(
                update, f"⏮️ Anterior: {item.title}", item
            )
            return
# Fallback: item del historico de cola. No llamamos a _play_item
        # (que hace _push_to_nav_back y limpiaria nav_forward). En su lugar,
        # hacemos el manejo manual directo para preservar los stacks de navegacion.
        self._cancel_prefetch()
        await self._render_pending(item.title, item)
        if item.directo:
            # Emisora desde el historial: sin yt-dlp y con el logo mediante
            # el helper comun (una emisora NUNCA pasa por resolucion).
            if not await self._reproducir_directo_manual(update, item, "/prev"):
                return
            self.queue._current = item
            self.queue._items = []
            self.queue._cursor = 0
            self._registrar_historial(item)
            if update is not None:
                await self._show_card(update.effective_chat.id)
            await self._send_track_card(
                update, f"⏮️ Anterior: {item.title}", item
            )
            return
        resolved = await self._stream_for(item.url)
        if not resolved:
            await self._reply(update, "No se pudo resolver el video. Espera un momento e intenta de nuevo.")
            await self._notify_admin(f"Fallo de resolucion en /prev: {item.title}")
            return
        stream_url, audio_url = resolved
        try:
            await self.player.start()
        except RuntimeError as exc:
            await self._reply(update, f"Problema al iniciar el reproductor: {exc}")
            await self._notify_admin(f"mpv no arranco en /prev: {exc}")
            return
        for attempt in range(2):
            try:
                await self.player.load(stream_url, audio_url)
                await self.player.play()
                self._paused = False
                self.queue._current = item
                self.queue._items = []
                self.queue._cursor = 0
                break
            except RuntimeError as exc:
                if attempt == 1 or item.url not in self._stream_cache:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    await self._notify_admin(f"Fallo al reproducir {item.title}: {exc}")
                    return
                logger.info("Stream cacheado expirado, re-resolviendo: %s", item.url)
                self._stream_cache.pop(item.url, None)
                resolved = await self._stream_for(item.url)
                if not resolved:
                    await self._reply(update, f"No se pudo reproducir: {exc}")
                    await self._notify_admin(f"Fallo al re-resolver {item.title}: {exc}")
                    return
                stream_url, audio_url = resolved
        self._registrar_historial(item)
        self._schedule_prefetch(item)
        if update is not None:
            await self._show_card(update.effective_chat.id)
        await self._send_track_card(
            update, f"⏮️ Anterior: {item.title}", item
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Detener y rebobinar a 0:00 SIN borrar historial, cola, radio ni
        # navegacion: conserva todo para que ▶ reanude el video desde el inicio.
        # ⏹ tambien BORRA el instante guardado: parar es parar, el proximo ▶
        # arranca de cero (el instante solo lo deja el temporizador al cerrar).
        self._posicion_guardada = None
        if self.player.is_running:
            await self.player.rewind()
        self._queue_advance_needed = False
        self._paused = True
        self._persist_dirty()
        await self._render_card(self._track_status_text())
        await self._reply(update, "⏹️ Detenido. Usa ▶ para reanudar desde el inicio.")

    async def _guardar_posicion_actual(self) -> None:
        """Guarda el instante de la cancion actual antes de cerrar todo (💤).

        Es lo que hace que al volver a abrir y dar ▶ el tema siga donde
        quedo, como en cualquier reproductor. Sin mpv corriendo o sin tiempo
        consultable no hay nada nuevo que guardar: se conserva lo ultimo.
        """
        if self.queue.current is None or not self.player.is_running:
            return
        pos = await self.player.get_time_pos()
        if pos is None:
            return
        self._posicion_guardada = {"item": self.queue.current.to_dict(), "pos": int(pos)}

    async def cmd_letra(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """La letra de lo que suena, como mensaje de texto. Cualquiera
        puede pedirla (como el 📋): es informacion, no reproducecon.

        En emisoras no aplica (no hay cancion: hay un stream en vivo). Si
        lrclib no tiene la cancion, la respuesta es "no encontre" SIN
        avisar al admin; si lrclib esta caido, el motivo honesto tambien
        sin avisar: nada de esto detiene la fiesta.
        """
        item = self.queue.current
        if item is None:
            await self._reply(update, "No hay nada sonando.")
            return
        if item.directo:
            await self._reply(update, "En la radio no hay letra.")
            return
        cancion, artista = _cancion_y_artista(item.title)
        try:
            letra = await lyrics.buscar_letra(cancion, artista)
        except lyrics.LetraError as exc:
            logger.warning("/letra fallo: %s", exc)
            await self._reply(update, f"La letra no se pudo buscar ({exc}).")
            return
        if letra is None:
            await self._reply(update, "No encontré la letra de esta canción.")
            return
        if len(letra) > _TOPE_LETRA:
            letra = letra[:_TOPE_LETRA].rstrip() + "\n… (letra cortada)"
        await self._reply(update, letra)

    async def cmd_volume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await self._reply(update, "Uso: /volume <0-100>")
            return
        try:
            vol = int(context.args[0])
        except ValueError:
            await self._reply(update, "El volumen debe ser un numero entre 0 y 100.")
            return
        if not 0 <= vol <= 100:
            await self._reply(update, "El volumen debe estar entre 0 y 100.")
            return
        if not self.player.is_running:
            await self._reply(update, "No hay ninguna reproduccion activa: el reproductor no esta corriendo.")
            return
        await self.player.set_volume(vol)
        self._volume = vol
        self._persist_dirty()
        await self._reply(update, f"🔊 Volumen: {vol}")

    def _queue_list_text(self, page: int = 0) -> str | None:
        """Encabezado del mensaje de la lista (None si esta vacia).

        Muestra pagina actual y total: "Lista (pag 2/3):", o "Lista:" si hay
        una sola pagina. Es el UNICO texto del mensaje: el listado en si son
        los BOTONES paginados (decision 7b: la lista se muestra una sola vez,
        sin texto plano duplicado).
        """
        items = self.queue.all()
        cur = self.queue.current
        if not items:
            if cur is None:
                return None
            return "Lista:"
        pages = max(1, (len(items) + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
        if pages <= 1:
            return "Lista:"
        page = max(0, min(page, pages - 1))
        return f"Lista (pag {page + 1}/{pages}):"

    def _list_keyboard(self) -> InlineKeyboardMarkup:
        """Teclado del mensaje de la lista: pagina de 10 temas (solo si el
        mensaje lo abrió un admin/dj) + navegacion con el boton ❌ siempre
        presente.

        Cada boton es el tema con su posicion global y el titulo COMPLETO
        (sin truncar); el actual arranca con "▶️ ". En modo radio (sin
        playlist) no hay temas que listar: se muestra una fila con el tema
        SONANDO precedido de "▶️ " (informacion, no seleccion); tocarla
        cierra igual que el ❌. El flag fijo
        `_list_can_select` decide si hay botones de tema: al paginar no
        cambia aunque lo pages un user. La fila de navegacion mantiene 3
        slots para que el ❌ quede centrado; el slot vacio usa "·" (callback
        inerte `lst:noop`). Al pie, un boton 🕘 Historial convierte el MISMO
        mensaje en el historial (el hub de contexto del 📋).
        """
        items = self.queue.all()
        current = self.queue.current
        pages = max(1, (len(items) + _LIST_PAGE_SIZE - 1) // _LIST_PAGE_SIZE)
        page = max(0, min(self._list_page, pages - 1))
        start = page * _LIST_PAGE_SIZE

        rows: list[list[InlineKeyboardButton]] = []
        # Modo radio (sin playlist): no hay items que listar, pero se muestra
        # el tema que esta sonando con ▶️; tocarlo cierra igual que el ❌.
        if not self.queue.has_playlist and current is not None:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"▶️ {current.title.strip()}",
                        callback_data="lst:close",
                    )
                ]
            )
        elif self._list_can_select:
            segment = items[start : start + _LIST_PAGE_SIZE]
            for idx, it in enumerate(segment, start=start + 1):
                label = it.title.strip()
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"{'▶️ ' if it is current else ''}{idx}. {label}",
                            callback_data=f"lst:{idx}",
                        )
                    ]
                )

        prev = (
            InlineKeyboardButton("◀", callback_data="lst:prev")
            if pages > 1 and page > 0
            else InlineKeyboardButton("·", callback_data="lst:noop")
        )
        close = InlineKeyboardButton("❌", callback_data="lst:close")
        next_ = (
            InlineKeyboardButton("▶", callback_data="lst:next")
            if pages > 1 and page < pages - 1
            else InlineKeyboardButton("·", callback_data="lst:noop")
        )
        rows.append(
            [InlineKeyboardButton("🕘 Historial", callback_data="lst:historial")]
        )
        rows.append([prev, close, next_])
        return InlineKeyboardMarkup(rows)

    def _historial(self, directo: int | None = None) -> list[dict]:
        """El historial GUARDADO (la base), lo mas reciente primero; con
        `directo` SOLO emisoras (1) o SOLO canciones (0) — las dos listas
        del hub 🕘 comparten tabla y se separan por ese flag.

        La base es la fuente del hub 🕘 — no el deque de navegacion de la
        cola, que existe para el /prev y el anti-repetir de la radio.
        """
        return self._state.leer_historial(directo=directo)

    def _hist_directo(self) -> int | None:
        """El flag `directo` de la vista ACTIVA del hub: 1 (emisoras) o
        0 (canciones); None en la cola o en el selector."""
        if self._list_view == "radio":
            return 1
        if self._list_view == "historial":
            return 0
        return None

    def _hist_indice_actual(self, items: list[dict]) -> int:
        """La posicion de lo que SUENA dentro del historial de su tipo.

        Solo aplica en una sesion del hub (con ancla). Devuelve:
        - el indice (0 = la mas nueva) si esta en la lista;
        - -1 si lo que suena ES del tipo pero NO esta en el historial
          (una cancion/emisora fresca del buscador: la regla dice que esa
          pasa a ser la 1);
        - None si no hay sesion de hub activa (sin ancla) o si lo que
          suena es del tipo equivocado.
        """
        ancla = self._hist_ancla
        if not ancla:
            return None
        actual = self.queue.current
        if actual is None:
            return None
        if ancla["tipo"] == "emisora" and not actual.directo:
            return None
        if ancla["tipo"] == "cancion" and actual.directo:
            return None
        for i, it in enumerate(items):
            if (it.get("url") or "") == actual.url:
                return i
        return -1

    def _history_text(self, page: int = 0) -> str | None:
        """Encabezado del historial del hub, SEGUN la vista activa (emisoras
        o canciones, dos listas separadas por el selector); None si la vista
        no tiene nada registrado."""
        es_radio = self._list_view == "radio"
        items = self._historial(1 if es_radio else 0)
        if not items:
            return None
        pages = max(1, (len(items) + _HISTORY_PAGE_SIZE - 1) // _HISTORY_PAGE_SIZE)
        if pages <= 1:
            return (
                "📻 Historial de emisoras:"
                if es_radio
                else "🕘 Historial:"
            )
        page = max(0, min(page, pages - 1))
        titulo = "📻 Historial de emisoras" if es_radio else "🕘 Historial"
        return f"{titulo} (pag {page + 1}/{pages}):"

    def _hub_text(self) -> str | None:
        """Encabezado del selector del historial: None si NO hay nada
        guardado de ningun tipo."""
        radios, canciones = self._state.contar_historial()
        if radios == 0 and canciones == 0:
            return None
        return "🕘 ¿Qué historial abro?"

    def _hub_keyboard(self) -> InlineKeyboardMarkup:
        """Selector del hub: dos entradas, el historial de EMISORAS (lo que
        sonó como emisora) y el de CANCIONES (lo que sonó de YouTube), cada
        una con su contador. El ❌ cierra el mensaje como siempre."""
        radios, canciones = self._state.contar_historial()
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"📻 Emisoras ({radios})", callback_data="lst:histr"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"🎵 Canciones ({canciones})", callback_data="lst:histm"
                    )
                ],
                [InlineKeyboardButton("❌", callback_data="lst:close")],
            ]
        )

    def _history_keyboard(self) -> InlineKeyboardMarkup:
        """Teclado del historial de la VISTA ACTIVA (emisoras o canciones:
        dos listas separadas; el indice de cada boton apunta a la lista de
        su vista). Una fila por entrada con UN SOLO boton de ancho completo
        (numero + titulo COMPLETO; tocarlo la repite con el callback
        lst:rep) — Telegram reparte el ancho de cada fila en partes
        iguales, asi que sin boton de borrar al lado el titulo se lee entero.
        Al pie, el salto cruzado al OTRO historial, [🗑 Borrar todo] (dj,
        con confirmacion en dos toques, SOLO del tipo de esta vista), [🔙
        Cola] y la navegacion habitual. Los callbacks lst:del y lst:link
        siguen atendidos (sin boton) para que un teclado viejo no quede muerto.
        """
        items = self._historial(self._hist_directo())
        pages = max(1, (len(items) + _HISTORY_PAGE_SIZE - 1) // _HISTORY_PAGE_SIZE)
        page = max(0, min(self._history_page, pages - 1))
        start = page * _HISTORY_PAGE_SIZE

        radios, canciones = self._state.contar_historial()
        salto = (
            InlineKeyboardButton(
                f"🎵 Canciones ({canciones})", callback_data="lst:histm"
            )
            if self._list_view == "radio"
            else InlineKeyboardButton(
                f"📻 Emisoras ({radios})", callback_data="lst:histr"
            )
        )

        rows: list[list[InlineKeyboardButton]] = []
        for idx, it in enumerate(
            items[start : start + _HISTORY_PAGE_SIZE], start=start
        ):
            filas = [
                InlineKeyboardButton(
                    f"{idx + 1}. {str(it.get('title') or '').strip()}",
                    callback_data=f"lst:rep:{idx}",
                )
            ]
            rows.append(filas)
        if not rows:
            vacio = (
                "No hay emisoras en el historial."
                if self._list_view == "radio"
                else "El historial esta vacio."
            )
            rows.append(
                [InlineKeyboardButton(vacio, callback_data="lst:noop")]
            )
        else:
            if self._historial_borrar_armado:
                # Confirmacion en dos toques: el alcance EXACTO esta en el
                # boton, asi que el dj ve cuantas ENTRADAS DE ESTA VISTA van
                # a la basura (el otro historial queda intacto).
                n = len(items)
                if self._list_view == "radio":
                    texto_borrar = (
                        f"✅ Borrar TODAS las emisoras ({n})"
                        if n != 1
                        else "✅ Borrar la emisora (1)"
                    )
                else:
                    texto_borrar = (
                        f"✅ Borrar TODAS las canciones ({n})"
                        if n != 1
                        else "✅ Borrar la cancion (1)"
                    )
                rows.append(
                    [
                        InlineKeyboardButton(
                            texto_borrar,
                            callback_data="lst:delall_ok",
                        )
                    ]
                )
                rows.append(
                    [
                        InlineKeyboardButton(
                            "❌ Cancelar", callback_data="lst:delall_no"
                        )
                    ]
                )
            else:
                rows.append(
                    [
                        InlineKeyboardButton(
                            "🗑 Borrar todo", callback_data="lst:delall"
                        )
                    ]
                )
        rows.append([salto])
        rows.append([InlineKeyboardButton("🔙 Cola", callback_data="lst:cola")])

        prev = (
            InlineKeyboardButton("◀", callback_data="lst:prev")
            if pages > 1 and page > 0
            else InlineKeyboardButton("·", callback_data="lst:noop")
        )
        close = InlineKeyboardButton("❌", callback_data="lst:close")
        next_ = (
            InlineKeyboardButton("▶", callback_data="lst:next")
            if pages > 1 and page < pages - 1
            else InlineKeyboardButton("·", callback_data="lst:noop")
        )
        rows.append([prev, close, next_])
        return InlineKeyboardMarkup(rows)

    async def _close_list_message(self) -> None:
        """Borra el mensaje de la lista abierto (con su desvanecimiento) y
        limpia las referencias. Tolera que el mensaje ya no exista.

        La pagina del listado NO se resetea: queda persistida para que una
        reapertura (o un reinicio) continuen donde iba el usuario.
        """
        chat = self._list_chat_id
        msg = self._list_message_id
        self._list_chat_id = None
        self._list_message_id = None
        self._list_can_select = False
        if msg is not None and chat is not None:
            try:
                await self._app.bot.delete_message(chat, msg)
            except Exception as exc:  # noqa: BLE001 - no romper el flujo
                logger.warning("No se pudo borrar el mensaje de la lista: %s", exc)

    async def _on_sleep_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Menu del apagado programado de la tarjeta (callback `ap:*`).

        Dj, igual que el resto de los controles: `ap:N` (re)programa el
        temporizador de la sesion — a los N minutos cierra TODO en silencio
        (mpv, bot y launcher; el chat queda sin aviso y la card intacta,
        se re-renderiza al arrancar de nuevo) y vuelve el teclado de control
        (el estado queda visible como "💤 Se apagará en N min" dentro de
        ⚙️ Ajustes). `ap:off` lo apaga, `ap:cerrar` solo cierra el menu
        (conserva el temporizador) y `ap:noop` es el encabezado inerte. El
        temporizador vive en la sesion: un reinicio lo borra.
        """
        query = update.callback_query
        user_id = query.from_user.id if query.from_user else None
        if user_id is not None and not self.roles.has_role(user_id, "dj"):
            await query.answer("No tiene permiso para eso.", show_alert=True)
            return

        if self._control_lock.locked():
            try:
                await query.answer("⏳ Un momento, primero termina...")
            except Exception:
                pass
            return

        async with self._control_lock:
            self._from_card = True
            try:
                data = query.data or ""
                accion = data.split(":", 1)[1] if data.startswith("ap:") else ""
                self._card_chat_id = update.effective_chat.id
                self._card_message_id = query.message.message_id

                if accion == "noop":
                    try:
                        await query.answer()
                    except Exception:
                        pass
                    return
                if accion == "cerrar":
                    try:
                        await query.answer()
                    except Exception:
                        pass
                    await self._swap_card_keyboard(self._control_keyboard())
                    return
                if accion == "off":
                    self._cancelar_apagado()
                    await self._swap_card_keyboard(self._control_keyboard())
                    try:
                        await query.answer("Temporizador apagado 💤")
                    except Exception:
                        pass
                    return
                try:
                    minutos = int(accion)
                except ValueError:
                    try:
                        await query.answer()
                    except Exception:
                        pass
                    return
                if minutos not in SLEEP_PRESETS:
                    try:
                        await query.answer()
                    except Exception:
                        pass
                    return
                self._programar_apagado(minutos)
                # Vuelve el teclado de control (el estado se ve en Ajustes
                # como "💤 Se apagará en N min") y confirma cuanto falta en el toast.
                await self._swap_card_keyboard(self._control_keyboard())
                try:
                    await query.answer(f"⏰ Se apagará en {minutos} min.")
                except Exception:
                    pass
            finally:
                self._from_card = False

    async def _on_ajustes_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Menu de Ajustes de la tarjeta (callback `aj:*`).

        Por ahora solo `aj:cerrar`: vuelve al teclado de control. Las
        opciones internas reusan los callbacks que ya existen (ctl:apagar,
        ctl:calidad, ctl:usuarios), cada uno con su chequeo de rol. Se
        serializa con el MISMO `_control_lock` que el resto de los botones.
        """
        query = update.callback_query
        if self._control_lock.locked():
            try:
                await query.answer("⏳ Un momento, primero termina...")
            except Exception:
                pass
            return

        async with self._control_lock:
            self._from_card = True
            try:
                accion = (query.data or "").split(":", 1)[1]
                self._card_chat_id = update.effective_chat.id
                self._card_message_id = query.message.message_id

                if accion == "cerrar":
                    try:
                        await query.answer()
                    except Exception:
                        pass
                    await self._swap_card_keyboard(self._control_keyboard())
                    return
            finally:
                self._from_card = False

    # --- Apagado programado (opcion 💤 de Ajustes) ------------------------

    def _programar_apagado(self, minutos: int) -> None:
        """(Re)programa el apagado de la sesion: a los `minutos` para la
        reproduccion. Reprogramar cancela el temporizador anterior."""
        self._cancelar_apagado()
        self._sleep_minutes = minutos
        self._sleep_deadline = time.monotonic() + minutos * 60
        self._sleep_task = asyncio.create_task(self._dormir_tras(minutos))

    def _cancelar_apagado(self) -> None:
        """Apaga el temporizador si estaba armado (idempotente)."""
        tarea = self._sleep_task
        self._sleep_task = None
        self._sleep_minutes = None
        self._sleep_deadline = None
        if tarea is not None:
            tarea.cancel()

    def _sleep_restante_min(self) -> int | None:
        """Minutos que FALTAN para el apagado programado (None si no hay).

        Se calcula contra la hora limite guardada al armar; el valor
        programado es solo el respaldo si por algun motivo no hay deadline.
        Nunca devuelve 0: a menos de un minuto todavia se muestra 1 (la
        tarea dispara sola bien antes de que importe el redondeo)."""
        if not self._sleep_minutes:
            return None
        if self._sleep_deadline is None:
            return self._sleep_minutes
        return max(1, round((self._sleep_deadline - time.monotonic()) / 60))

    async def _dormir_tras(self, minutos: int) -> None:
        """Espera `minutos` y dispara el apagado. Reprogramar o apagar
        cancela la tarea antes de que llegue a disparar."""
        await asyncio.sleep(minutos * 60)
        await self._apagar_por_temporizador()

    async def _apagar_por_temporizador(self) -> None:
        """Disparo del temporizador: cierra todo en silencio.

        Guarda el instante del tema para que al volver a abrir y dar ▶ siga
        donde quedo, y sale de la app completa con la marca
        "temporizador" (ver _apagar_y_salir). Igual que en /actualizar: el
        chat no recibe despedidas, la card se re-renderiza sola al volver.
        """
        await self._apagar_y_salir("temporizador")

    async def _apagar_y_salir(self, marca: str) -> None:
        """Cierra TODO en orden (instante guardado, estado a disco, mpv
        apagado) y deja la marca salir_total.flag para que el launcher, al
        ver salir al bot, cierre la app completa (ventana + tray): cero
        procesos abiertos. La marca dice QUE motiva la salida
        ("temporizador" o "actualizacion") para el log del ayudante.

        El instante se guarda ANTES de cerrar el mpv (necesita responder
        time-pos); al volver a abrir, el ▶ lo retoma y lo consume. El
        cierre es os._exit: atexit no corre, asi que el flush y el cierre
        de mpv van EXPLICITOS aca.
        """
        self._sleep_task = None
        self._sleep_minutes = None
        self._sleep_deadline = None
        await self._guardar_posicion_actual()
        self._persist_flush()
        await self.player.quit()
        try:
            import persistence as _persistence

            salida = _persistence.STATE_PATH.parent / "salir_total.flag"
            salida.write_text(marca, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - marque donde se pueda
            logger.warning("No se pudo dejar la marca de salida total: %s", exc)
        self._salir_del_proceso()

    def _salir_del_proceso(self) -> None:
        """Sale del proceso del bot sin limpiezas de atexit: ya se hace a
        mano justo antes (flush del estado y cierre del mpv)."""
        os._exit(0)

    async def _open_queue_list(
        self, query, user_id: int | None, chat_id: int
    ) -> None:
        """Boton 📋 de la card: abre el mensaje-lista como MENSAJE aparte.

        Es un hub de contexto: por defecto muestra la COLA (pagina actual) con
        un boton 🕘 Historial que convierte el MISMO mensaje en el SELECTOR
        del historial (dos listas separadas: emisoras y canciones); si la
        cola esta vacia pero hay historial guardado, abre DIRECTO ese
        selector (es el unico lugar donde vive). Solo responde el toast
        cuando no hay nada que mostrar en ninguna de las tres vistas.

        La card nunca se pisa: se envia un mensaje nuevo en el chat con la
        pagina ACTUAL (texto + botones paginados al unisono; el render clampa
        la pagina al rango valido). Si ya habia una lista abierta, se borra la
        anterior (con su desvanecimiento) y se manda una fresca (decision del
        usuario: nunca editar una lista existente).
        """
        self._list_view = "cola"
        self._history_page = 0
        self._historial_borrar_armado = False
        text = self._queue_list_text()
        if text is None:
            radios, canciones = self._state.contar_historial()
            if radios or canciones:
                text = self._hub_text()
                self._list_view = "hub"
        if text is None:
            try:
                await query.answer("La lista esta vacia.")
            except Exception:  # noqa: BLE001
                pass
            return
        await self._close_list_message()
        self._list_can_select = user_id is not None and self.roles.has_role(user_id, "dj")
        keyboard = (
            self._history_keyboard()
            if self._list_view in ("historial", "radio")
            else self._hub_keyboard()
            if self._list_view == "hub"
            else self._list_keyboard()
        )
        try:
            msg = await self._app.bot.send_message(
                chat_id, text, reply_markup=keyboard
            )
        except Exception as exc:  # noqa: BLE001 - no romper la card
            logger.warning("No se pudo enviar el mensaje de la lista: %s", exc)
            return
        self._list_chat_id = chat_id
        self._list_message_id = msg.message_id

    async def _edit_list_message(self) -> None:
        """Pagina el mensaje de la lista abierto (◀ ▶): edita TEXTO + botones
        de la pagina nueva (la misma pagina en ambos, se sincronizan). El
        mensaje no se recrea; el flag de seleccion no cambia con la pagina.
        Segun la vista (_list_view) pagina la cola o el historial."""
        if self._list_chat_id is None or self._list_message_id is None:
            return
        if self._list_view == "hub":
            text = self._hub_text()
            keyboard = self._hub_keyboard()
        elif self._list_view in ("historial", "radio"):
            text = self._history_text(self._history_page)
            keyboard = self._history_keyboard()
        else:
            text = self._queue_list_text(self._list_page)
            keyboard = self._list_keyboard()
        if text is None:
            return
        try:
            await self._app.bot.edit_message_text(
                text,
                chat_id=self._list_chat_id,
                message_id=self._list_message_id,
                reply_markup=keyboard,
            )
        except Exception as exc:  # noqa: BLE001 - no romper el flujo
            logger.warning("No se pudo paginar la lista: %s", exc)

    async def _list_answer(
        self, query, text: str | None = None, show_alert: bool = False
    ) -> None:
        """Responde un callback de la lista; el toast es prescindible, si
        falla la API se ignora."""
        try:
            await query.answer(text=text, show_alert=show_alert)
        except Exception:  # noqa: BLE001
            pass

    async def _on_list_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Botones del mensaje-lista (callback `lst:*`), el hub del 📋.

        - `lst:close`: CUALQUIER usuario cierra: borra el mensaje de la lista
          (con su desvanecimiento).
        - `lst:prev`/`lst:next`: cualquiera pagina la vista abierta (cola o
          historial); se edita TEXTO + botones al unisono.
        - `lst:historial`/`lst:cola`: cambia la vista sobre el MISMO mensaje.
        - `lst:N`: solo admin/dj (alerta si no): salta a la cancion N de la
          cola y la reproduce de inmediato; la lista se borra al salir.
        - `lst:rep:N` (dj) repite la cancion N del historial; `lst:link:N`
          (cualquiera) manda su enlace al chat; `lst:del:N` (dj) la borra;
          `lst:delall` (dj) arma la confirmacion, `lst:delall_ok` (dj,
          segundo toque) borra TODA la lista del historial y `lst:delall_no`
          cancela.
        - `lst:noop`: placeholder inerte, se responde mudo.

        Cada callback se responde UNA sola vez (un answer por query).
        """
        query = update.callback_query
        data = query.data or ""
        action = data.split(":", 1)[1] if data.startswith("lst:") else ""
        user_id = query.from_user.id if query.from_user else None

        if action in ("", "noop"):
            await self._list_answer(query)
            return

        # Regla del usuario: sin rol NO tiene ningun derecho. Todo toque
        # sobre el hub (paginar, cerrar, enlaces, repetir, borrar... ) es de
        # dj+; el unico aviso permitido es el de siempre.
        if not (user_id is not None and self.roles.has_role(user_id, "dj")):
            await self._list_answer(
                query, "🔒 El bot es solo para dj/admin.", show_alert=True
            )
            return

        if action == "historial":
            radios, canciones = self._state.contar_historial()
            if not (radios or canciones):
                await self._list_answer(query, "El historial esta vacio.")
                return
            # El selector DELANTE de las listas: el dj elige emisoras o
            # canciones y recien ahi se abre su historial.
            self._list_view = "hub"
            self._history_page = 0
            self._historial_borrar_armado = False
            await self._edit_list_message()
            await self._list_answer(query)
            return

        if action in ("histr", "histm"):
            radios, canciones = self._state.contar_historial()
            cuentos = radios if action == "histr" else canciones
            if not cuentos:
                await self._list_answer(
                    query,
                    "No hay emisoras en el historial."
                    if action == "histr"
                    else "El historial esta vacio.",
                )
                return
            self._list_view = "radio" if action == "histr" else "historial"
            self._history_page = 0
            self._historial_borrar_armado = False
            await self._edit_list_message()
            await self._list_answer(query)
            return

        if action == "cola":
            self._historial_borrar_armado = False
            self._list_view = "cola"
            await self._edit_list_message()
            await self._list_answer(query)
            return

        if action in ("prev", "next"):
            delta = -1 if action == "prev" else 1
            # El historial del hub son DOS vistas: emisoras (_list_view
            # "radio") y canciones ("historial"). Antes solo "historial"
            # movia _history_page: en emisoras el toque movia la pagina de
            # la COLA (que no estaba abierta), el historial no se movia y
            # ademas quedaba la pagina de la cola contaminada (relevo
            # 10/2026). Las dos vistas del hub usan la misma pagina.
            if self._list_view in ("historial", "radio"):
                self._history_page += delta
            else:
                self._list_page += delta
            await self._edit_list_message()
            await self._list_answer(query)
            return

        if action == "close":
            self._historial_borrar_armado = False
            await self._list_answer(query)
            await self._close_list_message()
            return

        if action == "delall":
            await self._historial_borrar_confirmar(query, user_id)
            return

        if action == "delall_ok":
            await self._historial_borrar_ejecutar(query, user_id)
            return

        if action == "delall_no":
            self._historial_borrar_armado = False
            await self._edit_list_message()
            await self._list_answer(query)
            return

        if action.startswith(("rep:", "link:", "del:")):
            await self._historial_accion(query, update, user_id, action)
            return

        try:
            position = int(action)
        except ValueError:
            await self._list_answer(query)
            return

        if user_id is None or not self.roles.has_role(user_id, "dj"):
            await self._list_answer(
                query, "Solo el admin o un dj pueden elegir.", show_alert=True
            )
            return

        async with self._control_lock:
            self._from_card = True
            try:
                current = self.queue.current
                item = self.queue.jump_to(position)
                if item is None:
                    await self._list_answer(query, "Numero fuera de rango.")
                    return
                if item is current:
                    # Elegir el tema que YA suena: no salta, no cierra la lista.
                    # Se compara contra el actual ANTES del jump (jump_to ya
                    # movio el cursor y la posicion 1 es el item actual).
                    await self._list_answer(query, f"Ya esta sonando: {item.title}")
                    return
                await self._list_answer(query)
                await self._close_list_message()
                await self._play_item(update, item, preserve_current=True)
            finally:
                self._from_card = False

    async def _historial_accion(
        self, query, update, user_id: int | None, action: str
    ) -> None:
        """Acciones del historial del hub: rep:N (dj, repite), link:N
        (cualquiera, manda el enlace al chat) y del:N (dj, borra la entrada).

        El historial de la VISTA ACTIVA se RELEE DE LA BASE en cada accion
        (emisoras o canciones: `self._hist_directo()`) — los indices del
        teclado son posiciones vivas de ESA lista, y si cambio entre el
        render y el toque se responde "fuera de rango" en vez de tocar la
        entrada equivocada.
        """
        kind, _, raw = action.partition(":")
        try:
            idx = int(raw)
        except ValueError:
            await self._list_answer(query)
            return
        historial = self._historial(self._hist_directo())
        if idx < 0 or idx >= len(historial):
            await self._list_answer(query, "Numero fuera de rango.")
            return
        fila = historial[idx]

        if kind == "link":
            if self._list_chat_id is None:
                return
            try:
                await self._app.bot.send_message(
                    self._list_chat_id, fila.get("url", "")
                )
            except Exception as exc:  # noqa: BLE001 - no romper el hub
                logger.warning("No se pudo enviar el enlace del historial: %s", exc)
                await self._list_answer(query, "No se pudo enviar el enlace.")
                return
            await self._list_answer(query, "Enlace enviado ✓")
            return

        if user_id is None or not self.roles.has_role(user_id, "dj"):
            await self._list_answer(
                query, "Solo el admin o un dj pueden hacer eso.", show_alert=True
            )
            return

        if kind == "del":
            # El borrado es contra la BASE (la fuente del hub). El deque de
            # navegacion de la cola no se toca: sus labores son el /prev y
            # el anti-repetir de la radio.
            video_id = fila.get("video_id") or fila.get("url") or ""
            if not self._state.borrar_historial(video_id):
                await self._list_answer(query, "Numero fuera de rango.")
                return
            await self._edit_list_message()
            await self._list_answer(query, "Borrado del historial ✓")
            return

        # rep: repetir la entrada del historial de la vista activa con las
        # MISMAS garantias que elegir de la cola: resolver el stream primero
        # y, si falla, no se toca lista, historial ni tarjeta (ni ancla).
        item = QueueItem.from_dict(fila)
        async with self._control_lock:
            self._from_card = True
            try:
                await self._list_answer(query)
                await self._close_list_message()
                # Sin preserve_current: un rep del hub es una sesion NUEVA
                # (el item debe quedar posicionado como current, como al
                # elegir de la cola).
                ok = await self._play_item(update, item)
            finally:
                self._from_card = False
        # El ancla SIEMPRE despues del resultado: solo una entrada que SE
        # PUSO a sonar define la sesion del hub (la 8 o la 28 del usuario).
        # Emisora: espera el pais y los TAGS (el ⏭ rota por genero si la
        # fila los trae); una fila vieja sin tags los recupera del API por
        # su stream (una consulta, cacheada). Cancion: la semilla del radio
        # es su artista (asi el auto-avance y la busqueda del ⏭ salen del
        # MISMO ancla).
        if ok:
            if item.directo:
                self._radio_ancla = None
                tags = str(fila.get("tags", "") or "")
                if not tags:
                    tags = str(await radio.tags_por_stream(item.url) or "")
                    # BACKFILL: lo que se acabo de recuperar por stream se
                    # ESCRIBE en la fila del historial. Si no, los tags del
                    # rep viejo viven solo en el ancla de la sesion y el
                    # menu de generos (que lee la base) jamas los ve.
                    if tags:
                        self._state.actualizar_tags(item.url, tags)
                self._hist_ancla = {
                    "tipo": "emisora",
                    "pais": fila.get("channel", ""),
                    "tags": tags,
                }
            else:
                self._hist_ancla = {
                    "tipo": "cancion",
                    "artista": fila.get("artist") or fila.get("channel") or "",
                }
                if self._hist_ancla["artista"]:
                    self._radio_artist = self._hist_ancla["artista"]

    async def _historial_borrar_confirmar(self, query, user_id: int | None) -> None:
        """Primer toque de 🗑 Borrar todo (solo dj): NO borra nada. Arma la
        confirmacion y re-renderiza el teclado con [✅ Borrar TODO el
        historial (N canciones)] + [❌ Cancelar]; el alcance exacto queda
        escrito en el boton para que el dj decida bien informado."""
        if user_id is None or not self.roles.has_role(user_id, "dj"):
            await self._list_answer(
                query, "Solo el admin o un dj pueden hacer eso.", show_alert=True
            )
            return
        if self._hist_directo() is None:
            # Un teclado viejo mandando delall desde el selector: ahi no hay
            # historial abierto que borrar.
            await self._list_answer(query)
            return
        self._historial_borrar_armado = True
        await self._edit_list_message()
        await self._list_answer(query)

    async def _historial_borrar_ejecutar(self, query, user_id: int | None) -> None:
        """Segundo toque (✅) de la confirmacion (solo dj): RECIEN aqui se
        vacia TODO el historial, porque borrar todo es irreversible.

        Despues de vaciar, la vista del historial quedo sin nada: si hay
        cola (o un tema sonando) se vuelve a ella, y si no hay nada se
        cierra el mensaje de lista."""
        if user_id is None or not self.roles.has_role(user_id, "dj"):
            await self._list_answer(
                query, "Solo el admin o un dj pueden hacer eso.", show_alert=True
            )
            return
        self._historial_borrar_armado = False
        # SOLO el tipo de la vista activa: borrar en emisoras vacia
        # emisoras y las canciones quedan (y viceversa); en el selector no
        # hay boton de borrar, asi que nunca llega None.
        self._state.vaciar_historial(self._hist_directo())
        if self._queue_list_text() is not None:
            self._list_view = "cola"
            self._list_page = 0
            await self._edit_list_message()
        else:
            await self._close_list_message()
        await self._list_answer(query, "Historial borrado ✓")

    def _members_text(self, page: int = 0) -> str:
        """Encabezado del mensaje del editor de usuarios (boton 👥): contador
        total de usuarios + pagina actual/total + contador de cambios
        pendientes de aplicar."""
        users = self.roles.known_users()
        pages = max(1, (len(users) + _MEMBERS_PAGE_SIZE - 1) // _MEMBERS_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        header = f" Usuarios ({len(users)}):"
        if pages > 1:
            header += f" · pag {page + 1}/{pages}"
        pendientes = len(self._members_staged)
        if pendientes:
            header += f" · {pendientes} cambio{'s' if pendientes != 1 else ''} pendiente{'s' if pendientes != 1 else ''}"
        return header

    def _members_keyboard(self) -> InlineKeyboardMarkup:
        """Teclado del editor de usuarios: una fila-boton por usuario con su
        rol actual (o el rol staged si el admin lo esta cambiando) + [◀ ❌ ✔ ▶].

        Iconos: 👤 user · 🎧 dj · 🔒 admin (el rol del admin no se toca).

        El ✔ (aplicar) solo aparece cuando HAY cambios staged; si no hay, se
        muestra un `·` placeholder. El ❌ (cancelar) siempre está visible: sin
        staged simplemente cierra (toast "Sin cambios."). El ✔ no cierra solo:
        pide confirmación con alerta Sí/No.
        """
        users = self.roles.known_users()
        pages = max(1, (len(users) + _MEMBERS_PAGE_SIZE - 1) // _MEMBERS_PAGE_SIZE)
        page = max(0, min(self._members_page, pages - 1))
        start = page * _MEMBERS_PAGE_SIZE

        rows: list[list[InlineKeyboardButton]] = []
        for u in users[start : start + _MEMBERS_PAGE_SIZE]:
            role = self._members_staged.get(u["id"], u["role"])
            icon = "🎧" if role == "dj" else "🔒" if role == "admin" else "👤"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{icon} {u['name']}",
                        callback_data=f"mem:user:{u['id']}",
                    )
                ]
            )

        prev = (
            InlineKeyboardButton("◀", callback_data="mem:prev")
            if pages > 1 and page > 0
            else InlineKeyboardButton("·", callback_data="mem:noop")
        )
        cancel = InlineKeyboardButton("❌", callback_data="mem:cancel")
        # ✔ solo cuando hay cambios pendientes; de lo contrario placeholder.
        commit = (
            InlineKeyboardButton("✔", callback_data="mem:commit")
            if self._members_staged
            else InlineKeyboardButton("·", callback_data="mem:noop")
        )
        next_ = (
            InlineKeyboardButton("▶", callback_data="mem:next")
            if pages > 1 and page < pages - 1
            else InlineKeyboardButton("·", callback_data="mem:noop")
        )
        rows.append([prev, cancel, commit, next_])
        return InlineKeyboardMarkup(rows)

    async def _open_members_list(self, query, admin_id: int) -> None:
        """Boton 👥 de la card: envia el editor de usuarios al chat PRIVADO
        del admin. En el grupo no aparece ningun mensaje; el admin solo ve el
        toast de confirmacion. Si el bot no puede escribirle por privado (el
        admin nunca inicio chat con el bot), avisa con una alerta."""
        users = self.roles.known_users()
        if not users:
            try:
                await query.answer("No hay usuarios registrados.", show_alert=True)
            except Exception:  # noqa: BLE001 - toast prescindible
                pass
            return
        try:
            msg = await self._app.bot.send_message(
                admin_id,
                self._members_text(),
                reply_markup=self._members_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001 - no romper la card
            logger.info("No se pudo enviar la lista de usuarios al privado: %s", exc)
            try:
                await query.answer(
                    "Abre un chat privado conmigo y envía /start.",
                    show_alert=True,
                )
            except Exception:  # noqa: BLE001
                pass
            return
        self._members_chat_id = admin_id
        self._members_message_id = msg.message_id
        self._members_user_id = admin_id
        self._members_page = 0
        self._members_staged = {}
        try:
            await query.answer("Envié la lista de usuarios a su chat privado.")
        except Exception:  # noqa: BLE001 - toast prescindible
            pass

    async def _edit_members_message(self) -> None:
        """Pagina el editor de usuarios (◀ ▶): edita TEXTO + botones en el
        mensaje abierto (el mismo, no se recrea)."""
        if self._members_chat_id is None or self._members_message_id is None:
            return
        try:
            await self._app.bot.edit_message_text(
                self._members_text(self._members_page),
                chat_id=self._members_chat_id,
                message_id=self._members_message_id,
                reply_markup=self._members_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001 - no romper el flujo
            logger.warning("No se pudo paginar la lista de usuarios: %s", exc)

    async def _close_members_message(self) -> None:
        """Borra el editor de usuarios (con su desvanecimiento) y limpia el
        estado de la edicion (incluidos los cambios staged)."""
        chat = self._members_chat_id
        msg = self._members_message_id
        self._members_chat_id = None
        self._members_message_id = None
        self._members_user_id = None
        self._members_page = 0
        self._members_staged = {}
        if msg is not None and chat is not None:
            try:
                await self._app.bot.delete_message(chat, msg)
            except Exception as exc:  # noqa: BLE001 - no romper el flujo
                logger.warning("No se pudo borrar el mensaje de usuarios: %s", exc)

    async def _commit_members_edits(self, query) -> None:
        """Boton ❌ del editor: aplica TODOS los cambios staged a roles.json,
        avisa a cada usuario de su rol nuevo y cierra la lista. Si no hubo
        cambios, solo la cierra sin tocar nada."""
        staged = dict(self._members_staged)
        total = len(staged)
        for uid, role in staged.items():
            self.roles.set_role(uid, role, self.roles.get_name(uid))
            try:
                await self._notify_role_change(uid, role)
            except Exception:  # noqa: BLE001 - no romper el commit
                logger.warning("No se pudo notificar el rol nuevo al usuario %s.", uid)
        try:
            await self._sincronizar_menu_dj(self._app.bot)
        except Exception:  # noqa: BLE001 - el commit del rol no se tira
            logger.warning("No se pudo sincronizar el menú de comandos tras los roles.")
        await self._close_members_message()
        try:
            if total:
                await query.answer(f"Roles guardados ({total} cambio{'s' if total != 1 else ''}).")
            else:
                await query.answer("Sin cambios.")
        except Exception:  # noqa: BLE001 - toast prescindible
            pass

    async def _on_members_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Botones del editor de usuarios (callback `mem:*`).

        Corre en el chat PRIVADO del admin que abrio la lista (guard propio,
        porque este handler no pasa por el filtro de chat del grupo):

        - `mem:user:<id>`: alterna el rol staged del usuario (user <-> dj).
          Los admins no se tocan. Nada se persiste: solo el contador del
          header y la etiqueta cambian en vivo.
        - `mem:prev`/`mem:next`: pagina el editor.
        - `mem:cancel`: si hay staged, muestra alerta `¿Descartar N cambios?`
          con botones `mem:cancel-yes`/`mem:cancel-no`; si NO hay staged,
          cierra directo con toast `Sin cambios.`.
        - `mem:cancel-yes`: descarta los staged y cierra (toast `Cambios
          descartados (N).`).
        - `mem:cancel-no`: restaura el mensaje original (staged intactos).
        - `mem:commit`: si hay staged, muestra alerta `¿Aplicar N cambios?`
          con botones `mem:commit-yes`/`mem:commit-no`; sin staged no debe
          llegar (boton oculto).
        - `mem:commit-yes`: aplica staged a roles.json, notifica a cada
          usuario y cierra (toast `Roles guardados (N cambios).`).
        - `mem:commit-no`: restaura el mensaje original (staged intactos).
        """
        query = update.callback_query
        user = query.from_user
        if user is None or query.message is None:
            return
        chat = query.message.chat
        if (
            chat is None
            or chat.type != "private"
            or chat.id != user.id
            or not self.roles.has_role(user.id, "admin")
        ):
            try:
                await query.answer("No tiene permiso para eso.", show_alert=True)
            except Exception:  # noqa: BLE001
                pass
            return

        data = query.data or ""
        parts = data.split(":")

        async def answer(text: str | None = None, show_alert: bool = False) -> None:
            try:
                await query.answer(text=text, show_alert=show_alert)
            except Exception:  # noqa: BLE001
                pass

        if len(parts) < 2 or parts[1] in ("", "noop"):
            await answer()
            return

        if parts[1] in ("prev", "next"):
            self._members_page += -1 if parts[1] == "prev" else 1
            await self._edit_members_message()
            await answer()
            return

        # CANCELAR
        if parts[1] == "cancel":
            if self._members_staged:
                total = len(self._members_staged)
                msg = f"¿Descartar {total} cambio{'s' if total != 1 else ''}?"
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "❌ Sí, descartar",
                                callback_data="mem:cancel-yes",
                            ),
                            InlineKeyboardButton(
                                "⚠️ No",
                                callback_data="mem:cancel-no",
                            ),
                        ]
                    ]
                )
                try:
                    await self._app.bot.edit_message_text(
                        msg,
                        chat_id=self._members_chat_id,
                        message_id=self._members_message_id,
                        reply_markup=kb,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("No se pudo editar mensaje confirmacion cancel: %s", exc)
                await answer()
                return
            # Sin staged: cierra directo
            await self._close_members_message()
            await answer("Sin cambios.")
            return

        if parts[1] == "cancel-yes":
            await self._close_members_message()
            await answer("Cambios descartados.", show_alert=False)
            return

        if parts[1] == "cancel-no":
            await self._edit_members_message()
            await answer()
            return

        # APLICAR (COMMIT)
        if parts[1] == "commit":
            if self._members_staged:
                total = len(self._members_staged)
                msg = f"¿Aplicar {total} cambio{'s' if total != 1 else ''}?"
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✔ Sí, aplicar",
                                callback_data="mem:commit-yes",
                            ),
                            InlineKeyboardButton(
                                "⚠️ No",
                                callback_data="mem:commit-no",
                            ),
                        ]
                    ]
                )
                try:
                    await self._app.bot.edit_message_text(
                        msg,
                        chat_id=self._members_chat_id,
                        message_id=self._members_message_id,
                        reply_markup=kb,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("No se pudo editar mensaje confirmacion commit: %s", exc)
                await answer()
                return
            # Sin staged: no deberia llegar (boton oculto), pero por seguridad
            await self._close_members_message()
            await answer("Sin cambios.")
            return

        if parts[1] == "commit-yes":
            await self._commit_members_edits(query)
            return

        if parts[1] == "commit-no":
            await self._edit_members_message()
            await answer()
            return

        # TOGGLE USER
        if parts[1] != "user" or len(parts) < 3:
            await answer()
            return
        try:
            uid = int(parts[2])
        except ValueError:
            await answer()
            return

        if uid == user.id:
            await answer("Tu propio rol no se cambia desde aquí.", show_alert=True)
            return
        base = self.roles.get_role(uid)
        if base == "admin":
            await answer("El rol del admin no se puede cambiar.", show_alert=True)
            return
        current = self._members_staged.get(uid, base)
        next_role = "dj" if current == "user" else "user"
        if next_role == base:
            self._members_staged.pop(uid, None)  # revertir el cambio en vivo
        else:
            self._members_staged[uid] = next_role
        await self._edit_members_message()
        name = self.roles.get_name(uid) or str(uid)
        await answer(f"{name} ahora es {next_role}.")

    async def _auto_kick_job(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Expulsa (ban + unban) a los usuarios con rol 'user' que superaron
        las horas de tolerancia desde su registro (config.kick_after_hours).

        El ban+unban es un "kick": puede volver a entrar con el enlace de
        invitacion y volver a pedir el rol. Admin y dj estan exentos; quien
        no tiene timestamp (migrado del formato viejo) no se expulsa. Si el
        bot no es admin del grupo, el intento falla con log (y no rompe nada).
        """
        horas = self.config.kick_after_hours or 0
        chat = self.config.allowed_chat_id
        if horas <= 0 or chat is None:
            return
        limite = int(time.time()) - int(horas * 3600)
        for u in self.roles.known_users():
            if u["role"] != "user":
                continue
            joined = u["joined_at"]
            if not isinstance(joined, int) or joined > limite:
                continue
            try:
                await self._app.bot.ban_chat_member(chat, u["id"])
                await self._app.bot.unban_chat_member(chat, u["id"])
                logger.info(
                    "Expulsado usuario inactivo %s (rol user, mas de %s h).",
                    u["name"],
                    int(horas),
                )
            except Exception as exc:  # noqa: BLE001 - no tumbar el job
                logger.warning(
                    "No se pudo expulsar al usuario %s: %s (¿el bot es admin del grupo?)",
                    u["id"],
                    exc,
                )

    async def cmd_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cur = self.queue.current
        if cur is None:
            await self._reply(update, "No hay ninguna cancion reproduciendose.")
        else:
            await self._send_track_card(update, f"▶️ Sonando: {cur.title}", cur)
