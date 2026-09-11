<p align="center">
  <img src="static/img/logo-ytremote.png" alt="YT-Remote" width="180" />
</p>

# YT-Remote

Bot de Telegram para controlar la reproducción de videos de YouTube en su PC.
Búsqueda por chat, tarjeta de control con botones, radio automática por artista.

**Versión actual:** v1.0.0 (launcher con ventana, tarjeta de control con
botones, calidad elegible, playlist con persistencia, directos sincronizados
y expulsión automática de invitados).

## Descarga e instalación

Descargue el ZIP desde GitHub, descomprímalo en cualquier carpeta y listo.
No necesita instalar Python, mpv ni nada externo: todo viene incluido
dentro de la carpeta (es portable de verdad).

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

La ventana de YT-Remote es la puerta de entrada del programa:

| Campo | Qué es |
|-------|--------|
| Token del bot | El código que le dio `@BotFather` al crear el bot |
| ID de admin | Su ID numérico de Telegram (primer usuario con rol admin) |
| Expulsar tras horas | Horas de tolerancia para invitados: `0` desactiva la expulsión |

Botones:

| Botón | Qué hace |
|-------|---------|
| **Conectar** | Guarda la configuración en `session.enc` y arranca el bot |
| **Salir** | Cierra la ventana sin iniciar el bot |
| **Detener** | Detiene el bot sin borrar la configuración guardada |
| **Cerrar sesión** | Borra la configuración y vuelve al formulario vacío |

Al cerrar la ventana con la **X**, el programa pregunta: *"¿Minimizar al lado
del reloj?"* (el bot sigue corriendo) o salir (detiene el bot y cierra). Si
ya hay una sesión guardada, al abrir el programa el bot se conecta solo y
pasa al lado del reloj automáticamente. La configuración queda en
`session.enc`, al lado del ejecutable, y **no se sube a GitHub**.

Solo puede ejecutarse una instancia a la vez: si intenta abrir otra, aparece
un aviso. La ventana requiere el runtime *Microsoft Edge WebView2* (Windows
10/11 suelen traerlo; si falta, el programa indica cómo instalarlo).

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

## Tarjeta de reproducción

Cuando suena algo aparece una tarjeta con botones:

| Botón | Qué hace |
|-------|---------|
| ⏮ | Canción anterior |
| ▶/⏸ | Pausa o reanuda |
| ⏭ | Siguiente canción del mismo artista |
| ⏹ | Detiene todo |
| 🔊−10 / 🔊+10 | Bajar o subir volumen 10 puntos |
| 📋 | Ver la lista |
| ⚙️ Calidad: N | Elegir la resolución de los videos (solo admin) |

La radio pasa automáticamente a la siguiente canción del mismo artista cuando
termina. Use `/buscar` con otro nombre para cambiar de cantante.

La tarjeta queda siempre como el último mensaje del chat: cuando usted escribe
un comando o un mensaje, la tarjeta se desvanece y reaparece abajo, al final
de la conversación.

Cuando `/buscar` muestra los resultados, la tarjeta **no se mueve**: queda arriba
y el listado de 5 canciones aparece debajo. Si no le gusta ninguna, toque
**❌ Cancelar** al final de la lista y esta se desvanecerá, quedando de nuevo la
tarjeta a la vista. Al elegir una canción, la lista se borra y se muestra la
tarjeta nueva con su miniatura.

## Calidad de video

El botón `⚙️ Calidad` de la tarjeta muestra la resolución actual (por
defecto **1080p**). Todos lo ven, pero solo un **admin** puede tocarlo: abre
una lista de niveles (144, 240, 360, 480, 720, 1080) y basta tocar uno para
aplicarlo de inmediato: la canción recarga con la nueva resolución.

La calidad es un **tope máximo**: si un video no tiene la resolución elegida,
usa la mayor que no la supere (1080 es el máximo soportado). El cambio queda
**guardado** en la PC (estado del bot) y sobrevive a reinicios.

El botón `👥 Usuarios` de la tarjeta abre la gestión de roles. Todos lo ven,
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
| `/solicitar` | user | Solicitar acceso de dj al admin |
| `/reglas` | admin | Re-envía y fija (pin) el mensaje de reglas del grupo |

**Todos los roles** pueden tocar el botón 📋 (ver lista). Los demás
botones de la tarjeta (▶ ⏮ ⏭ ⏹ 🔊) son solo para dj y admin; los botones
⚙️ Calidad y 👥 Usuarios son exclusivos de **admin**.

## Roles

| Rol | Qué puede hacer |
|-----|----------------|
| admin | Todo + gestionar usuarios (botón 👥) y calidad (botón ⚙️) |
| dj | Buscar, controlar reproducción (tarjeta y comandos) |
| user | Solo ver la lista con el botón 📋 (sin elegir temas) |

Por defecto solo el dueño es admin. Para dar acceso a otra persona, use el
botón `👥 Usuarios` de la tarjeta: la lista llega a su chat privado y basta
tocar al usuario para alternarlo entre dj y user.

Un user puede escribir `/solicitar` para pedir acceso de dj al admin: al
admin le llega un aviso por privado con la indicación de abrir la lista con
el botón 👥.

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