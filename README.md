# YT-Remote

Bot de Telegram para controlar la reproduccion de videos de YouTube en tu TV
(conectado por HDMI a tu PC). Busqueda privada en el chat, video en pantalla
completa en el TV.

## Requisitos

- Python 3.10+
- mpv (`winget install mpv`)
- Un bot token de Telegram (creado con @BotFather)

## Instalacion

1.  Clona o copia esta carpeta a tu PC
2.  Ejecuta `setup.bat` (crea el entorno virtual e instala dependencias)
3.  Configura tu token en `config.json`
4.  Ejecuta `start.bat`

## Uso

| Comando | Rol | Descripcion |
|---------|-----|-------------|
| `/play <busqueda>` | user | Busca en YouTube y muestra opciones con thumbnail |
| `/play <link>` | user | Reproduce directo desde link de YouTube |
| `/pause` | dj | Pausa |
| `/resume` | dj | Reanuda |
| `/next` | dj | Siguiente en la cola |
| `/prev` | dj | Anterior |
| `/stop` | dj | Para y limpia la cola |
| `/queue` | user | Ver cola actual |
| `/now` | user | Ver que esta sonando |
| `/volume <0-100>` | dj | Ajusta volumen |
| `/adduser @user <rol>` | admin | Asigna rol (admin/dj/user) |
| `/removeuser @user` | admin | Quita acceso |

## Roles

| Rol | Permisos |
|-----|----------|
| admin | Todo |
| dj | Play, pause, skip, cola, volumen |
| user | Solo pedir canciones |
