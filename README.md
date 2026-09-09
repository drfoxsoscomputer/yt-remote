# YT-Remote

Bot de Telegram para controlar la reproducción de videos de YouTube en su PC.
Búsqueda por chat, tarjeta de control con botones, radio automática por artista.

**Versión actual:** v0.3.1

## Descarga e instalación

Descargue el ZIP desde GitHub, descomprímalo en cualquier carpeta y listo.
No necesita instalar Python, mpv ni nada externo: todo viene incluido
dentro de la carpeta (es portable de verdad).

1. Descargue el ZIP desde la página de releases de GitHub
2. Descomprímalo donde quiera
3. Doble clic en `ytremote.bat`. La primera vez le guía para configurar
   su TOKEN y su ID de Telegram (se guardan en `.env`, no se suben a GitHub)
4. Deje la ventana abierta
5. En un grupo de Telegram agregue el bot y escriba `/start`

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
demuxer de video ampliado (`file, http, https`): sin eso, el playlist local que
une video y audio no podía bajar sus sub-playlists https y mpv quedaba en
"Drop files to play here". Si un directo no logra cargar, el bot avisa
"No se pudo reproducir" en vez de mostrar una tarjeta engañosa.

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

## Comandos

| Comando | Rol | Descripcion |
|---------|-----|-------------|
| `/buscar <artista> - <cancion>` | dj, admin | Busca y reproduce del artista indicado |
| `/buscar <artista>` | dj, admin | Busca un artista (sin cancion especifica) |
| `/buscar <link>` | dj, admin | Reproduce un link de YouTube directo |
| `/adduser <id> <rol>` | admin | Dar acceso (dj, admin) |
| `/removeuser <id>` | admin | Quitar acceso |
| `/solicitar` | user | Solicitar acceso de dj al admin |

**Todos los roles** pueden tocar el boton 📋 (ver lista). Los demas
botones de la tarjeta (▶ ⏮ ⏭ ⏹ 🔊) son solo para dj y admin; el boton
⚙️ Calidad es exclusivo de **admin**.

## Roles

| Rol | Que puede hacer |
|-----|----------------|
| admin | Todo + dar/quitar acceso a otros |
| dj | Buscar, controlar reproduccion (tarjeta y comandos) |
| user | Solo ver la lista con el boton 📋 (sin elegir temas) |

Por defecto solo el dueño es admin. Use `/adduser` con el ID de
Telegram de la otra persona para darle acceso dj o admin.

Un user puede escribir `/solicitar` para pedir acceso de dj al admin.

## Variables de entorno

La primera vez que ejecute `ytremote.bat`, el wizard le guía para crear
el archivo `.env` con sus datos. También puede crearlo a mano copiando
`.env.example` y completando:

| Variable | Obligatoria | Cómo obtenerla |
|----------|-------------|----------------|
| `TELEGRAM_TOKEN` | sí | Crear bot con [@BotFather](https://t.me/BotFather) → `/newbot` |
| `OWNER_ID` | sí | Su ID numérico de Telegram con [@userinfobot](https://t.me/userinfobot) |
| `ALLOWED_CHAT_ID` | no | Se configura solo con el primer `/start` del dueño |

El `.env` **no se sube a GitHub** (está en `.gitignore`). El `.env.example`
muestra el formato y es seguro versionarlo.

## Configuración extra

Edite `config.json` para ajustar:
- `mpv_path` — ruta al ejecutable de mpv
- `default_role` — rol por defecto para usuarios nuevos
- `max_results` — cantidad de resultados al buscar (por defecto 5)