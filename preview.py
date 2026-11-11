"""The preview page server on 127.0.0.1:DEVICEDECK_PORT (default 7317). View only.

Routes:
  GET  /, /index.html          the page: every shown device side by side (static/deck.*)
  GET  /deck.css, /deck.js     its assets
  GET  /api/devices            detected devices merged with the user's device list
  GET  /api/config             the user's device list (config.CONFIG_PATH)
  POST /api/config             save it (application/json only); the gear panel and drag-and-drop use this.
                               409 while the current file does not load (it is never overwritten then)
  GET  /frame/<model>.png      bezel image from device_frame.py (<model>.mask.png: display shape)
  GET  /ios/<udid>/...         view-only viewer for one iPhone (viewer_patch.py); only the read-only
                               stream paths in viewer_patch.VIEWER_PROXY_PATHS reach the iPhone's worker

Every request must name this server in Host (stops DNS rebinding). A browser Origin, when sent,
must be this server; "Origin: null" (sandboxed frames, file: pages) passes only for GET and
HEAD. POST /api/config also needs Content-Type application/json, which a cross-site page cannot
send without a CORS preflight this server never grants. Requests without an Origin (non-browser
local clients) are accepted. A malformed request (bad Content-Length, a request line or header
over 64 KiB, too many headers) gets 400. Nothing here sends input to a device.
"""
import asyncio
import contextlib
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import config
import device_frame
import viewer_patch

logger = logging.getLogger("devicedeck.preview")

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 256 * 1024
MAX_HEADERS = 100
UDID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
TYPES = {".html": b"text/html; charset=utf-8", ".css": b"text/css; charset=utf-8",
         ".js": b"application/javascript; charset=utf-8"}


