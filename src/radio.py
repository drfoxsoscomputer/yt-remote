"""Emisoras de radio por internet via radio-browser (API publico).

El plan /radio pide destacadas y busqueda por nombre, genero y pais: todo
es UNA llamada GET al API publico de radio-browser, sin dependencias
nuevas (solo httpx, que ya va en el bundle). Las favoritas NO pasan por
aqui: son una lista propia del bot que se persiste con el estado.

Solo se piden campos que el bot usa, y cada consulta exige
`hidebroken=true`: una emisora caida en el listado es una reproduccion
que falla en vivo, que es lo peor que le puede pasar al salon.
"""

from dataclasses import dataclass

from constants import RADIO_API, RADIO_API_TIMEOUT


class RadioError(Exception):
    """Motivo honesto por el que una consulta a radio-browser fallo."""


@dataclass
class Emisora:
    """Emisora normalizada: nombre a una linea, UN solo stream (el que el
    servidor recomienda) y pais/logo/tags opcionales."""

    nombre: str
    pais: str
    stream: str
    logo: str = ""
    # Tags del API como una sola cadena 'salsa,vallenato' (la respuesta del
    # servicio viene separada por comas). Sirven para el ancla por genero.
    tags: str = ""

    def to_dict(self) -> dict:
        return {
            "nombre": self.nombre,
            "pais": self.pais,
            "stream": self.stream,
            "logo": self.logo,
            "tags": self.tags,
        }

    @classmethod
    def desde_dict(cls, d: dict) -> "Emisora":
        return cls(
            nombre=str(d.get("nombre", "")),
            pais=str(d.get("pais", "")),
            stream=str(d.get("stream", "")),
            logo=str(d.get("logo", "")),
            tags=str(d.get("tags", "")),
        )


def _tags_de(d: dict) -> str:
    """Los tags de UNA fila del API como cadena plana 'salsa,vallenato'.

    El servicio devuelve `tags` como cadena separada por comas (y a veces
    con espacios raros o mayusculas): se parte, se limpia, se une SIN
    espacios y en minuscula para que las comparaciones de genero nunca
    dependan de como lo escribieron.
    """
    crudo = d.get("tags") or []
    partes = (
        [crudo] if isinstance(crudo, str) else [str(t) for t in crudo]
    )
    limpios: list[str] = []
    vistos: set[str] = set()
    for parte in partes:
        for tag in str(parte).split(","):
            tag = tag.strip().lower()
            if tag and tag not in vistos:
                vistos.add(tag)
                limpios.append(tag)
    return ",".join(limpios)


def _normalizar(filas: list) -> list:
    """Filas del API -> Emisoras. Se descarta lo sin nombre o sin stream:
    el nombre del API trae a veces saltos de linea pegados, asi que se
    aplana a una linea."""
    salida: list[Emisora] = []
    for d in filas:
        stream = str(d.get("url_resolved") or d.get("url") or "").strip()
        nombre = " ".join(str(d.get("name") or "").split())
        if not nombre or not stream:
            continue
        salida.append(
            Emisora(
                nombre=nombre,
                pais=" ".join(str(d.get("country") or "").split()),
                stream=stream,
                logo=str(d.get("favicon") or "").strip(),
                tags=_tags_de(d),
            )
        )
    return salida


def _motivo_red(exc: Exception) -> str:
    """El motivo de una falla de red, SIEMPRE con el tipo de excepcion
    incluido: hay excepciones de httpx cuyo str() sale VACIO (p. ej.
    ConnectError de un servidor que corta), y el aviso quedaba
    'radio-browser no responde ()', que no diagnostica nada."""
    texto = str(exc).strip()
    return f"{type(exc).__name__}: {texto}" if texto else type(exc).__name__


async def _get(path: str, params: dict) -> list:
    """GET al API de radio-browser. Cualquier falla se devuelve honesta:
    RadioError con el motivo, para que el usuario vea QUE fallo."""
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=RADIO_API_TIMEOUT, follow_redirects=True
        ) as cli:
            r = await cli.get(RADIO_API + path, params=params)
            r.raise_for_status()
            datos = r.json()
    except Exception as exc:  # noqa: BLE001 - el motivo honesto va al usuario
        raise RadioError(f"radio-browser no responde ({_motivo_red(exc)})") from exc
    if not isinstance(datos, list):
        raise RadioError("radio-browser devolvió algo raro (no es una lista)")
    return datos


