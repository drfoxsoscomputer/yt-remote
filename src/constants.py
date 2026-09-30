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

# Editor de usuarios (boton de usuarios): usuarios conocidos visibles por pagina.
MEMBERS_PAGE_SIZE = 10

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