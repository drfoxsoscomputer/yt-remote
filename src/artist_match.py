"""Comparar el artista que el usuario pidio con el canal de un resultado.

Cuatro funciones PURAS: no tocan nada del bot, no leen estado, no dependen de
telegram. Estaban metidas como metodos de `YTRemoteBot` sin usar ni un atributo,
lo cual es la senal de que no son metodos: son funciones.

Que vivan aqui tambien las hace probables SIN levantar el bot entero, que antes
implicaba importar `telegram`.

OJO CON LOS DOS NORMALIZADORES: `_normalizar` y `clave_texto` parecen el mismo
y NO lo son. El primero se queda solo con `[a-z0-9]` (una "ñ" desaparece
entera); el segundo usa `isalnum()` y la conserva. Cada uno tiene sus
llamadores y cambiarlos seria cambiar el comportamiento, no un refactor, asi
que conviven con la diferencia documentada.
"""

import unicodedata


def normalizar(s: str) -> str:
    """Normaliza un texto para comparaciones: minusculas, sin acentos,
    sin puntuacion ni espacios ("Kent Leroy" -> "kentleroy")."""
    import re as _re

    s = "".join(
        c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
    )
    return _re.sub(r"[^a-z0-9]", "", s)


def clave_texto(valor: str) -> str:
    """Clave para comparar nombres de artista o de canal.

    Sin mayusculas, sin acentos y solo letras y numeros, para que "GP BAND",
    "Gp Band", "gp band" y "gpband" sean la MISMA clave. Si no, comparar
    cadenas escritas por personas distintas no termina nunca.
    """
    base = unicodedata.normalize("NFKD", valor or "")
    sin_acentos = "".join(c for c in base if not unicodedata.combining(c))
    return "".join(c for c in sin_acentos.lower() if c.isalnum())


def coincide_artista(artist: str, r) -> bool:
    """¿Este resultado es del artista pedido?

    SOLO el canal decide, y a proposito. Se probo tambien con el titulo y
    es un error: una cover de otro artista pone el nombre del original en el
    titulo ("IMPACTANTE - Mafe Restrepo - GP BAND - Video Oficial") y
    entonces la cover pasaba como si fuera del artista. El canal es el unico
    dato estructurado que dice quien es.

    Con menos de 3 letras no se filtra nada: con claves tan cortas cualquier
    palabra contiene a otra y se terminaria descartando todo.
    """
    clave = clave_texto(artist)
    if len(clave) < 3:
        return True
    canal = clave_texto(r.channel)
    if not canal:
        return False
    return clave in canal or (len(canal) >= 3 and canal in clave)


def filtrar_por_artista(artist: str, resultados: list) -> tuple[list, str]:
    """Deja el listado en el artista pedido.

    Un `/buscar gp band - impacto` que devuelve primero a Mafe Restrepo y a
    Luisa Yepez hace que el usuario elija otra cosa, y si elige la
    equivocada la radio se queda anclada a un artista que no pidio.

    Sin coincidencia NO se oculta nada y NO se reordena: cuando el canal no
    es el artista no hay forma honesta de distinguir una version original de
    una cover, porque las dos ponen el mismo titulo. Se avisa y el usuario
    elige. Antes se prometeria "esto es lo mas parecido" sin poder cumplirlo.
    """
    if not artist:
        return resultados, ""
    del_articista = [r for r in resultados if coincide_artista(artist, r)]
    if del_articista:
        return del_articista, f"solo {artist}"
    return resultados, (
        f"ningun resultado es de «{artist}»: te muestro los que parece"
    )