<p align="center">
  <img src="static/img/logo-ytremote.png" alt="YT-Remote" width="180" />
</p>

# <p align="center"> YT-Remote </p>

Bot de Telegram para controlar la reproducción de videos de YouTube en su PC.
Búsqueda por chat, tarjeta de control con botones, radio automática por artista.

**Versión actual:** v1.2.0 (radio por país y género, `/buscar` sin escribir,
historiales separados, `/letra` y menú de comandos por rol).

Lo que cambia respecto de v1.1.4:

| Cambio | Qué es |
|--------|--------|
| Radio por país y género | `/radio` abre un menú con 🌍 Países y 🎵 Géneros. El país de la PC va primero (sale de la zona horaria de Windows, sin preguntar nada) y los géneros son los de las emisoras de ese país, con los que ya sonaron en la sala arriba. La rotación se queda en ese país: ya no salta a una emisora de otro continente |
| Destacadas y favoritas | 🔥 Destacadas trae las emisoras más votadas del catálogo. ❤️ Favoritas guarda las suyas con el ♥ de cada fila, y la lista sobrevive a los reinicios |
| `/buscar` sin escribir | Sin texto, `/buscar` muestra botones con los artistas que más sonaron en la sala: un toque busca ese artista, con las mismas garantías que escribirlo |
| Historiales separados | El 🕘 del 📋 pregunta qué historial abrir: 📻 Emisoras o 🎵 Canciones, cada uno con su contador. Tocar una fila la repite y ⏭ / ⏮ caminan por ese historial |
| `/letra` | La letra de lo que está sonando, como mensaje de texto. En una emisora avisa que no hay letra y, si la base libre no la tiene, lo dice sin inventar |
| Menú por persona | El "/" se arma por usuario: el invitado solo ve `/start`, el dj ve además `/buscar`, `/radio` y `/letra`, y el admin suma `/reglas`, `/salud` y `/actualizar` |
| Contenido infantil | Los videos que YouTube marca como "hecho para niños" volvieron a reproducirse: el bot prueba otro cliente de consulta antes de darse por vencido |
| Tarjeta quieta | Con un listado abierto (`/buscar`, `/radio` o el home de artistas) la tarjeta no se mueve; vuelve al final al elegir o cancelar |

Lo que cambia respecto de v1.1.3:

| Cambio | Qué es |
|--------|--------|
| Video que no arrancaba | En Windows, el programa descartaba por error el comando que le mandaba el video a mpv: el reproductor abría la ventana y se quedaba sin nada, y el bot respondía "mpv no confirmó la carga". Arreglado |
| `/next` se rendía | Cuando YouTube rechazaba un enlace viejo (error 403), el bot esperaba hasta 15 segundos con el reproductor muerto y después fallaba. Ahora descarta ese enlace, busca uno nuevo al instante y el siguiente tema suena igual |
| Motivo real de los fallos | Si mpv rechaza un video, el aviso dice el motivo real (por ejemplo "HTTP 403") en vez de un error genérico. Si YouTube devuelve el video sin formatos, también lo explica |
| El proceso no salía | Al detener el bot, el proceso quedaba vivo por un hilo bloqueado. Ahora cierra limpio |
| Log del reproductor | mpv escribe su propio registro en `data/mpv.log`, así que cualquier fallo se puede diagnosticar a la distancia |

Lo que cambia respecto de v1.1.2:

| Cambio | Qué es |
|--------|--------|
| Resultados en texto | El listado de `/buscar` es un solo mensaje de texto: "Página 1", una fila por canción con su nombre y su duración, y los botones de paginar. Antes mandaba una foto por resultado, que en el celular se veía diminuta |
| Filtro de artista | Si usted nombra un artista, el listado deja solo canciones **de ese artista** (se decide por el canal, no por el título, porque una cover pone el nombre del original en el título). Si no entra ninguna, el bot lo dice y muestra todas en vez de esconderlas |
| Aviso de "Sin conexión" | Cuando se cae internet, la **ventana** pone "Sin conexión" al instante. Antes el bot no decía nada hasta mucho después, y los comandos enviados durante la caída se perdían en silencio |
| El token no se muestra | Al abrir **Configuración** los campos salen vacíos, con la nota de que están guardados. Si deja los campos vacíos y guarda, se conserva lo que ya tenía |
| Una sola base | Lista, historial, roles y ajustes viven todos en `data/ytremote.db`. Si algo no se puede entender, el bot **no escribe encima** y avisa: sus datos quedan intactos |
| La lista no se borra sola | Antes, si el estado estaba dañado, el primer guardado lo sobrescribía y la lista y el historial desaparecían sin avisar. Ahora deja una copia y se niega a escribir |

