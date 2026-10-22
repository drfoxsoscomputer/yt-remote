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
import sys

from constants import RADIO_API_ESPEJOS, RADIO_API_TIMEOUT


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


async def _pedir_una(base: str, path: str, params: dict) -> list:
    """UN GET a UN espejo. Separado del bucle para poder probar la rotacion
    (la prueba reemplaza esta funcion y hace fallar el primer espejo)."""
    import httpx

    async with httpx.AsyncClient(
        timeout=RADIO_API_TIMEOUT, follow_redirects=True
    ) as cli:
        r = await cli.get(base + path, params=params)
        r.raise_for_status()
        return r.json()


_INDICE_ESPEJO = 0


def _servidores() -> list[str]:
    """Los espejos en el orden de ESTA consulta, empezando por uno distinto
    cada vez (round-robin): asi el trafico no se acumula en el mismo host y
    un 404 por tasa no condena la llamada."""
    global _INDICE_ESPEJO  # noqa: PLW0603
    if not RADIO_API_ESPEJOS:
        return []
    _INDICE_ESPEJO = (_INDICE_ESPEJO + 1) % len(RADIO_API_ESPEJOS)
    return list(RADIO_API_ESPEJOS[_INDICE_ESPEJO:]) + list(
        RADIO_API_ESPEJOS[:_INDICE_ESPEJO]
    )


async def _get(path: str, params: dict) -> list:
    """GET al API de radio-browser con rotacion de espejos: si uno no
    contesta (o lo corta por tasa), se prueba el siguiente con los MISMOS
    params. Si ninguno responde, RadioError con el motivo del ultimo."""
    ultimo = ""
    for base in _servidores():
        try:
            datos = await _pedir_una(base, path, params)
        except Exception as exc:  # noqa: BLE001 - el motivo honesto va al usuario
            ultimo = _motivo_red(exc)
            continue
        if not isinstance(datos, list):
            raise RadioError("radio-browser devolvió algo raro (no es una lista)")
        return datos
    raise RadioError(f"radio-browser no responde ({ultimo})")


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


async def buscar_genero(
    termino: str, cantidad: int, pais: str | None = None
) -> list:
    """Busqueda por genero (el tag del API: salsa, bolero, rock...).

    Con `pais` (nombre oficial o codigo de dos letras) el listado queda
    ACOTADO a ese pais: es lo que impide que la rotacion de una emisora
    venezolana salte a una de Arabia Saudita o Indonesia. Sin `pais` es la
    busqueda global de siempre (la usa el listado abierto a proposito). Un
    pais que el catalogo no reconoce NO se ignora callado: RadioError, que
    es preferible al cruce de frontera invisible."""
    extra = {
        "tag": termino,
        "limit": str(cantidad),
        "order": "votes",
        "reverse": "true",
    }
    if pais:
        iso = await _iso_de_pais(pais)
        if iso is None:
            raise RadioError(
                f"no reconozco «{pais}» como país para acotar el género"
            )
        extra["countrycode"] = iso
    filas = await _get("/json/stations/search", _params(**extra))
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


async def _iso_de_pais(termino: str) -> str | None:
    """El codigo ISO de dos letras de un pais: acepta el codigo ('VE') o el
    nombre oficial/del catalogo ('Bolivarian Republic Of Venezuela',
    'Venezuela'). None si el catalogo no lo reconoce."""
    texto = str(termino or "").strip()
    if not texto:
        return None
    if len(texto) == 2 and texto.isalpha():
        return texto.upper()
    catalogo = await paises()
    clave = _normalizar_clave(texto)
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
        return None
    return str(fila["iso_3166_1"])


