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
    servidor recomienda) y pais/logo opcionales."""

    nombre: str
    pais: str
    stream: str
    logo: str = ""

    def to_dict(self) -> dict:
        return {
            "nombre": self.nombre,
            "pais": self.pais,
            "stream": self.stream,
            "logo": self.logo,
        }

    @classmethod
    def desde_dict(cls, d: dict) -> "Emisora":
        return cls(
            nombre=str(d.get("nombre", "")),
            pais=str(d.get("pais", "")),
            stream=str(d.get("stream", "")),
            logo=str(d.get("logo", "")),
        )


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
            )
        )
    return salida


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
        raise RadioError(f"radio-browser no responde ({exc})") from exc
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


async def buscar_pais(termino: str, cantidad: int) -> list:
    """Busqueda por pais: dos letras (VE, MX) = countrycode oficial; lo
    demas = nombre del pais tal como lo escribio el usuario."""
    clave = "countrycode" if len(termino) == 2 and termino.isalpha() else "country"
    valor = termino.upper() if clave == "countrycode" else termino
    filas = await _get(
        "/json/stations/search",
        _params(**{clave: valor}, limit=str(cantidad), order="votes", reverse="true"),
    )
    return _normalizar(filas)
