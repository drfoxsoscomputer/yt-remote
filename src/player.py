"""Control del reproductor mpv via IPC.

En Windows, mpv expone un named pipe con --input-ipc-server y acepta
comandos JSON por linea. Este modulo lanza mpv y le envia comandos.
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

MPV_PIPE = r"\\.\pipe\mpv-ytremote"

# Segundos que esperamos que mpv cree el named pipe antes de rendirnos.
_PIPE_WAIT_TIMEOUT = 15.0
# Timeout para cada escritura en el pipe (evita colgar el bot para siempre).
_SEND_TIMEOUT = 10.0


def _mpv_env() -> dict[str, str]:
    """Variables de entorno para mpv.

    mpv necesita encontrar yt-dlp.exe para resolver videos de YouTube.
    El unico yt-dlp del proyecto vive en runtime\\python\\Scripts, asi que
    se lo agregamos al PATH del proceso mpv (queda portable, sin depender
    de un yt-dlp instalado en el sistema).
    """
    env = os.environ.copy()
    project_root = Path(__file__).resolve().parent.parent
    scripts_dir = str(project_root / "runtime" / "python" / "Scripts")
    old_path = env.get("PATH", "")
    env["PATH"] = scripts_dir + os.pathsep + old_path
    return env


class Player:
    """Wrapper asincrono sobre el proceso mpv y su IPC."""

    def __init__(self, mpv_path: str = "mpv") -> None:
        self.mpv_path = mpv_path
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = asyncio.Lock()

    @property
    def is_playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _pipe_ready(self) -> bool:
        """True si el named pipe de mpv ya existe en el sistema."""
        return os.path.exists(MPV_PIPE)

    async def start(self) -> None:
        """Lanza mpv en segundo plano con IPC habilitado y espera su pipe."""
        if self.is_playing:
            return

        await asyncio.to_thread(self._launch_mpv)
        if self._proc is None:
            return

        # Esperar a que mpv cree el pipe (no bloquear el event loop).
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._wait_pipe),
                timeout=_PIPE_WAIT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            # mpv no creo el pipe: matar el proceso para no quedar colgado.
            await asyncio.to_thread(self._terminate)
            self._proc = None
            raise RuntimeError("mpv no inicio correctamente (no creo el IPC).")

    def _launch_mpv(self) -> None:
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
            env=_mpv_env(),
        )

    def _wait_pipe(self) -> None:
        deadline = time.monotonic() + _PIPE_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            if self._pipe_ready():
                return
            if self._proc is not None and self._proc.poll() is not None:
                # mpv salio antes de crear el pipe.
                return
            time.sleep(0.1)

    def _terminate(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()

    def _write_sync(self, data: str) -> None:
        """Escribe una linea en el pipe. Se ejecuta en un thread."""
        with open(MPV_PIPE, "w", encoding="utf-8") as pipe:
            pipe.write(data + "\n")
            pipe.flush()

    async def _send_raw(self, data: str) -> None:
        """Escribe una linea de comando JSON en el pipe de mpv.

        El I/O del pipe se ejecuta en un thread para no bloquear el
        event loop de Telegram, con un timeout para que nunca se cuelgue.
        """
        async with self._lock:
            if not self._pipe_ready():
                return
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._write_sync, data),
                    timeout=_SEND_TIMEOUT,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "MPV no responde (timeout escribiendo al pipe)."
                )

    async def command(self, command: str, *args) -> None:
        """Envia un comando mpv (ej: 'play', 'pause', 'cycle')."""
        payload = json.dumps({"command": [command, *args]})
        await self._send_raw(payload)

    async def load(self, url: str, audio_url: str | None = None) -> None:
        """Carga y reproduce un URL/stream en mpv."""
        await self.command("loadfile", url, "replace")
        if audio_url:
            await self.command("audio-add", audio_url, "select")

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