async def buscar_pais(termino: str, cantidad: int) -> list:
    """Busqueda por pais. El API pide el nombre del pais EXACTO (es
    case-sensitive: country=venezuela devuelve 0 emisores, country=Venezuela
    funciona), asi que el nombre se RESUELVE contra el catalogo oficial
    (/json/countries, se pide una vez y queda en cache) y el filtro sale
    del codigo ISO de dos letras. Con 2 letras va directo como countrycode.
    Sin match: motivo honesto en vez del 'no encontre emisoras' que no
    diagnostica nada."""
    iso = await _iso_de_pais(termino)
    if iso is None:
        raise RadioError(
            f"no reconozco «{termino}» como país; usá el nombre completo "
            "(Venezuela, Colombia...) o el código de dos letras (VE, MX)"
        )
    filas = await _get(
        "/json/stations/search",
        _params(
            countrycode=iso,
            limit=str(cantidad),
            order="votes",
            reverse="true",
        ),
    )
    return _normalizar(filas)


_TAGS_POR_PAIS: "dict[str, list[tuple[str, int]]]" = {}  # cache de la sesion


async def tags_de_pais(
    iso: str, cantidad: int = 300, tope: int = 60
) -> list[tuple[str, int]]:
    """Los generos (tags) REALMENTE usados por las emisoras de un pais,
    ordenados por cantidad de emisoras: es la suma directa del listado del
    API (nada inventado), cacheada por proceso y por pais (UNA consulta).

    `tope` limita el menu (los tags raros de una sola emisora no merecen
    boton); el `cantidad` muestrea las mas votadas del pais."""
    clave = str(iso or "").strip().upper()
    if not clave:
        return []
    if clave not in _TAGS_POR_PAIS:
        filas = await buscar_pais(clave, cantidad)
        suma: dict[str, int] = {}
        for emisora in filas:
            for tag in str(emisora.tags or "").split(","):
                tag = tag.strip().lower()
                if len(tag) >= 3:
                    suma[tag] = suma.get(tag, 0) + 1
        ordenado = sorted(suma.items(), key=lambda par: (-par[1], par[0]))
        _TAGS_POR_PAIS[clave] = [par for par in ordenado[:tope] if par[1] >= 2]
    return _TAGS_POR_PAIS[clave]

_PAIS_COORDENADAS: dict[str, tuple[float, float]] = {
    # Centroide de cada pais (iso, lat, lon): es DATO GEOGRAFICO, no uso.
    # Sirve para ordenar el menu de paises por cercania al equipo
    # (vez primero lo tuyo, despues los vecinos, no lo que mas vota).
    "VE": (7.1, -66.2), "CO": (4.6, -74.1), "EC": (-1.8, -78.2),
    "PE": (-9.2, -75.0), "PA": (8.5, -80.1), "CR": (9.7, -84.2),
    "NI": (12.9, -85.2), "HN": (14.8, -86.6), "SV": (13.8, -88.9),
    "GT": (15.5, -90.3), "BZ": (17.2, -88.7), "CU": (21.5, -77.8),
    "JM": (18.1, -77.3), "HT": (18.9, -72.7), "DO": (18.7, -70.2),
    "PR": (18.2, -66.4), "TT": (10.7, -61.2), "GY": (4.9, -58.9),
    "SR": (4.1, -56.0), "BR": (-10.8, -52.9), "BO": (-16.7, -64.7),
    "PY": (-23.2, -58.4), "CL": (-35.7, -71.5), "AR": (-34.0, -64.2),
    "UY": (-32.8, -55.8), "MX": (23.6, -102.5), "US": (39.8, -98.6),
    "CA": (56.1, -106.3), "GL": (71.7, -42.6),
    "ES": (40.2, -3.6), "PT": (39.4, -8.2), "FR": (46.6, 2.2),
    "IT": (41.9, 12.6), "GB": (54.8, -3.5), "IE": (53.4, -8.2),
    "DE": (51.2, 10.4), "NL": (52.2, 5.5), "BE": (50.6, 4.7),
    "CH": (46.8, 8.2), "AT": (47.6, 14.1), "PL": (52.1, 19.4),
    "CZ": (49.7, 15.3), "RO": (45.9, 25.0), "GR": (39.1, 22.0),
    "TR": (39.0, 35.2), "RU": (58.0, 60.0), "UA": (49.0, 31.4),
    "MA": (31.9, -6.3), "DZ": (28.2, 2.7), "TN": (34.1, 9.6),
    "EG": (26.5, 29.9), "NG": (9.1, 8.7), "ZA": (-30.6, 22.9),
    "IL": (31.4, 34.9), "SA": (24.0, 45.1), "AE": (23.9, 54.1),
    "IN": (21.1, 78.0), "PK": (30.4, 69.4), "CN": (35.9, 104.2),
    "JP": (36.2, 138.3), "KR": (35.9, 127.8), "PH": (12.9, 121.8),
    "ID": (-2.5, 118.0), "AU": (-25.7, 134.5), "NZ": (-41.5, 172.8),
}

