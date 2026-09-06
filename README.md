# YT-Remote

Bot de Telegram para controlar la reproducción de videos de YouTube en tu PC.
Búsqueda por chat, tarjeta de control con botones, radio automática por artista.

**Versión actual:** v0.2.0

## Descarga e instalación

Descargá el ZIP desde GitHub, descomprimilo en cualquier carpeta y listo.
No necesitás instalar Python, mpv ni nada externo: todo viene incluido
dentro de la carpeta (es portable de verdad).

1. Descargá el ZIP desde la página de releases de GitHub
2. Descomprimilo donde quieras
3. Doble clic en `ytremote.bat`. La primera vez te guía para configurar
   tu TOKEN y tu ID de Telegram (se guardan en `.env`, no se suben a GitHub)
4. Dejá la ventana abierta
5. En un grupo de Telegram agregá el bot y escribí `/start`

## Cómo buscar

```
/buscar Marcos Witt - Dios de lo Impossible
```

Lo que va **antes del guion** es el **artista** (el bot reproduce canciones
de ese cantante). Lo que va después es la primera canción.

Si no usás el guion, todo el texto se busca como nombre de artista:
```
/buscar Jesus Adrian Romero
```

Si pegás un link de YouTube, reproduce directo sin buscar.

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

La radio pasa automáticamente a la siguiente canción del mismo artista cuando
termina. Usá `/buscar` con otro nombre para cambiar de cantante.

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
botones de la tarjeta (▶ ⏮ ⏭ ⏹ 🔊) son solo para dj y admin.

## Roles

| Rol | Que puede hacer |
|-----|----------------|
| admin | Todo + dar/quitar acceso a otros |
| dj | Buscar, controlar reproduccion (tarjeta y comandos) |
| user | Solo ver la lista con /lista o el boton 📋 |

Por defecto solo el dueno (vos) es admin. Usá `/adduser` con el ID de
Telegram de la otra persona para darle acceso dj o admin.

Un user puede escribir `/solicitar` para pedir acceso de dj al admin.

## Variables de entorno

La primera vez que ejecutás `ytremote.bat`, el wizard te guía para crear
el archivo `.env` con tus datos. También podés crearlo a mano copiando
`.env.example` y completando:

| Variable | Obligatoria | Cómo obtenerla |
|----------|-------------|----------------|
| `TELEGRAM_TOKEN` | sí | Crear bot con [@BotFather](https://t.me/BotFather) → `/newbot` |
| `OWNER_ID` | sí | Tu ID numérico de Telegram con [@userinfobot](https://t.me/userinfobot) |
| `ALLOWED_CHAT_ID` | no | Se configura solo con el primer `/start` del dueño |

El `.env` **no se sube a GitHub** (está en `.gitignore`). El `.env.example`
muestra el formato y es seguro versionarlo.

## Configuración extra

Editá `config.json` para ajustar:
- `mpv_path` — ruta al ejecutable de mpv
- `default_role` — rol por defecto para usuarios nuevos
- `max_results` — cantidad de resultados al buscar (por defecto 5)
