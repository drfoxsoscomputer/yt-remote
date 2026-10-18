"""La letra de lo que suena, via lrclib.net (API publico, sin API key).

El plan /letra pide la letra como MENSAJE DE TEXTO y, si no existe,
responder "No encontre la letra de esta cancion" SIN avisar al admin:
la letra es informacion, no reproducecon. Lrclib es una base de datos
libre de letras; se le pregunta por pista+artista (mas preciso) o por
texto libre cuando no hay artista, y se devuelve el mejor resultado
que tenga letra, tal cual lo trajo (sin tocarlo).
"""

from constants import LRCLIB_API, LRCLIB_TIMEOUT, LRCLIB_USER_AGENT


class LetraError(Exception):
    """Motivo honesto por el que la consulta a lrclib fallo."""


async def buscar_letra(cancion: str, artista: str = "") -> str | None:
    """Letra de la cancion, o None si lrclib no tiene coincidencia.

    Con artista se busca por pista+artista; sin artista, texto libre (q).
    Los instrumentales se saltan: None es "no encontre", no un error.
    """
    import httpx

    params = (
        {"track_name": cancion, "artist_name": artista}
        if artista
        else {"q": cancion}
    )
    try:
        async with httpx.AsyncClient(
            timeout=LRCLIB_TIMEOUT, headers={"User-Agent": LRCLIB_USER_AGENT}
        ) as cli:
            r = await cli.get(LRCLIB_API, params=params)
            r.raise_for_status()
            datos = r.json()
    except Exception as exc:  # noqa: BLE001 - el motivo honesto va al usuario
        raise LetraError(f"lrclib no responde ({exc})") from exc
    if not isinstance(datos, list):
        raise LetraError("lrclib devolvio algo raro (no es una lista)")
    for fila in datos:
        if fila.get("instrumental"):
            continue
        letra = fila.get("plainLyrics") or fila.get("syncedLyrics")
        if letra:
            return str(letra)
    return None