_ZONA_PAIS: dict[str, tuple[str | None, float, float]] = {
    # Windows TimeZoneKeyName -> (iso o None, lat, lon) del EQUIPO: es el
    # LUGAR que importa (la maquina del salon suena ahi; el telefono no
    # dice nada). Zonas compartidas por varios paises quedan sin iso y
    # con su punto representativo: el menu de paises se ordena desde ese
    # punto (cercania) sin afirmar un pais equivocado.
    "Venezuela Standard Time": ("VE", 7.1, -66.2),
    "Central Standard Time (Mexico)": ("MX", 23.6, -102.5),
    "Pacific Standard Time (Mexico)": ("MX", 20.7, -105.2),
    "Mountain Standard Time (Mexico)": ("MX", 29.0, -107.5),
    "Cuba Standard Time": ("CU", 21.5, -77.8),
    "SA Pacific Standard Time": (None, 4.6, -74.1),
    "SA Western Standard Time": (None, 10.5, -66.9),
    "SA Eastern Standard Time": (None, -9.2, -75.0),
    "SA Central Standard Time": (None, -16.7, -64.7),
    "E. South America Standard Time": ("BR", -10.8, -52.9),
    "Central America Standard Time": (None, 9.7, -84.2),
    "Central Standard Time": (None, 39.8, -98.6),
    "Mountain Standard Time": (None, 39.8, -98.6),
    "Eastern Standard Time": (None, 39.8, -98.6),
    "Pacific Standard Time": (None, 39.8, -98.6),
    "Romance Standard Time": (None, 46.6, 2.2),
    "GMT Standard Time": ("GB", 54.8, -3.5),
    "Central Europe Standard Time": (None, 48.7, 9.5),
    "AUS Eastern Standard Time": ("AU", -25.7, 134.5),
    "New Zealand Standard Time": ("NZ", -41.5, 172.8),
}


def pais_del_equipo() -> tuple[str | None, float | None, float | None]:
    """(iso, lat, lon) del LUGAR del equipo: se lee la zona horaria de
    Windows (stdlib winreg, cero preguntas, cero escritura del usuario).

    - Zona con pais propio ('Venezuela Standard Time'): notifica el iso y
      su punto, y el menu de paises lo pone PRIMERO.
    - Zona compartida: iso None y punto representativo (solo sirve para
      ordenar por cercania; jamas se afirma un pais equivocado).
    - Fuera de Windows o zona desconocida: (None, None, None) y el menu
      muestra el catalogo por orden del servicio (stationcount)."""
    if sys.platform != "win32":
        return (None, None, None)
    try:
        import winreg  # solo existe en Windows

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
        ) as clave:
            zona = str(winreg.QueryValueEx(clave, "TimeZoneKeyName")[0] or "")
    except OSError:
        return (None, None, None)
    if zona in _ZONA_PAIS:
        iso, lat, lon = _ZONA_PAIS[zona]
        return (iso, lat, lon)
    # Zona conocida por Windows pero no mapeada por nosotros: mejor
    # callado y sin pais que inventar el lugar del salon.
    return (None, None, None)


def _distancia(lat1, lon1, lat2, lon2):
    """Distancia aproximada en km (Haversine, suficiente para ORDENAR
    paises; no es geodesia fina)."""
    import math

    r = 6371.0
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(df / 2) ** 2
        + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


