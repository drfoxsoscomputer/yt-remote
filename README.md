# YT-Remote

Bot de Telegram para controlar la reproducción de videos de YouTube.
Búsqueda privada en el chat, video en la PC donde corre el programa.

## Requisitos

- Python 3.10+
- mpv (`winget install mpv`)
- Un bot token de Telegram (creado con @BotFather)

## Instalación

1.  Clona o copia esta carpeta a tu PC
2.  Ejecuta `ytremote.bat`. La primera vez te guía para configurar tu
    TOKEN y tu ID (que quedan guardados en `.env`, sin subirse a GitHub)
3.  Si necesitas ajustes extra, edita `config.json` (`mpv_path`,
    `default_role`, `max_results`)
4.  Ejecuta `ytremote.bat` para arrancar el bot y déjalo abierto
5.  En un grupo de Telegram agrega el bot y mándale `/start`

## Uso

| Comando | Rol | Descripción |
|---------|-----|-------------|
| `/play <búsqueda>` | user | Busca en YouTube y muestra opciones con thumbnail |
| `/play <link>` | user | Reproduce directo desde link de YouTube |
| `/pause` | dj | Pausa |
| `/resume` | dj | Reanuda |
| `/next` | dj | Siguiente en la cola |
| `/prev` | dj | Anterior |
| `/stop` | dj | Para y limpia la cola |
| `/queue` | user | Ver cola actual |
| `/now` | user | Ver qué está sonando |
| `/volume <0-100>` | dj | Ajusta el volumen |
| `/adduser @user <rol>` | admin | Asigna rol (admin/dj/user) |
| `/removeuser @user` | admin | Quita acceso |

## Roles

| Rol | Permisos |
|-----|----------|
| admin | Todo |
| dj | Play, pause, skip, cola, volumen |
| user | Solo pedir canciones |