## Descarga e instalación

Descargue el ZIP desde GitHub, descomprímalo en cualquier carpeta y listo.
No necesita instalar nada del propio programa: Python, mpv y el bot viajan
dentro de la carpeta. La ventana usa el componente WebView2 del sistema (ya
viene con Windows 11 y en Windows 10 actualizados); si falta, el programa
indica cómo instalarlo.

1. Descargue el ZIP desde la página de releases de GitHub
2. Descomprímalo donde quiera
3. Doble clic en `ytremote.exe`. Se abre la ventana del launcher: pegue su
   TOKEN de bot (créelo con [@BotFather](https://t.me/BotFather)) y su ID de
   Telegram (consíguelo con [@userinfobot](https://t.me/userinfobot)), y
   presione **Conectar**
4. El bot arranca en segundo plano y la ventana se oculta al lado del reloj.
   Para abrirla de nuevo, doble clic en el ícono de YT-Remote en la bandeja
5. En un grupo de Telegram agregue el bot y escriba `/start`

## Ventana del launcher

La ventana de YT-Remote es la puerta de entrada del programa y tiene dos
pantallas:

**Estado** — es la pantalla principal. Un botón de encendido grande en el
centro conecta o detiene el bot, y debajo se muestra el estado con un color:

| Estado | Color |
|--------|-------|
| Conectando / En línea | Verde |
| Desconectado / Error | Rojo |
| Sin conexión | Gris |
| Sin sesión | Gris |

**Sin conexión** es distinto de **Desconectado**: el proceso sigue vivo y se
recupera solo cuando vuelve internet, sin que usted tenga que tocar nada. Los
comandos que lleguen durante la caída se descartan (el bot no puede responder
mientras no hay red).

El avatar se muestra en la esquina superior con un punto del mismo color de
estado. El menú de la esquina superior abre las opciones **Configuración** y
**Cerrar sesión**.

**Configuración** — el formulario con los datos iniciales. Los campos salen
**vacíos**: el token y el ID no se vuelven a mostrar en pantalla, aunque estén
guardados. Guardar con los campos vacíos **conserva** lo que ya estaba.

| Campo | Qué es |
|-------|--------|
| Token del bot | El código que le dio `@BotFather` al crear el bot (vacío = se conserva el guardado) |
| ID de admin | Su ID numérico de Telegram (primer usuario con rol admin) |
| Expulsar tras horas | Horas de tolerancia para invitados: `0` desactiva la expulsión |

El botón **Guardar** (la primera vez dice **Conectar**) valida y guarda los
datos y arranca el bot. Si ya hay una sesión guardada, al abrir el programa
el bot se conecta solo y se minimiza al lado del reloj automáticamente.

Al cerrar la ventana con la **X**, el programa se minimiza al lado del reloj
sin preguntar (el bot sigue corriendo). Para salir de verdad, clic derecho en
el ícono de la bandeja y elija **Salir**. La opción **Cerrar sesión** borra la
configuración, detiene el bot y vuelve a la pantalla de Configuración.

La configuración queda en `session.enc`, dentro de la carpeta `data/` que
está junto al ejecutable, y **no se sube a GitHub**.

La lista de canciones, el historial, los roles y los ajustes viven todos en un
solo archivo: `data/ytremote.db`. La primera vez que arranca, el bot trae los
datos de las versiones anteriores y deja los archivos viejos renombrados
`.migrado` (no los borra). Si esa base no se puede entender —porque es de una
versión más nueva que el programa, o porque está dañada— el bot **no escribe
nada encima**, deja una copia al lado y lo avisa por el log: es preferible no
guardar antes que perder lo que tenía.

Solo puede ejecutarse una instancia a la vez: si intenta abrir otra, aparece
un aviso. La ventana usa el runtime *Microsoft Edge WebView2* que ya viene
con Windows 11 y con Windows 10 actualizados; si falta, el programa indica
cómo instalarlo.

## Cómo buscar

```
/buscar Marcos Witt - Dios de lo Impossible
```

Lo que va **antes del guion** es el **artista** (el bot reproduce canciones
de ese cantante). Lo que va después es la primera canción.

Si no usa el guion, todo el texto se busca como nombre de artista:
```
/buscar Jesus Adrian Romero
```

Si pega un link de YouTube, reproduce directo sin buscar.

Si escribe `/buscar` sin nada y ya sonó música en la sala, el bot muestra los
artistas que más sonaron como botones: un toque busca ese artista, igual que
escribirlo. Si todavía no hay historial, le recuerda el uso con un ejemplo y
que también acepta un link.

Si el link es de una **playlist o mix**, arranca de inmediato con los primeros
temas y sigue cargando el resto en segundo plano (el bot avisa cuando quedó
completa). El 📋 muestra la lista completa mientras se va expandiendo, y la
posición se **guarda**: al reiniciar, retoma la misma canción y la misma página
del 📋 donde iba.

Los videos **en vivo** se reproducen con su audio normal y **sincronizado**:
el HLS del directo se resuelve como video y audio separados (YouTube no
entrega directos en un solo stream combinado), se unen en un único playlist
y mpv los sincroniza. Las duraciones desconocidas aparecen como `--:--`.

Para que un directo cargue, el bot abre mpv con el whitelist de protocolos del
demuxer de video ampliado
(`--demuxer-lavf-o=protocol_whitelist=[file,http,https,tcp,tls,crypto,data]`):
sin eso, el playlist local que une video y audio no podía bajar sus
sub-playlists https y mpv quedaba en "Drop files to play here". Si un directo
no logra cargar, el bot avisa "No se pudo reproducir" en vez de mostrar una
tarjeta engañosa.

## Radio

`/radio` sin argumentos abre el menú: **🌍 Países**, **🎵 Géneros** y, al pie,
**📻 Inicio**, **🔥 Destacadas** y **❤️ Favoritas**.

| Opción | Qué hace |
|--------|----------|
| 🌍 Países | El catálogo completo, ordenado por cercanía a la PC. El país de su equipo aparece primero: el bot lo deduce de la zona horaria de Windows, sin preguntarle nada |
| 🎵 Géneros | Los géneros de las emisoras de ese país, con los que ya sonaron en la sala primero. Los nombres van en español |
| 🔥 Destacadas | Las emisoras más votadas del catálogo |
| ❤️ Favoritas | Su lista propia: el ♥ de cada fila guarda o quita la emisora, y la lista sobrevive a los reinicios |

Al elegir una emisora, el logo ocupa la pantalla (los streams de radio son
solo audio) y la tarjeta muestra lo que está sonando. ⏭ y ⏮ rotan dentro de la
lista desde la que salió, con vuelta al principio; si el stream se corta, el
bot gira a la siguiente emisora de esa misma lista y nunca cae en la radio de
YouTube. Sin una lista de referencia (por ejemplo, después de reiniciar) avisa
que abra `/radio` para seguir rotando en vez de inventar el siguiente.

`/radio pais:<país>`, `/radio genero:<género>` y `/radio favoritas` siguen
funcionando si prefiere escribirlo.

## Tarjeta de reproducción

Cuando suena algo aparece una tarjeta con botones:

| Botón | Qué hace |
|-------|---------|
| ⏮ | Canción anterior |
| ▶/⏸ | Un solo botón con tres funciones: sin nada sonando carga la canción actual; sonando pone en pausa; en pausa (o apagado el mpv, tras el temporizador) sigue donde quedó |
| ⏭ | Siguiente canción del mismo artista |
| ⏹ | Detiene: rebobina a 0 y el próximo ▶ arranca desde el inicio |
| 🔉 / 🔊 | Bajar o subir volumen 10 puntos (el nivel actual muestra 🔇 en 0) |
| 📋 | Ver la lista |
| ⚙️ Ajustes | Abre el menú de configuración: 💤 apagado programado (dj), ⚙️ Calidad y 👥 Usuarios (solo admin), una opción por fila. El 💤 cierra todo a la hora elegida (mpv, bot y app) sin avisar en el chat; al día siguiente, abrir la app, Conectar y dar ▶ sigue en el minuto donde quedó |

La radio pasa automáticamente a la siguiente canción del mismo artista cuando
termina. Use `/buscar` con otro nombre para cambiar de cantante.

La tarjeta queda siempre como el último mensaje del chat: cuando usted escribe
un comando o un mensaje, la tarjeta se desvanece y reaparece abajo, al final
de la conversación.

Cuando `/buscar` muestra los resultados, la tarjeta **no se mueve**: queda arriba
y el listado aparece debajo. El listado es **un solo mensaje de texto** con el
encabezado "Página N" y una línea por canción (nombre y duración), porque una
foto por resultado se veía diminuta en el celular. Abajo están los botones para
ir a la página anterior, cancelar y ver la siguiente. Si no le gusta ninguna,
toque **❌ Cancelar** y el listado se desvanece, quedando de nuevo la tarjeta a
la vista. Al elegir una canción, la lista se borra y la tarjeta muestra la
nueva.

El 📋 abre la lista de lo que está en cola. Desde ahí, el botón 🕘 abre el
historial y el bot pregunta cuál: **📻 Emisoras (N)** o **🎵 Canciones (N)**.
Tocar una fila la vuelve a reproducir y ⏭ / ⏮ caminan por ese historial; al
llegar al extremo más nuevo, una emisora abre la radio de su país y una canción
vuelve al buscador con ese artista. Repetir un tema no lo duplica: lo mueve al
tope.

Si al filtrar por artista no queda ninguna canción de ese artista, el bot **no
esconde nada**: lo avisa y muestra las que encontró. Entre una versión original
y una cover el texto es idéntico, así que no hay forma honesta de distinguirlas
y prometer "lo más parecido" sería mentir.

## Calidad de video

En ⚙️ Ajustes, el botón `⚙️ Calidad` muestra la resolución actual (por
defecto **1080p**). Todos lo ven, pero solo un **admin** puede tocarlo: abre
una lista de niveles (144, 240, 360, 480, 720, 1080) y basta tocar uno para
aplicarlo de inmediato: la canción recarga con la nueva resolución.

La calidad es un **tope máximo**: si un video no tiene la resolución elegida,
usa la mayor que no la supere (1080 es el máximo soportado). El cambio queda
**guardado** en la PC (estado del bot) y sobrevive a reinicios.

En ⚙️ Ajustes, el botón `👥 Usuarios` abre la gestión de roles. Todos lo ven,
pero solo un **admin** puede tocarlo: la lista de usuarios llega a su **chat
privado** (en el grupo no aparece nada). Tocar un usuario alterna su rol entre
`user` y `dj` (se ve en vivo en la botonera); el botón `❌` aplica todos los
cambios y avisa a cada usuario de su rol nuevo. El rol `admin` no se cambia
desde la lista.

## Comandos

| Comando | Rol | Descripción |
|---------|-----|-------------|
| `/buscar <artista> - <canción>` | dj, admin | Busca y reproduce del artista indicado |
| `/buscar <artista>` | dj, admin | Busca un artista (sin canción específica) |
| `/buscar <link>` | dj, admin | Reproduce un link de YouTube directo |
| `/reglas` | admin | Re-envía y fija (pin) el mensaje de reglas del grupo |
| `/radio` | dj, admin | Abre el listado de emisoras y favoritas |
| `/letra` | dj, admin | Letra de lo que está sonando |
| `/salud` | admin | Estado del bot: reproductor, versión de YT-Remote y conexión |
| `/actualizar` | admin | Trae YT-Remote y sus componentes a la versión publicada más nueva (reinicia solo) |

El 📋 (ver lista) y los botones de la tarjeta (▶ ⏮ ⏭ ⏹ 🔊) son de **dj y
admin**; ⚙️ Ajustes abre el menú de configuración: 💤 apagado programado para
**dj** y ⚙️ Calidad con 👥 Usuarios exclusivos de **admin**. Quien no tiene rol
no toca nada: recibe un solo aviso.

## Roles

| Rol | Qué puede hacer |
|-----|----------------|
| admin | Todo, más la calidad (⚙️) y los usuarios (👥) |
| dj | `/buscar`, `/radio`, `/letra` y el control de la reproducción |
| user | Nada: es el invitado que está en el grupo sin permiso |

Por defecto solo el dueño es admin. Para dar acceso a otra persona, use el
botón `👥 Usuarios` de la tarjeta: la lista llega a su chat privado y basta
tocar al usuario para alternarlo entre dj y user. Esa es la única puerta a
dj: quien no tiene rol no puede usar el bot (ni comandos, ni listados, ni
botones) y recibe un solo aviso: "🔒 El bot es solo para dj/admin".

El menú "/" se arma por persona: el invitado solo ve `/start`; el dj ve además
`/buscar`, `/radio` y `/letra`; el admin suma `/reglas`, `/salud` y
`/actualizar`. Si alguien pierde el rol, el bot le retira ese menú.

## Variables de entorno

La ventana del launcher le pide estos datos la primera vez y guarda la
sesión en `session.enc`. Si prefiere configurarlos a mano, copie
`.env.example` y complete:

| Variable | Obligatoria | Cómo obtenerla |
|----------|-------------|----------------|
| `TELEGRAM_TOKEN` | sí | Crear bot con [@BotFather](https://t.me/BotFather) → `/newbot` |
| `OWNER_ID` | sí | Su ID numérico de Telegram con [@userinfobot](https://t.me/userinfobot) |
| `ALLOWED_CHAT_ID` | no | Se configura solo con el primer `/start` del dueño |

Ni `.env` ni `session.enc` se suben a GitHub (están en `.gitignore`). El
`.env.example` muestra el formato y es seguro versionarlo.

## Un solo grupo (chat permitido)

El bot responde solo en **un** chat: el grupo donde el dueño envió el primer
`/start`. Esa configuración se guarda en `ALLOWED_CHAT_ID` (`.env`). Los
mensajes de otros chats (privado u otros grupos) se ignoran; en la consola de
la PC aparece `Rechazado mensaje de chat no permitido (chat_id=...)`.

Para cambiar el grupo permitido:

1. Detenga el bot (`Ctrl+C` en su consola).
2. Obtenga el ID del grupo nuevo: reenvíe un mensaje del grupo a
   [@getidsbot](https://t.me/getidsbot); le responde un número negativo (tipo
   `-100...`).
3. En la carpeta del bot ejecute:
   `runtime\python\python.exe src\setup_cli.py set-chat <id>`
4. Vuelva a abrir `ytremote.exe` y envíe `/start` de nuevo en el grupo.

Si Telegram le avisa que "este bot no puede unirse a grupos", revise los
permisos del bot en [@BotFather](https://t.me/BotFather) (`/setjoingroups`).

## Configuración extra

Edite `config.json` para ajustar:
- `mpv_path` — ruta al ejecutable de mpv
- `default_role` — rol por defecto para usuarios nuevos
- `max_results` — cantidad de resultados al buscar (por defecto 5)
- `kick_after_hours` — horas de tolerancia para invitados (rol `user`). Con un
  valor mayor a 0, el bot fija el mensaje de reglas al iniciar y **retira del
  grupo** (kick, puede volver con el enlace de invitación) a todo usuario que
  supere ese plazo sin rol dj/admin. En `0` (por defecto) la auto-expulsión
  está **desactivada**. Requiere que el bot sea admin del grupo.
  Al unirse, el invitado recibe una bienvenida con el plazo si está activo;
  y al cambiarle el rol, se le avisa por privado (o en el grupo si nunca
  habló con el bot).