_PAIS_VISIBLE = {
    # Nombres para MOSTRAR en espanol; la BUSQUEDA SIEMPRE usa el nombre
    # oficial del catalogo (el API entiende su propio filete inglés).
    "Bolivarian Republic Of Venezuela": "Venezuela",
    "The United States Of America": "Estados Unidos",
    "The United Mexican States": "México",
    "The Dominican Republic": "República Dominicana",
    "Republic Of Colombia": "Colombia",
    "Republic Of Chile": "Chile",
    "Republic Of Peru": "Perú",
    "Republic Of Ecuador": "Ecuador",
    "Republic Of Cuba": "Cuba",
    "Republic Of Panama": "Panamá",
    "Republic Of Bolivia": "Bolivia",
    "Republic Of Paraguay": "Paraguay",
    "Republic Of Costa Rica": "Costa Rica",
    "Republic Of El Salvador": "El Salvador",
    "Republic Of Nicaragua": "Nicaragua",
    "Republic Of Guatemala": "Guatemala",
    "Republic Of Honduras": "Honduras",
    "Republic Of Argentina": "Argentina",
    "Republic Of Uruguay": "Uruguay",
    "Kingdom Of Spain": "España",
    "Portuguese Republic": "Portugal",
    "French Republic": "Francia",
    "Republic Of Peru ": "Perú",
    "United Mexican States": "México",
    "Dominican Republic": "República Dominicana",
    "United States Of America": "Estados Unidos",
    "United States": "Estados Unidos",
    "The United Kingdom Of Great Britain And Northern Ireland": "Reino Unido",
    "Federal Republic Of Germany": "Alemania",
    "Italian Republic": "Italia",
    "Swiss Confederation": "Suiza",
    "Switzerland": "Suiza",
    "Commonwealth Of Puerto Rico": "Puerto Rico",
    "Republic Of Trinidad And Tobago": "Trinidad y Tobago",
    "Republic Of Panama ": "Panamá",
}


def pais_visible(nombre: str) -> str:
    """El nombre EN ESPAÑOL para mostrar de un país del API. Sin mapa:
    el nombre original (los rótulos mentirosos mejor ni aparecen)."""
    texto = str(nombre or "").strip()
    return _PAIS_VISIBLE.get(texto, texto)


_GENERO_VISIBLE = {
    # Rotulo en ESPAÑOL para los tags de genero que el API escribe en
    # ingles (o en singular/plural mezclado). La BUSQUEDA siempre usa el
    # tag real: esto es solo lo que se lee en el boton.
    "christian": "Cristianas", "cristiana": "Cristianas",
    "cristiano": "Cristianas", "cristian": "Cristianas",
    "cristianas": "Cristianas", "christians": "Cristianas",
    "catholic": "Católicas", "catolica": "Católicas",
    "catolico": "Católicas", "catolicas": "Católicas",
    "gospel": "Gospel", "evangelio": "Evangélicas",
    "evangelical": "Evangélicas", "evangelica": "Evangélicas",
    "religious": "Religiosas", "religion": "Religiosas",
    "religioso": "Religiosas", "religiosa": "Religiosas",
    "bible": "Bíblicas", "biblia": "Bíblicas",
    "biblical": "Bíblicas", "biblica": "Bíblicas",
    "talk": "Conversación", "news": "Noticias", "noticias": "Noticias",
    "music": "Música", "musica": "Música",
    "sports": "Deportes", "deportes": "Deportes",
    "classical": "Clásica", "clasica": "Clásica",
    "electronic": "Electrónica", "electronica": "Electrónica",
    "romantic": "Romántica", "romantica": "Romántica",
    "balada": "Baladas", "baladas": "Baladas",
    "latin": "Latina", "latina": "Latina", "latino": "Latina",
    "tropical": "Tropical", "folk": "Folclor", "folklore": "Folclor",
    "instrumental": "Instrumental", "oldies": "Clásicos", "retro": "Retro",
    "variada": "Variada", "varied": "Variada", "hits": "Éxitos",
}


def genero_visible(tag: str) -> str:
    """El rotulo EN ESPAÑOL de un genero (tag del API). Sin mapa: el tag
    con la inicial en mayuscula (nunca vacio, nunca en crudo si se conoce)."""
    texto = str(tag or "").strip()
    if not texto:
        return ""
    mapeado = _GENERO_VISIBLE.get(texto.lower())
    if mapeado:
        return mapeado
    return texto[:1].upper() + texto[1:]


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
