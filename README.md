# YT-Remote

Bot de Telegram para controlar la reproducción de videos de YouTube en tu PC.
Búsqueda por chat, tarjeta de control con botones, radio automática por artista.

## Instalación

1. Copia o clona esta carpeta a tu PC
2. Doble clic en `ytremote.bat`. La primera vez te guía para configurar
   tu TOKEN y tu ID de Telegram (se guardan en `.env`, no se suben a GitHub)
3. Ejecuta `ytremote.bat` para arrancar y déjalo abierto
4. En un grupo de Telegram agrega el bot y escribe `/start`

## Cómo buscar

```
/buscar Kent Leroy - Dame Jesús
```

Lo que va **antes del guion** es el **artista** (el bot reproduce canciones de ese cantante). Lo que va después es la primera canción.

Si no usas el guion, todo el texto se busca como nombre de artista:
```
/buscar Mafe Restrepo
```

Si pegas un link de YouTube, reproduce directo sin buscar.

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

La radio pasa automáticamente a la siguiente canción del mismo artista cuando termina. Usa `/buscar` con otro nombre para cambiar de cantante.

## Comandos

| Comando | Rol | Descripción |
|---------|-----|-------------|
| `/buscar <artista> - <canción>` | user | Busca y reproduce del artista indicado |
| `/buscar <artista>` | user | Busca un artista (sin canción específica) |
| `/buscar <link>` | user | Reproduce un link de YouTube directo |
| `/lista` | user | Ver la lista; `/lista N` salta al tema N |
| `/now` | user | Qué está sonando |
| `/pause` | dj | Pausar |
| `/resume` | dj | Reanudar |
| `/next` | dj | Siguiente canción |
| `/stop` | dj | Detener y limpiar todo |
| `/volume <0-100>` | dj | Ajustar volumen |
| `/adduser <id> <rol>` | admin | Dar acceso (rol: admin, dj, user) |
| `/removeuser <id>` | admin | Quitar acceso |

## Roles

| Rol | Qué puede hacer |
|-----|---------------|
| admin | Todo, incluyendo dar/quitar permisos |
| dj | Reproducir, pausar, saltar, ajustar volumen |
| user | Solo pedir canciones y ver la lista |

Por defecto solo el dueño (vos) es admin. Usá `/adduser` con el ID de Telegram de la otra persona para darle acceso.

## Configuración extra

Editá `config.json` para ajustar:
- `mpv_path` — ruta al ejecutable de mpv
- `default_role` — rol por defecto para usuarios nuevos
- `max_results` — cantidad de resultados al buscar (por defecto 5)
