"""Web App (Mini App) de Telegram para YT-Remote.

Levanta un servidor HTTP local (stdlib) que sirve el HTML de la
Web App y un endpoint /api. La Web App se abre DENTRO de Telegram
y el bot valida los datos con HMAC-SHA256 derivado del token (nunca
se expone el token en el HTML ni en la URL).

La visibilidad publica la da un tunnel de cloudflared PORTABLE
(bin/cloudflared.exe, quick tunnel: sin cuenta, sin registro, sin
configuracion). La URL cambia en cada arranque; el bot la lee y la
inyecta en los botones web_app con la URL fresca.

El servidor HTTP corre en un hilo aparte (ThreadingHTTPServer) para
no congelar el loop asyncio del bot. Las peticiones a /api se
envian al loop del bot con run_coroutine_threadsafe.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
BIN_DIR = PROJECT_ROOT / "bin"
INDEX_HTML = "miniapp.html"

DEFAULT_PORT = 8765
DEFAULT_TUNNEL_TIMEOUT = 45


def derive_validation_key(token: str) -> bytes:
    """Clave de validacion de la Web App (HMAC-SHA256 de 'WebAppData').

    Telegram no usa el token directo: primero se deriva esta clave.
    """
    return hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()


def validate_init_data(init_data: str, token: str) -> dict | None:
    """Valida initData de una Web App y devuelve sus campos.

    initData es un query string firmado por Telegram. Se compara el
    hash en tiempo constante contra el HMAC de los pares en orden
    alfabetico. Devuelve los campos (con "user" parseado) si la firma
    es valida, o None si no lo es.
    """
    if not init_data or not token:
        return None
    fields = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    got = fields.pop("hash", "")
    if not got:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = derive_validation_key(token)
    expected = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(got, expected):
        return None
    user = fields.get("user")
    if user:
        try:
            fields["user"] = json.loads(user)
        except (ValueError, TypeError):
            pass
    return fields


def _json_bytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _make_handler(server: "MiniAppServer"):
    """Handler HTTP atado a una instancia de MiniAppServer."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "MiniApp/1.0"

        def log_message(self, format: str, *args):
            logger.info("miniapp http %s - %s", self.address_string(), format % args)

        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html", "/" + INDEX_HTML):
                index = STATIC_DIR / INDEX_HTML
                if not index.exists():
                    self._send(404, b'{"ok":false,"error":"html no existe"}')
                    return
                self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b'{"ok":false,"error":"no encontrado"}')

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/api":
                self._send(404, b'{"ok":false,"error":"no encontrado"}')
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(400, _json_bytes({"ok": False, "error": f"json invalido: {exc}"}))
                return
            user = validate_init_data(str(data.get("initData", "")), server.token)
            if user is None:
                self._send(401, _json_bytes({"ok": False, "error": "initData invalido"}))
                return
            loop = server.get_loop()
            if loop is None or server.on_api is None:
                self._send(503, _json_bytes({"ok": False, "error": "servidor sin handler"}))
                return
            data["user"] = user.get("user") or {}
            data["auth_date"] = user.get("auth_date", "")
            try:
                future = asyncio.run_coroutine_threadsafe(
                    server.on_api(data), loop
                )
                result = future.result(timeout=server.api_timeout)
                self._send(200, _json_bytes(result))
            except Exception as exc:  # noqa: BLE001 - el detalle va al cliente
                logger.exception("mini app /api fallo")
                self._send(500, _json_bytes({"ok": False, "error": str(exc)}))

    return Handler


class MiniAppServer:
    """Servidor HTTP local de la Web App (loop del bot en otro hilo).

    - GET /            : sirve static/miniapp.html (la Web App).
    - POST /api        : valida initData y llama on_api en el loop del bot.
    """

    def __init__(
        self,
        token: str,
        port: int = DEFAULT_PORT,
        on_api=None,
        loop: asyncio.AbstractEventLoop | None = None,
        api_timeout: int = 120,
    ) -> None:
        self.token = token
        self.port = port
        self.on_api = on_api
        self.api_timeout = api_timeout
        self._loop = loop
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        """Arranca el servidor en un hilo daemon. Devuelve la URL local."""
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _make_handler(self))
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="miniapp-http"
        )
        self._thread.start()
        logger.info("Servidor Web App local: http://127.0.0.1:%s", self.port)
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def get_loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop


class CloudflaredTunnel:
    """Tunnel quick de cloudflared (portable, sin cuenta).

    Lanza bin/cloudflared.exe apuntando a la URL local y espera (con
    timeout) a que imprima la URL http(s) de trycloudflare. El bot usa
    esa URL fresca para los botones web_app.
    """

    URL_REGEX = re.compile(r"https://[\w-]+\.trycloudflare\.com")

    def __init__(
        self,
        local_url: str,
        executable: Path | None = None,
        timeout: int = DEFAULT_TUNNEL_TIMEOUT,
    ) -> None:
        self.local_url = local_url
        self.executable = executable or (BIN_DIR / "cloudflared.exe")
        self.timeout = timeout
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None

    def start(self) -> str | None:
        """Levanta el tunnel y devuelve la URL publica (o None si falla)."""
        exe = str(self.executable)
        if not os.path.exists(exe):
            logger.error("cloudflared no existe en %s", exe)
            return None
        args = [exe, "tunnel", "--url", self.local_url, "--no-autoupdate"]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        url = self._wait_for_url(self.timeout)
        if url:
            self.url = url
            logger.info("Tunnel cloudflared listo: %s", self.url)
        return self.url

    def _wait_for_url(self, timeout: int) -> str | None:
        lines: list[str] = []

        def _read() -> None:
            if self.proc is None or self.proc.stdout is None:
                return
            for line in self.proc.stdout:
                lines.append(line)

        reader = threading.Thread(target=_read, daemon=True, name="cloudflared-out")
        reader.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                logger.error("cloudflared termino antes de dar URL: %r", lines[-3:])
                return None
            for line in lines:
                m = self.URL_REGEX.search(line)
                if m:
                    return m.group(0)
            time.sleep(0.2)
        logger.error("cloudflared no entrego URL en %ss", timeout)
        return None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass


def start_mini_app(
    token: str,
    port: int = DEFAULT_PORT,
    on_api=None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> tuple[MiniAppServer, CloudflaredTunnel, str | None]:
    """Arranca servidor + tunnel y devuelve (server, tunnel, url publica)."""
    server = MiniAppServer(token, port=port, on_api=on_api, loop=loop)
    local = server.start()
    tunnel = CloudflaredTunnel(local)
    public = tunnel.start()
    return server, tunnel, public