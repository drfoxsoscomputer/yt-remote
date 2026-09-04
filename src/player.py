"""Control del reproductor mpv via IPC.

En Windows, mpv expone un named pipe con --input-ipc-server y acepta
comandos JSON por linea. Este modulo lanza mpv y le envia comandos.
"""

import asyncio
import json
import subprocess

MPV_PIPE = r"\\.\pipe\mpv-ytremote"


class Player:
    """Wrapper asincrono sobre el proceso mpv y su IPC."""

    def __init__(self, mpv_path: str = "mpv") -> None:
        self.mpv_path = mpv_path
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = asyncio.Lock()

    @property
    def is_playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def start(self) -> None:
        """Lanza mpv en segundo plano con IPC habilitado."""
        if self.is_playing:
            return
        self._proc = subprocess.Popen(
            [
                self.mpv_path,
                "--input-ipc-server=" + MPV_PIPE,
                "--terminal=no",
                "--really-quiet",
                "--idle=yes",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def _send_raw(self, data: str) -> None:
        """Escribe una linea de comando JSON en el pipe de mpv."""
        await self._lock.acquire()
        try:
            pipe = open(MPV_PIPE, "w", encoding="utf-8")
            try:
                pipe.write(data + "\n")
                pipe.flush()
            finally:
                pipe.close()
        finally:
            self._lock.release()

    async def command(self, command: str, *args) -> None:
        """Envia un comando mpv (ej: 'play', 'pause', 'cycle')."""
        payload = json.dumps({"command": [command, *args]})
        await self._send_raw(payload)

    async def load(self, url: str) -> None:
        """Carga y reproduce un URL/stream en mpv."""
        await self.command("loadfile", url, "replace")

    async def play(self) -> None:
        await self.command("set", "pause", "no")

    async def pause(self) -> None:
        await self.command("set", "pause", "yes")

    async def toggle_pause(self) -> None:
        await self.command("cycle", "pause")

    async def stop(self) -> None:
        await self.command("stop")
        await self.command("playlist-clear")

    async def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, volume))
        await self.command("set", "volume", str(volume))

    async def quit(self) -> None:
        if self.is_playing:
            await self.command("quit")
            self._proc = None
