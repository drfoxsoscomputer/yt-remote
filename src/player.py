"""Control del reproductor mpv via IPC.

En Windows, mpv expone un named pipe con --input-ipc-server y acepta
comandos JSON por linea. Este modulo lanza mpv y le envia comandos.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

MPV_PIPE = r"\\.\pipe\mpv-ytremote"

# Nombre del master .m3u8 temporal que une video+audio de un directo HLS.
# Se sobrescribe en cada load: no se acumulan archivos.
_LIVE_MASTER_NAME = "ytremote_live.m3u8"


def _is_hls_url(url: str) -> bool:
    """True si la URL apunta a un playlist HLS (.m3u8), p.ej. un directo."""
    return ".m3u8" in url.lower()


def _build_master_playlist(
    video_url: str,
    audio_url: str,
    *,
    bandwidth: int = 0,
    codecs: str = "avc1.64001F,mp4a.40.2",
    resolution: str = "",
) -> str:
    """Construye un master HLS minimo: video child como unica variante y
    audio child como su rendicion EXT-X-MEDIA (mismo grupo, DEFAULT=YES).

    mpv/ffmpeg reciben video y audio juntos en UN demuxer HLS y los
    sincronizan segun el spec (live edge compartido). Sin esto, cada
    sub-playlist de un directo se carga con su propio live edge y el
    audio queda permanentemente desfasado del video.
    """
    group = "yt-remote-audio0"
    lines = [
        "#EXTM3U",
        f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{group}",NAME="Default",'
        f'DEFAULT=YES,AUTOSELECT=YES,URI="{audio_url}"',
    ]
    attrs = [f'CODECS="{codecs}"', f"BANDWIDTH={bandwidth}", f'AUDIO="{group}"']
    if resolution:
        attrs.append(f"RESOLUTION={resolution}")
    lines.append("#EXT-X-STREAM-INF:" + ",".join(attrs))
    lines.append(video_url)
    return "\n".join(lines)

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
        self._reader_task: asyncio.Task[None] | None = None
        self._on_track_ended: Callable[[], None] | None = None
        # Flag de track activo: lo actualiza el reader thread (fin de cancion
        # -> False) y los metodos load/play (-> True). Permite a is_playing
        # distinguir "proceso vivo" de "hay un track realmente reproduciendose".
        self._track_active = False
        # Historial de load() para debugging/testing: lista de (url, audio_url).
        self._loaded: list[tuple[str, str | None]] = []
        # Se dispara cuando mpv termina de cargar un track (evento
        # file-loaded). load() lo espera ANTES del audio-add: si el comando
        # llega mientras el video se esta cargando, mpv descarta la pista de
        # audio al completar file-loaded => video sin sonido.
        self._file_loaded_event = threading.Event()
        # Se marca cuando el reader thread abrio el pipe, se suscribio a
        # eof-reached y entro en bucle de lectura. start() espera este evento
        # antes de retornar para no descartar los primeros comandos.
        self._pipe_ready_event = threading.Event()
        # Solo debug: lista de eventos crudos vistos por el reader.
        self._debug_events: list[object] = []
        # Evita iniciar el reader thread mas de una vez por proceso mpv.
        self._reader_started = False
        # Respuestas a comandos con request_id (ver _request/_dispatch).
        # mpv responde {"request_id": N, "data": ...}; el reader las captura
        # aqui para que load() pueda VERIFICAR el track-list (audio presente)
        # en vez de mandar el audio-add a ciegas.
        self._request_events: dict[int, threading.Event] = {}
        self._request_results: dict[int, object] = {}
        self._request_id = 0
        self._lock_req = threading.Lock()

    @property
    def is_playing(self) -> bool:
        """True si hay un proceso mpv vivo Y un track activo cargado.

        Tras 'end-file' mpv queda vivo pero sin track; _track_active pasa a
        False y is_playing devuelve False, lo que permite al auto-advance
        reproducir el siguiente item en vez de encolarlo.
        """
        return (
            self._proc is not None
            and self._proc.poll() is None
            and self._track_active
        )

    @property
    def is_running(self) -> bool:
        """True si hay un proceso mpv vivo (aunque no tenga track cargado)."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def loaded(self) -> list[tuple[str, str | None]]:
        """Lista de (url, audio_url) cargados. Para debugging/testing."""
        return self._loaded

    def _pipe_ready(self) -> bool:
        """True si el named pipe de mpv ya existe en el sistema."""
        return os.path.exists(MPV_PIPE)

    async def start(self) -> None:
        """Garantiza un proceso mpv vivo con IPC y lector de eventos.

        Si mpv ya esta corriendo (ej: quedó en idle tras un end-file),
        NO se relanza: reutiliza el proceso y solo asegura el reader.
        """
        if self.is_playing:
            return
        if self._proc is not None and self._proc.poll() is None:
            self._start_reader()
            if not self._pipe_ready_event.is_set():
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._pipe_ready_event.wait),
                        timeout=_PIPE_WAIT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    pass
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

        # Iniciar lector background de eventos del pipe (auto-advance, watchdog).
        # El reader mantiene UN SOLO handle r+ y al abrirlo se suscribe a
        # eof-reached, de modo que los property-change lleguen al cliente
        # que tambien lee los eventos (cliente unico compartido).
        self._start_reader()

        # Esperar a que el reader thread abra el pipe y este listo para
        # recibir comandos (evita descartar load/play por _pipe None).
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._pipe_ready_event.wait),
                timeout=_PIPE_WAIT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await asyncio.to_thread(self._terminate)
            self._proc = None
            raise RuntimeError("mpv no abrio el pipe IPC a tiempo.")

    def _launch_mpv(self) -> None:
        self._proc = subprocess.Popen(
            [
                self.mpv_path,
                "--input-ipc-server=" + MPV_PIPE,
                "--terminal=no",
                "--really-quiet",
                "--idle=yes",
                # Un master .m3u8 LOCAL (directos HLS) se apertura con el
                # demuxer lavf, que por defecto restringe los protocolos a
                # 'file,crypto,data': los childs https quedarian bloqueados.
                # Se amplia el whitelist para que el demuxer pueda bajarlos.
                "--demuxer-lavf-o=protocol_whitelist=[file,http,https,tcp,tls,crypto,data]",
                # La ventana NO se cierra al terminar la cancion:
                # keep-open pausa en el ultimo frame (no emite end-file),
                # force-window mantiene la ventana base siempre visible.
                "--keep-open=yes",
                "--force-window=yes",
                # Siempre arranca en pantalla completa (desde Telegram).
                # Si el usuario en la PC la cambia, no se fuerza de vuelta.
                "--fullscreen=yes",
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

    def _start_reader(self) -> None:
        """Abre el named pipe en modo r+ (un solo cliente) y lee eventos.

        mpv envia JSON lines por el input-ipc-server. Con keep-open el fin de
        cancion no emite end-file; en su lugar la propiedad eof-reached pasa a
        true y mpv emite un 'property-change' AL CLIENTE QUE SE SUSCRIBIO.
        Por eso reader y writer comparten el MISMO handle: el reader abre el
        pipe, se suscribe a eof-reached por el mismo, y guarda el handle para
        que los comandos se escriban por el mismo cliente.
        Solo se inicia una vez por proceso mpv.
        """
        if self._reader_started:
            return
        self._reader_started = True
        import threading

        def _reader_loop() -> None:
            try:
                with open(MPV_PIPE, "r+", encoding="utf-8", errors="replace") as pipe:
                    # El reader abre SU PROPIO handle. El subscribe a
                    # eof-reached se hace por ESTE handle: mpv emite los
                    # property-change de esa propiedad al cliente que se
                    # suscribio, asi llegan aqui donde se leen los eventos.
                    # Los comandos load/play/etc. van por OTRO handle (ver
                    # _write_sync) que SE ABRE Y CIERRA por comando.
                    try:
                        pipe.write(
                            json.dumps({"command": ["observe_property", 1, "eof-reached"]}) + "\n"
                        )
                        pipe.flush()
                    except Exception:
                        pass
                    # El pipe esta listo: desbloquear start().
                    self._pipe_ready_event.set()

                    for line in pipe:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if getattr(self, "_debug_events", None) is not None:
                            self._debug_events.append(ev)
                        # Respuesta a un comando con request_id (ver _request):
                        # capturar el 'data' y despertar al que espera.
                        if isinstance(ev, dict) and ev.get("request_id") is not None:
                            rid = ev["request_id"]
                            ev_obj = self._request_events.get(rid)
                            if ev_obj is not None:
                                self._request_results[rid] = ev.get("data")
                                ev_obj.set()
                                self._request_events.pop(rid, None)
                        if isinstance(ev, dict) and ev.get("event") == "file-loaded":
                            # mpv termino de cargar el track: load() lo espera
                            # antes de mandar el audio-add (ver _file_loaded_event).
                            self._file_loaded_event.set()
                        # mpv emits 'end-file' when the current track finishes.
                        if isinstance(ev, dict) and ev.get("event") == "end-file":
                            # El track termino: el player deja de estar activo,
                            # permitiendo al auto-advance reproducir el siguiente.
                            self._track_active = False
                            # Solo un fin NATURAL (eof) dispara el avance.
                            # 'stop' (comando /stop) o 'replace' (cambio de
                            # tema) NO deben encadenar el siguiente tema.
                            if ev.get("reason") == "eof":
                                try:
                                    if self._on_track_ended:
                                        self._on_track_ended()
                                except Exception:
                                    pass
                            # only one end-file per track, continue listening
                        # Con keep-open mpv NO descarga el archivo al terminar,
                        # asi que end-file no llega; en su lugar la propiedad
                        # eof-reached pasa a true (property-change).
                        if (
                            isinstance(ev, dict)
                            and ev.get("event") == "property-change"
                            and ev.get("name") == "eof-reached"
                            and ev.get("data") is True
                        ):
                            self._track_active = False
                            try:
                                if self._on_track_ended:
                                    self._on_track_ended()
                            except Exception:
                                pass
            except FileNotFoundError:
                # pipe gone (mpv stopped); ignore and exit thread
                pass
            except Exception:
                pass
            finally:
                self._reader_started = False
                self._pipe_ready_event.clear()

        thread = threading.Thread(target=_reader_loop, daemon=True)
        thread.start()

    def _terminate(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()

    def stop_reader(self) -> None:
        """Request the reader thread to stop (it will exit naturally on next iteration)."""
        # We use a simple approach: the reader loop checks _proc, and when the
        # process dies the pipe becomes unavailable and the thread exits.
        # No explicit signal needed for our simple use case.
        pass

    def _quit(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
        self._track_active = False
        self.stop_reader()

    def _write_sync(self, data: str) -> None:
        """Abre su propio handle, escribe una linea y lo cierra.

        El reader mantiene SU propio handle (donde se suscribio a
        eof-reached). Los comandos van por un handle EFIMERO aparte: en
        Windows escribir y leer por el mismo handle de un named pipe hace que
        la lectura se cuelgue, asi que cada comando abre y cierra su conexion.
        """
        try:
            with open(MPV_PIPE, "w", encoding="utf-8") as pipe:
                pipe.write(data + "\n")
                pipe.flush()
        except (ValueError, OSError, FileNotFoundError):
            # Pipe cerrado (mpv termino): no hay nada que escribir.
            pass

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

    async def _request(self, command: list) -> object | None:
        """Envia un comando con request_id y espera la respuesta del reader.

        El reader captura la respuesta y la deja en _request_results.
        Devuelve el 'data' de la respuesta o None si no llego.
        """
        with self._lock_req:
            self._request_id += 1
            rid = self._request_id
        payload = {"command": command, "request_id": rid}
        ev = threading.Event()
        self._request_events[rid] = ev
        try:
            await self._send_raw(json.dumps(payload))
        except RuntimeError:
            self._request_events.pop(rid, None)
            return None
        try:
            await asyncio.wait_for(asyncio.to_thread(ev.wait), timeout=3.0)
        except asyncio.TimeoutError:
            self._request_events.pop(rid, None)
            self._request_results.pop(rid, None)
            return None
        return self._request_results.pop(rid, None)

    async def command(self, command: str, *args) -> None:
        """Envia un comando mpv (ej: 'play', 'pause', 'cycle')."""
        payload = json.dumps({"command": [command, *args]})
        await self._send_raw(payload)

    async def load(self, url: str, audio_url: str | None = None) -> None:
        """Carga y reproduce un URL/stream en mpv.

        Si hay stream de audio separado (DASH), espera a que mpv termine de
        cargar el video (file-loaded) ANTES del audio-add: si el comando llega
        mientras el video se carga, mpv descarta la pista al completar el load
        y queda video sin sonido.

        Un DIRECTO HLS es distinto: video y audio llegan como sub-playlists
        independientes (loadfile + audio-add dejaba cada una con su propio
        live edge => audio desfasado). Se construye un master .m3u8 temporal
        que las une en un solo demuxer HLS y mpv sincroniza A/V nativamente.

        Tras el audio-add VERIFICA con 'track-list' que haya una pista de
        audio con 'selected'; si no, lo reintenta una vez mas. Asi los videos
        sin sonido no pasan sin diagnostico.

        Si mpv NO confirma el file-loaded dentro del timeout, el load fallo
        (mpv quedo en idle): se lanza RuntimeError para que el bot muestre un
        error real en vez de una tarjeta "Sonando" sin miniatura ni sonido.
        """
        self._file_loaded_event.clear()
        self._loaded.append((url, audio_url))
        if audio_url and _is_hls_url(url) and _is_hls_url(audio_url):
            # Directo HLS: master local que une video + audio en un solo
            # demuxer (sync A/V nativo mpv). Si no se puede escribir el
            # archivo, se cae al camino DASH (loadfile child + audio-add).
            master = Path(tempfile.gettempdir()) / _LIVE_MASTER_NAME
            try:
                master.write_text(
                    _build_master_playlist(url, audio_url), encoding="utf-8"
                )
            except OSError:
                pass
            else:
                url = master.as_posix()
                audio_url = None
        await self.command("loadfile", url, "replace")
        # Event.wait(timeout) devuelve False al vencer (no usar wait_for +
        # to_thread: un thread bloqueado en Event.wait no se cancela y colgaria).
        if not await asyncio.to_thread(
            self._file_loaded_event.wait, _PIPE_WAIT_TIMEOUT
        ):
            # Sin file-loaded no hubo track: mpv quedo en idle. No mentir en
            # la tarjeta: el bot convierte esto en un error visible.
            raise RuntimeError("mpv no confirmo la carga del stream (timeout).")
        if audio_url:
            # DASH: el video ya cargo; agregar el audio separado ahora.
            for attempt in range(2):
                await self.command("audio-add", audio_url, "select")
                await asyncio.sleep(0.3)
                if await self._has_audio_track():
                    break
        self._track_active = True

    async def _has_audio_track(self) -> bool:
        """True si el track-list de mpv muestra al menos una pista de audio."""
        data = await self._request(
            ["get_property", "track-list"]
        )
        if not isinstance(data, list):
            return True  # sin respuesta: no bloquear, asumir que anda
        for track in data:
            if isinstance(track, dict) and track.get("type") == "audio":
                return True
        return False

    async def play(self) -> None:
        await self.command("set", "pause", "no")

    async def pause(self) -> None:
        await self.command("set", "pause", "yes")

    async def toggle_pause(self) -> None:
        await self.command("cycle", "pause")

    async def stop(self) -> None:
        await self.command("stop")
        await self.command("playlist-clear")

    async def rewind(self) -> None:
        """Detiene la reproduccion sin descargar el track: pausa y rebobina a 0:00.

        Mantiene la ventana de mpv y el track cargado, para que al presionar
        ▶ se reanude desde el principio del video actual.
        """
        await self.command("set", "pause", "yes")
        await self.command("seek", "0", "absolute")

    async def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, volume))
        await self.command("set", "volume", str(volume))

    async def quit(self) -> None:
        if self.is_playing:
            await self.command("quit")
            self._proc = None