def merge(saved: list[dict[str, Any]], detected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Saved devices in their order (connected or not), then newly detected ones."""
    by_key = {(d["kind"], d["id"]): d for d in detected}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def entry(conf: Optional[dict[str, Any]], found: Optional[dict[str, Any]]) -> dict[str, Any]:
        base = found or {"id": conf["id"], "kind": conf["kind"]}  # type: ignore[index]
        custom = conf.get("name") if conf else None
        detected_name = found.get("name") if found else None
        return {**base, "name": custom or detected_name or base["id"], "custom_name": custom,
                "detected_name": detected_name, "hidden": bool(conf and conf.get("hidden")),
                "connected": found is not None}

    for conf in saved:
        key = (conf["kind"], conf["id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(entry(conf, by_key.get(key)))
    for found in detected:
        key = (found["kind"], found["id"])
        if key not in seen:
            seen.add(key)
            out.append(entry(None, found))
    for i, item in enumerate(out):
        item["order"] = i
    return out


class Preview:
    def __init__(self, hub: Any, bind: str, port: int, panels_port: Optional[int]) -> None:
        self.hub = hub
        self.bind, self.port, self.panels_port = bind, port, panels_port
        host = f"[{bind}]" if ":" in bind else bind
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"{host}:{port}".lower()}
        self.origins = {f"http://{h}" for h in self.hosts}
        self._server: Optional[asyncio.Server] = None
        self._viewer_html = viewer_patch.html()
        self._viewer_js = viewer_patch.js()
        self._viewer_css = viewer_patch.css()

    @property
    def url(self) -> str:
        host = f"[{self.bind}]" if ":" in self.bind else self.bind
        return f"http://{host}:{self.port}/"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.bind, self.port)
        logger.info("preview at %s", self.url)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()

    async def devices(self) -> dict[str, Any]:
        detected = list(self.hub.ios_devices())
        panels = self.hub.panels
        if panels is not None:
            detected += await panels.devices()
        return {"devices": merge(config.load_devices(), detected), "panels_port": self.panels_port}

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except Exception:
            logger.exception("preview request failed")
        finally:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            parts = (await reader.readline()).decode("latin-1").split()
            headers: dict[str, str] = {}
            while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                if len(headers) >= MAX_HEADERS:
                    return self._reply(writer, 400, b"too many headers")
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
        except (ValueError, asyncio.LimitOverrunError):
            return self._reply(writer, 400, b"request line or header too long")
        if len(parts) < 2:
            if parts:
                self._reply(writer, 400, b"bad request")
            return
        method, path = parts[0].upper(), urllib.parse.unquote(parts[1].split("?", 1)[0])
        origin = headers.get("origin")
        if headers.get("host", "").lower() not in self.hosts or not self._origin_ok(method, origin):
            return self._reply(writer, 403, b"forbidden")
        raw_length = headers.get("content-length") or "0"
        if not raw_length.isascii() or not raw_length.isdigit():
            return self._reply(writer, 400, b"bad Content-Length")
        length = int(raw_length)
        if length > MAX_BODY:
            return self._reply(writer, 413, b"too large")
        body = await reader.readexactly(length) if length > 0 else b""

        if method == "GET" and path in ("/", "/index.html"):
            return self._static(writer, "deck.html")
        if method == "GET" and path in ("/deck.css", "/deck.js"):
            return self._static(writer, path[1:])
        if method == "GET" and path in ("/favicon.ico", "/logo.png"):
            return self._reply(writer, 200, viewer_patch.LOGO_PNG, b"image/png")
        if path == "/api/devices" and method == "GET":
            return self._json(writer, 200, await self.devices())
        if path == "/api/config":
            if method == "GET":
                return self._json(writer, 200, {"devices": config.load_devices()})
            if method == "POST":
                return await self._save_config(writer, headers, body)
            return self._reply(writer, 405, b"method not allowed")
        if method == "GET" and path.startswith("/frame/") and path.endswith(".mask.png"):
            return await self._frame(writer, path[len("/frame/"):-len(".mask.png")], mask=True)
        if method == "GET" and path.startswith("/frame/") and path.endswith(".png"):
            return await self._frame(writer, path[len("/frame/"):-len(".png")])
        if path.startswith("/ios/"):
            return await self._ios(writer, method, path)
        return self._reply(writer, 404, b"not found")

    def _origin_ok(self, method: str, origin: Optional[str]) -> bool:
        if origin is None:
            return True
        if origin == "null":
            return method in ("GET", "HEAD")
        return origin.lower() in self.origins

    async def _save_config(self, writer: asyncio.StreamWriter, headers: dict[str, str], body: bytes) -> None:
        # A JSON content type makes a cross-site browser request need a CORS preflight, which
        # this server never grants.
        if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return self._reply(writer, 415, b"send application/json")
        problem = config.config_error()
        if problem is not None:
            return self._json(writer, 409, {"error": f"not saved: {problem}; fix or move the file by hand"})
        try:
            saved = config.save_devices(json.loads(body or b"null"))
        except config.ConfigUnreadable as e:
            return self._json(writer, 409, {"error": f"not saved: {e}; fix or move the file by hand"})
        except ValueError as e:
            return self._json(writer, 400, {"error": str(e)})
        except OSError as e:
            logger.warning("cannot save %s: %s", config.CONFIG_PATH, e)
            return self._json(writer, 500, {"error": f"cannot save: {e}"})
        logger.info("device list saved to %s", config.CONFIG_PATH)
        self.hub.config_changed()
        return self._json(writer, 200, {"devices": config.load_devices(), "saved": len(saved)})

    async def _frame(self, writer: asyncio.StreamWriter, product: str, mask: bool = False) -> None:
        if not device_frame.PRODUCT_RE.match(product):
            return self._reply(writer, 404, b"not found")
        try:
            if mask:
                path = await asyncio.to_thread(device_frame.mask_file, product)
            else:
                found = await asyncio.to_thread(device_frame.frame_files, product)
                path = found[0] if found else None
        except Exception as e:
            logger.warning("frame for %s: %s", product, e)
            path = None
        if path is None:
            return self._reply(writer, 404, b"no frame")
        return self._reply(writer, 200, path.read_bytes(), b"image/png", cache=True)

    async def _ios(self, writer: asyncio.StreamWriter, method: str, path: str) -> None:
        udid, _, rest = path[len("/ios/"):].partition("/")
        if not UDID_RE.match(udid):
            return self._reply(writer, 404, b"not found")
        if path == f"/ios/{udid}":
            writer.write(f"HTTP/1.1 301 Moved\r\nLocation: /ios/{udid}/\r\nContent-Length: 0\r\n"
                         "Connection: close\r\n\r\n".encode())
            return
        if method == "GET" and rest in ("", "index.html"):
            return self._reply(writer, 200, self._viewer_html, b"text/html; charset=utf-8")
        if method == "GET" and rest == "viewer.js":
            return self._reply(writer, 200, self._viewer_js, TYPES[".js"])
        if method == "GET" and rest == "viewer.css":
            return self._reply(writer, 200, self._viewer_css, TYPES[".css"])
        if method == "GET" and rest in ("logo.png", "favicon.ico"):
            return self._reply(writer, 200, viewer_patch.LOGO_PNG, b"image/png")
        if (method, rest) not in viewer_patch.VIEWER_PROXY_PATHS:
            return self._reply(writer, 404, b"not found")
        sock = self.hub.worker_http_socket(udid)
        if sock is None:
            return self._reply(writer, 503, b"iPhone not connected")
        await self._proxy(writer, sock, method, rest)

    @staticmethod
    async def _proxy(writer: asyncio.StreamWriter, sock: Path, method: str, rest: str) -> None:
        """Pass one read-only request to the worker's stream server and pipe the answer back."""
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_unix_connection(str(sock)), 5.0)
        except (OSError, asyncio.TimeoutError):
            Preview._reply(writer, 503, b"iPhone stream not ready")
            return
        try:
            up_writer.write(f"{method} /{rest} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n"
                            "Connection: close\r\n\r\n".encode())
            await up_writer.drain()
            while chunk := await up_reader.read(1 << 16):
                writer.write(chunk)
                await writer.drain()
        finally:
            up_writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(up_writer.wait_closed(), 2.0)

    def _static(self, writer: asyncio.StreamWriter, name: str) -> None:
        path = STATIC / name
        self._reply(writer, 200, path.read_bytes(), TYPES[path.suffix])

    @staticmethod
    def _json(writer: asyncio.StreamWriter, status: int, data: Any) -> None:
        Preview._reply(writer, status, json.dumps(data, ensure_ascii=False).encode(), b"application/json")

    @staticmethod
    def _reply(writer: asyncio.StreamWriter, status: int, body: bytes,
               content_type: bytes = b"text/plain", cache: bool = False) -> None:
        head = (f"HTTP/1.1 {status} X\r\nContent-Type: {content_type.decode()}\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Cache-Control: {'max-age=3600' if cache else 'no-store'}\r\n"
                "X-Frame-Options: SAMEORIGIN\r\nX-Content-Type-Options: nosniff\r\n"
                "Referrer-Policy: no-referrer\r\nConnection: close\r\n")
        writer.write(head.encode() + b"\r\n" + body)
