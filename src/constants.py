"""Las constantes del bot, con el POR QUE de cada una.

Estaban arriba de `bot.py`, mezcladas con los imports, y cada una con su
explicacion al lado. Eso servia cuando el bot era un archivo; ahora que el
codigo esta partido en piezas, un timeout escondido en el modulo que coordina
los handlers no dice a que pieza pertenece. Ademas, para probar un limite hay
que importar el modulo entero: moverlos aqui los hace importables sin arrastrar
telegram.

`bot.py` los reexporta: los nombres siguen valiendo para las 40 referencias
internas y para las pruebas.
"""

# Version de YT-Remote, la unica de cara al usuario. Es la misma que el
# release de GitHub y la que muestra /salud; el test la comparara contra
# release_rules.json (--release_rules_version_match) para que los dos
# sitios no se desincronicen al publicar.
VERSION = "1.1.4"

# Timeout para cada resolucion de stream (yt-dlp en thread): si tarda mas,
# se corta y se trata como fallo de resolucion. Sin esto, un yt-dlp colgado
# congelaria el polling entero (los handlers de python-telegram-bot corren
# en secuencia) y el bot dejaria de responder botones y comandos.
RESOLVE_TIMEOUT = 20.0

# Timeout para cada busqueda (yt-dlp en thread): si tarda mas, se corta y se
# avisa; sin esto, un yt-dlp colgado dejaria el "Buscando..." eterno.
SEARCH_TIMEOUT = 30.0

# Arranque al toque de playlists: los primeros tracks se cargan al instante
# (una sola llamada rapida de yt-dlp con playlistend) y el RESTO se expande
# en segundo plano. El techo tecnico de seguridad: YouTube no permite
# playlists de mas de 5000 items, asi que no hay tope de usuario.
QUICK_TRACKS = 15
QUICK_TIMEOUT = 15.0
MAX_PLAYLIST = 5000
EXPAND_TIMEOUT = 300.0

# La playlist se carga de a VENTANAS, no entera: se mantienen estos temas
# cargados por delante de lo que esta sonando y se pide la tanda siguiente
# cuando la reproduccion se acerca al final de lo cargado. Con una lista de
# 1.882 temas, traerla entera era descargar 1.882 metadatos para mostrar 10
# por pagina.
PLAYLIST_WINDOW = 30
# Cuantas vueltas de espera (1 s cada una) se le dan a una tanda antes de
# seguir con lo que hay. Es el tope de seguridad por si la carga se traba.
PLAYLIST_ESPERAS = 15

# Mensaje de lista (boton de lista): temas visibles por pagina.
LIST_PAGE_SIZE = 10

# Historial (boton 🕘 del hub del 📋): entradas visibles por pagina. Mas
# corto que la cola a proposito: cada fila lleva tres botones (repetir,
# enlace y borrar) ademas del titulo completo.
HISTORY_PAGE_SIZE = 10

# Cuantas canciones GUARDADAS conserva el historial (filas unicas: si una
# cancion vuelve a sonar se reubica arriba, no suma otra fila). 500 unicas
# son varios meses de memoria de escucha; lo mas viejo se recorta solo.
MAX_HISTORY = 500

# Editor de usuarios (boton de usuarios): usuarios conocidos visibles por pagina.
MEMBERS_PAGE_SIZE = 10

# /actualizar: de donde baja el yt-dlp nuevo y cuanto esperar. El portable
# no trae pip, asi que el comando pide la metadata de PyPI, verifica el
# sha256 publicado del wheel y reemplaza la carpeta yt_dlp/ del
# site-packages que corre el bot.
PYPI_YTDLP_JSON = "https://pypi.org/pypi/yt-dlp/json"
PYPI_META_TIMEOUT = 30.0
PYPI_WHEEL_TIMEOUT = 60.0

# El release de YT-Remote en GitHub: /actualizar pregunta el ULTIMO release
# estable y baja el ZIP portable publicado. Sin token: el repo es publico y
# el limite de GitHub (60/hora por IP) sobra para un comando de admin.
GITHUB_RELEASE_JSON = (
    "https://api.github.com/repos/drfoxsoscomputer/yt-remote/releases/latest"
)
GITHUB_META_TIMEOUT = 30.0
GITHUB_ZIP_TIMEOUT = 300.0
# Patron del asset portable dentro del release (fnmatch sobre el nombre).
RELEASE_ZIP_PATRON = "yt-remote-*-portable.zip"
# Carpeta (dentro de data/) donde queda el ZIP bajado hasta que el
# ayudante lo aplica.
UPDATE_DIR = "update"

# /radio (radio-browser): lote de emisoras por consulta y emisoras por
# pagina del listado. El lote es mayor que la pagina para que ◀ ▶ paginen
# sin volver al API.
RADIO_BATCH = 20
RADIO_PAGE_SIZE = 5

# Host fijo de radio-browser: si cae, el error es honesto ("radio-browser
# no responde (...)"); no hay fallback multi-servidor porque el plan no lo
# pide.
RADIO_API = "https://de1.api.radio-browser.info"
RADIO_API_TIMEOUT = 15.0

# /letra (lrclib.net): base de datos libre de letras, sin API key. El
# User-Agent identifica al bot (lrclib pide un agente que no parezca un
# navegador vacio); si no responde, el aviso es honesto ("lrclib no
# responde (...)") y NUNCA se le avisa al admin: la letra es informacion,
# no reproducecon.
LRCLIB_API = "https://lrclib.net/api/search"
LRCLIB_TIMEOUT = 10.0
LRCLIB_USER_AGENT = "yt-remote-bot (github.com/drfoxsoscomputer/yt-remote)"
# Telegram corta los mensajes en 4096 caracteres: la letra se manda entera
# hasta este tope y mas alla se corta con un aviso honesto, nunca a medias
# sin decirlo.
TOPE_LETRA = 3500

# Listado de resultados de /buscar: cuantos resultados se traen por tanda y
# cuantos se ven por pagina. El listado se pagina, asi que traer 10 de una no
# hace esperar mas: es la MISMA busqueda con mas resultados.
SEARCH_BATCH = 10
SEARCH_PAGE_SIZE = 5
# Cuando el usuario nombra un artista se piden el doble de resultados: el filtro
# se queda solo con los cuyo canal es el artista, y con 10 resultados puede no
# entrar ni uno. Es la unica forma de darle al filtro una oportunidad real sin
# inventar un orden.
SEARCH_BATCH_CON_ARTISTA = SEARCH_BATCH * 2