def _params(**extra: str) -> dict:
    """Params comunes: solo emisoras vivas; las busquedas van ordenadas por
    votos, que es la mejor aproximacion a "lo que la gente de verdad
    escucha" que da el API."""
    base = {"hidebroken": "true"}
    base.update(extra)
    return base


async def destacadas(cantidad: int) -> list:
    """Las emisoras mas votadas de todo radio-browser (topvote)."""
    filas = await _get(f"/json/stations/topvote/{cantidad}", _params())
    return _normalizar(filas)


async def buscar_nombre(termino: str, cantidad: int) -> list:
    """Busqueda por nombre de emisora."""
    filas = await _get(
        "/json/stations/search",
        _params(name=termino, limit=str(cantidad), order="votes", reverse="true"),
    )
    return _normalizar(filas)


async def buscar_genero(termino: str, cantidad: int) -> list:
    """Busqueda por genero (el tag del API: salsa, bolero, rock...)."""
    filas = await _get(
        "/json/stations/search",
        _params(tag=termino, limit=str(cantidad), order="votes", reverse="true"),
    )
    return _normalizar(filas)


def _normalizar_clave(texto: str) -> str:
    """Clave para matchear nombres paises: minusculas SIN acentos y
    espacios aplastados ('México' == 'mexico' == 'me  xico')."""
    import unicodedata

    sin_acento = "".join(
        c
        for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(sin_acento.casefold().split())


_POR_PAIS: "dict[str, dict] | None" = None  # cache del catalogo de paises


async def paises() -> "dict[str, dict]":
    """Catalogo de paises del API, por nombre normalizado. Una sola
    consulta por proceso (/json/countries son unos cientos de filas)."""
    global _POR_PAIS  # noqa: PLW0603
    if _POR_PAIS is None:
        filas = await _get("/json/countries", {})
        catalogo: dict[str, dict] = {}
        for fila in filas:
            if isinstance(fila, dict) and fila.get("iso_3166_1"):
                catalogo[_normalizar_clave(str(fila.get("name") or ""))] = fila
        _POR_PAIS = catalogo
    return _POR_PAIS


def _es_imagen_raster(datos: bytes) -> bool:
    """True si los bytes parecen una imagen raster que mpv puede
    decodificar (PNG, JPEG, GIF, BMP, ICO, WEBP). Rechaza SVG y HTML:
    hay favicons del API que son SVG y mpv los rechaza (antes un logo
    SVG rechazado MATABA la emisora entera con 'mpv rechazo el stream')."""
    if not datos or len(datos) < 24:
        return False
    if datos[:4] == b"RIFF":
        return datos[8:12] == b"WEBP"
    cabeceras = (
        b"\x89PNG\r\n\x1a\n",  # PNG
        b"\xff\xd8\xff",  # JPEG
        b"GIF87a",
        b"GIF89a",  # GIF
        b"BM",  # BMP
        b"\x00\x00\x01\x00",  # ICO
    )
    return datos.startswith(cabeceras)


async def bajar_logo(url: str) -> str | None:
    """Baja el logo de una emisora a data/radio/logo.png para que mpv lo
    muestre a pantalla completa mientras suena el stream (los directos de
    radio son solo audio: sin imagen, mpv no tiene mas que una ventana
    negra). Devuelve la ruta o None si no hay logo / no se pudo bajar:
    la imagen NUNCA frena el audio. El tamaño queda topado por si la
    'favicon' es en realidad un album entero."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    import httpx

    from bot_process import data_dir

    destino = data_dir() / "radio" / "logo.png"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as cli:
            r = await cli.get(url)
            r.raise_for_status()
            datos = r.content
        if not datos or len(datos) > 8 * 1024 * 1024:
            return None
        if not _es_imagen_raster(datos):
            # SVG, HTML de error, basura: sin imagen usable. El audio
            # sigue; NUNCA se deja un logo podrido en el archivo de mpv.
            import logging

            logging.getLogger(__name__).info(
                "Logo de emisora no es una imagen raster para mpv: %s", url
            )
            return None
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(datos)
    except Exception as exc:  # noqa: BLE001 - sin imagen no pasa nada
        import logging

        logging.getLogger(__name__).info(
            "Logo de emisora no disponible (%s): %s", url, _motivo_red(exc)
        )
        return None
    return str(destino)


async def buscar_pais(termino: str, cantidad: int) -> list:
    """Busqueda por pais. El API pide el nombre del pais EXACTO (es
    case-sensitive: country=venezuela devuelve 0 emisores, country=Venezuela
    funciona), asi que el nombre se RESUELVE contra el catalogo oficial
    (/json/countries, se pide una vez y queda en cache) y el filtro sale
    del codigo ISO de dos letras. Con 2 letras va directo como countrycode.
    Sin match: motivo honesto en vez del 'no encontre emisoras' que no
    diagnostica nada."""
    if len(termino) == 2 and termino.isalpha():
        filas = await _get(
            "/json/stations/search",
            _params(
                countrycode=termino.upper(),
                limit=str(cantidad),
                order="votes",
                reverse="true",
            ),
        )
        return _normalizar(filas)
    catalogo = await paises()
    clave = _normalizar_clave(termino)
    fila = catalogo.get(clave)
    if fila is None:
        # Match parcial: el API nombra a los paises por su nombre OFICIAL
        # ('Bolivarian Republic Of Venezuela'), asi que 'venezuela' no
        # iguala exacto pero SI esta DENTRO del nombre. Entre varios
        # candidatos gana el que mas emisoras tiene (el que la gente
        # realmente escucha); sin candidatos ahi si se avisa honesto.
        candidatas = [
            (fila_c, int(fila_c.get("stationcount") or 0))
            for nombre_clave, fila_c in catalogo.items()
            if clave and (clave in nombre_clave or nombre_clave in clave)
        ]
        if candidatas:
            fila = max(candidatas, key=lambda par: par[1])[0]
    if fila is None or not fila.get("iso_3166_1"):
        raise RadioError(
            f"no reconozco «{termino}» como país; usá el nombre completo "
            "(Venezuela, Colombia...) o el código de dos letras (VE, MX)"
        )
    filas = await _get(
        "/json/stations/search",
        _params(
            countrycode=str(fila["iso_3166_1"]),
            limit=str(cantidad),
            order="votes",
            reverse="true",
        ),
    )
    return _normalizar(filas)


_IDIOMA_NOMBRE = {
    # Codigo de idioma de Telegram -> nombre del idioma en radio-browser.
    # El catalogo de idiomas del API va por nombre (verificado contra el
    # servicio: 'language=spanish' trae emisoras en espanol con tags y
    # votos); el `languagecode` crudo mezcla emisoras ajenas.
    "es": "spanish",
    "pt": "portuguese",
    "en": "english",
    "fr": "french",
    "de": "german",
    "it": "italian",
}


def idioma_del_api(codigo: str) -> str | None:
    """El nombre del idioma del API para un codigo de Telegram, None si
    el bot no lo mapea (el home cae a destacadas sin inventar idiomas)."""
    return _IDIOMA_NOMBRE.get(str(codigo or "").strip().lower().split("-")[0])


async def buscar_idioma(termino: str, cantidad: int) -> list:
    """Emisoras del IDIOMA pedido (name en el API, ej. 'spanish'),
    ordenadas por votos: la semilla del primer arranque, cuando todavia
    no hay historial que determine un pais."""
    filas = await _get(
        "/json/stations/search",
        _params(language=termino, limit=str(cantidad), order="votes", reverse="true"),
    )
    return _normalizar(filas)


_TAGS_POR_STREAM: "dict[str, str | None]" = {}  # cache de la sesion


async def tags_por_stream(stream: str) -> str | None:
    """Los tags de UNA emisora por su stream (`/json/stations/byurl`).

    Para filas del historial guardadas ANTES de la columna tags: el API
    compara si identifica la emisora y devuelve None si no la conoce. El
    resultado queda en cache por proceso: se consulta UNA vez por stream.
    """
    global _TAGS_POR_STREAM  # noqa: PLW0603
    clave = str(stream or "").strip()
    if not clave:
        return None
    if clave in _TAGS_POR_STREAM:
        return _TAGS_POR_STREAM[clave]
    tags: str | None = None
    try:
        filas = await _get(
            "/json/stations/byurl", _params(url=clave)
        )
    except RadioError:
        _TAGS_POR_STREAM[clave] = None
        return None
    if isinstance(filas, list) and filas:
        objetivo = None
        # Match por stream exacto primero; si el API devolvio la
        # url_resolved distinta, se acepta la fila si comparte el stream base.
        for fila in filas:
            if str(fila.get("url_resolved") or "").strip() == clave:
                objetivo = fila
                break
        if objetivo is None:
            objetivo = filas[0]
        tags = _tags_de(objetivo) or None
    _TAGS_POR_STREAM[clave] = tags
    return tags
