#!/usr/bin/env python3
"""devicedeck daemon (hub). Run it with the venv's python: `venv/bin/python phoned.py --preview`.

- Finds connected physical iPhones (usbmux, USB or network, plus `xcrun devicectl list devices`)
  and starts one ios_worker.py per connected iPhone (hiding a device only affects the page). pymobiledevice3 allows
  one userspace tunnel per process, hence one process per iPhone.
- Serves the preview page (preview.py) on 127.0.0.1:DEVICEDECK_PORT (default 7317) and the
  Android and simulator streams (side_panels.py) on the next port.
- Takes CLI commands on a Unix socket (config.SOCKET_PATH), one line each, and forwards them to
  the right worker. A line may start with `@<udid>` (or `@<name>`) to pick the iPhone; without
  it the target is --udid / DEVICEDECK_UDID, else the only connected iPhone.
  `ping` and `devices` are answered here, `quit` stops everything.
"""
import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import config
import device_frame
import preview
import side_panels

logger = logging.getLogger("phoned")

HERE = Path(__file__).resolve().parent
DISCOVERY_SECS = 3.0
DEVICECTL_TTL = 15.0
MISSING_ROUNDS = 2  # a device must be missing this many rounds in a row before its worker stops
WORKER_STOP_SECS = 20.0
REQUEST_TIMEOUT = 115.0
LINE_LIMIT = 64 * 1024 * 1024


class CommandError(Exception):
    pass


async def _run(*args: str, timeout: float = 20.0) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, out


async def usbmux_devices() -> dict[str, str]:
    """{udid: "USB" | "Network"} of the iPhones usbmuxd sees right now."""
    from pymobiledevice3 import usbmux

    out: dict[str, str] = {}
    for device in await usbmux.list_devices():
        kind = str(getattr(device, "connection_type", "") or "")
        if device.serial not in out or kind == "USB":
            out[device.serial] = kind
    return out


async def devicectl_devices() -> dict[str, dict[str, Any]]:
    """Physical iOS devices CoreDevice knows (paired), with name, model and OS."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "devices.json"
        await _run("xcrun", "devicectl", "list", "devices", "--json-output", str(path), "-q")
        data = json.loads(path.read_text()) if path.exists() else {}
    out: dict[str, dict[str, Any]] = {}
    for device in (data.get("result") or {}).get("devices") or []:
        hw, props = device.get("hardwareProperties") or {}, device.get("deviceProperties") or {}
        conn = device.get("connectionProperties") or {}
        if hw.get("reality") != "physical" or hw.get("platform") not in ("iOS", "iPadOS") or not hw.get("udid"):
            continue
        out[hw["udid"]] = {
            "name": props.get("name"), "product_type": hw.get("productType"),
            "model": hw.get("marketingName") or hw.get("productType"),
            "ios": props.get("osVersionNumber"), "platform": hw.get("platform") or "iOS",
            "connected": conn.get("tunnelState") == "connected",
            "transport": conn.get("transportType"),
        }
    return out


async def display_points(udid: str) -> Optional[list[float]]:
    """Screen size in points of the primary display, as the device reports it (bounds / scale).
    Xcode's device types are not enough: an iPhone 12 mini is 375x812 pt, not 1080x2340 / 3."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "displays.json"
        await _run("xcrun", "devicectl", "device", "info", "displays", "--device", udid,
                   "--json-output", str(path), "-q", timeout=20.0)
        data = json.loads(path.read_text()) if path.exists() else {}
    for display in (data.get("result") or {}).get("displays") or []:
        if display.get("primary"):
            (_, _), (w, h) = display["bounds"]
            scale = float(display.get("pointScale") or 1)
            return [round(w / scale, 2), round(h / scale, 2)]
    return None


class Worker:
    """One ios_worker.py process for one iPhone."""

    def __init__(self, udid: str) -> None:
        self.udid = udid
        rundir = config.runtime_dir()
        self.socket = rundir / f"w-{udid}.sock"
        self.http_socket = rundir / f"w-{udid}.http"
        self.log = rundir / f"w-{udid}.log"
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.status: dict[str, Any] = {}
        self.started_at = 0.0
        self.exited_at: Optional[float] = None
        self.failures = 0

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, verbose: bool = False) -> None:
        # The worker's own log is kept per device; the newest start goes on top of the old text.
        log = open(self.log, "ab")
        try:
            args = [sys.executable, str(HERE / "ios_worker.py"), "--udid", self.udid, "--socket", str(self.socket),
                    "--http-socket", str(self.http_socket), "--parent", str(os.getpid())]
            if verbose:
                args.append("-v")
            self.proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                             stdout=log, stderr=log, cwd=str(HERE))
        finally:
            log.close()
        self.started_at, self.exited_at = time.monotonic(), None
        logger.info("worker for %s started (pid %s, log %s)", self.udid, self.proc.pid, self.log)

    async def request(self, line: str, timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket), limit=LINE_LIMIT), 5.0)
        except (OSError, asyncio.TimeoutError):
            raise CommandError(f"iPhone {self.udid} is starting up, try again in a moment") from None
        try:
            writer.write(line.encode() + b"\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout)
        finally:
            writer.close()
        if not raw:
            raise CommandError(f"worker for {self.udid} closed the connection")
        return json.loads(raw)

    async def refresh_status(self) -> None:
        if not self.running():
            self.status = {}
            return
        try:
            self.status = await self.request("ping", timeout=3.0)
        except (CommandError, OSError, ValueError, asyncio.TimeoutError):
            self.status = {"connected": False, "last_error": "starting"}

    async def stop(self) -> None:
        if not self.running():
            return
        assert self.proc is not None
        logger.info("stopping worker for %s", self.udid)
        with contextlib.suppress(ProcessLookupError):
            self.proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(self.proc.wait(), WORKER_STOP_SECS)
        except asyncio.TimeoutError:
            logger.warning("worker for %s did not stop; killing it", self.udid)
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            await self.proc.wait()


class Hub:
    def __init__(self, socket_path: Path, preview_at: Optional[tuple[str, int]], panels_port: int,
                 default_udid: Optional[str], verbose: bool = False) -> None:
        self.socket_path = socket_path
        self.default_udid = default_udid
        self.verbose = verbose
        self.panels: Optional[side_panels.SidePanels] = None
        self.preview: Optional[preview.Preview] = None
        if preview_at:
            if panels_port:
                self.panels = side_panels.SidePanels(preview_at[0], panels_port, preview_at[1])
            self.preview = preview.Preview(self, preview_at[0], preview_at[1], panels_port or None)
        self.workers: dict[str, Worker] = {}
        self.iphones: dict[str, dict[str, Any]] = {}  # connected physical iOS devices
        self._known: dict[str, dict[str, Any]] = {}  # devicectl data, also for unplugged ones
        self._known_at = 0.0
        self._known_usb: set[str] = set()
        self._missing: dict[str, int] = {}
        self._frames: dict[str, Optional[dict[str, Any]]] = {}  # product type -> bezel geometry
        self._points: dict[str, list[float]] = {}  # udid -> screen size in points
        self._points_tried: dict[str, float] = {}
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.started_at = time.time()

    # ----- discovery and workers

    def config_changed(self) -> None:
        self.wake.set()

    async def discover(self) -> None:
        try:
            usb = await usbmux_devices()
        except Exception as e:
            logger.debug("usbmux: %s", e)
            usb = {}
        now = time.monotonic()
        if now - self._known_at > DEVICECTL_TTL or set(usb) != self._known_usb:
            try:
                self._known = await devicectl_devices()
            except Exception as e:
                logger.debug("devicectl: %s", e)
            self._known_at, self._known_usb = now, set(usb)
        found: dict[str, dict[str, Any]] = {}
        for udid in set(usb) | {u for u, d in self._known.items() if d.get("connected")}:
            info = dict(self._known.get(udid) or {})
            info["connection"] = usb.get(udid) or info.get("transport") or "network"
            found[udid] = info
        self.iphones = found
        for udid in found:
            if udid not in self._points and now - self._points_tried.get(udid, -1e9) > 60:
                self._points_tried[udid] = now
                try:
                    points = await display_points(udid)
                except Exception as e:
                    logger.debug("displays of %s: %s", udid, e)
                    points = None
                if points:
                    self._points[udid] = points
        for info in found.values():
            product = info.get("product_type")
            if product and product not in self._frames:
                try:
                    self._frames[product] = await asyncio.to_thread(device_frame.frame_meta, product)
                except Exception as e:
                    logger.warning("frame for %s: %s", product, e)
                    self._frames[product] = None

    def wanted(self) -> set[str]:
        return set(self.iphones)

    async def reconcile(self) -> None:
        want = self.wanted()
        for udid, worker in list(self.workers.items()):
            if udid in want:
                self._missing.pop(udid, None)
                continue
            self._missing[udid] = self._missing.get(udid, 0) + 1
            if self._missing[udid] >= MISSING_ROUNDS:
                await worker.stop()
                del self.workers[udid]
                self._missing.pop(udid, None)
        for udid in want:
            worker = self.workers.setdefault(udid, Worker(udid))
            if worker.running():
                continue
            now = time.monotonic()
            if worker.proc is not None:  # it exited by itself; restart it, slower if it keeps failing
                if worker.exited_at is None:
                    worker.exited_at = now
                    worker.failures = worker.failures + 1 if now - worker.started_at < 30 else 0
                    logger.warning("worker for %s exited (%s); see %s", udid, worker.proc.returncode, worker.log)
                if worker.failures and now - worker.exited_at < min(5 * 2 ** worker.failures, 120):
                    continue
            await worker.start(self.verbose)
        await asyncio.gather(*(w.refresh_status() for w in self.workers.values()))

    async def _discovery_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.discover()
                await self.reconcile()
            except Exception:
                logger.exception("discovery failed")
            self.wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), DISCOVERY_SECS)

    # ----- what preview.py asks

    def worker_http_socket(self, udid: str) -> Optional[Path]:
        worker = self.workers.get(udid)
        return worker.http_socket if worker and worker.running() else None

    def ios_devices(self) -> list[dict[str, Any]]:
        out = []
        for udid, info in self.iphones.items():
            worker = self.workers.get(udid)
            status = worker.status if worker else {}
            product = status.get("product_type") or info.get("product_type")
            ios = status.get("ios") or info.get("ios")
            meta = [info.get("model") or product, f"{info.get('platform', 'iOS')} {ios}" if ios else None,
                    {"USB": "USB", "Network": "Wi-Fi"}.get(info.get("connection", ""), info.get("connection"))]
            frame = self._frames.get(product) if product else None
            error = None
            if worker is None:
                error = "not shown"
            elif not status.get("connected"):
                error = status.get("last_error") or "connecting…"
            elif not status.get("stream"):
                error = "waiting for the video stream…"
            out.append({"id": udid, "kind": "ios", "name": info.get("name") or product or udid,
                        "meta": " · ".join(m for m in meta if m), "frame": frame, "size": None,
                        "live": bool(status.get("stream")), "error": error,
                        "stream": f"/ios/{udid}/" if worker and worker.running() else None})
        return out

    # ----- CLI commands

    def _display_names(self) -> dict[str, str]:
        names = {d["id"]: d["name"] for d in config.load_devices() if d["kind"] == "ios" and d["name"]}
        return {u: names.get(u) or (self.iphones.get(u) or {}).get("name") or u for u in self.workers}

    def _pick(self, target: Optional[str]) -> Worker:
        if target:
            if target in self.workers:
                return self.workers[target]
            names = self._display_names()
            matches = [u for u, n in names.items() if target.lower() in (u.lower(), n.lower())]
            if len(matches) == 1:
                return self.workers[matches[0]]
            raise CommandError(f"no connected iPhone {target!r} (connected: {self._listing() or 'none'})")
        if self.default_udid:
            if self.default_udid in self.workers:
                return self.workers[self.default_udid]
            raise CommandError(f"iPhone {self.default_udid} (DEVICEDECK_UDID / --udid) is not connected")
        if len(self.workers) == 1:
            return next(iter(self.workers.values()))
        if not self.workers:
            raise CommandError("no iPhone connected")
        raise CommandError(f"several iPhones connected ({self._listing()}); start the line with @<udid>, "
                           "or set DEVICEDECK_UDID")

    def _listing(self) -> str:
        return ", ".join(f"{n} = {u}" for u, n in self._display_names().items())

    def _device_status(self, worker: Worker) -> dict[str, Any]:
        info = self.iphones.get(worker.udid) or {}
        status = {k: v for k, v in worker.status.items() if k not in ("ok", "error", "ms")}
        status.setdefault("connected", False)
        status["udid"] = worker.udid
        status["name"] = self._display_names().get(worker.udid)
        status.setdefault("product_type", info.get("product_type"))
        status.setdefault("ios", info.get("ios"))
        if worker.udid in self._points:
            status["points"] = self._points[worker.udid]
        status["connection"] = info.get("connection")
        return status

    async def _status(self, target: Optional[str]) -> dict[str, Any]:
        await asyncio.gather(*(w.refresh_status() for w in self.workers.values()))
        devices = [self._device_status(w) for w in self.workers.values()]
        reply: dict[str, Any] = {"connected": False, "udid": None,
                                 "uptime": round(time.time() - self.started_at, 1), "devices": devices}
        with contextlib.suppress(CommandError):
            chosen = self._pick(target)
            reply.update(next(d for d in devices if d["udid"] == chosen.udid))
            reply["uptime"] = round(time.time() - self.started_at, 1)
        if self.preview is not None:
            reply["preview"] = self.preview.url
        return reply

    async def execute(self, line: str) -> dict[str, Any]:
        line = line.strip()
        target = None
        if line.startswith("@"):
            target, _, line = line[1:].partition(" ")
            line = line.strip()
        cmd = line.split(" ", 1)[0]
        if cmd == "ping":
            return {"ok": True, "error": None, **await self._status(target)}
        if cmd == "devices":
            await asyncio.gather(*(w.refresh_status() for w in self.workers.values()))
            return {"ok": True, "error": None, "devices": [self._device_status(w) for w in self.workers.values()]}
        if cmd == "quit":
            asyncio.get_running_loop().call_later(0.1, self.stop.set)
            return {"ok": True, "error": None}
        if not cmd:
            raise CommandError("empty command")
        worker = self._pick(target)
        reply = await worker.request(line)
        reply.setdefault("udid", worker.udid)
        return reply

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while raw := await reader.readline():
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                started = time.monotonic()
                try:
                    reply = await self.execute(line)
                except CommandError as e:
                    reply = {"ok": False, "error": str(e)}
                except asyncio.TimeoutError:
                    reply = {"ok": False, "error": "timed out"}
                except Exception as e:
                    logger.warning("command %r failed", line, exc_info=True)
                    reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                reply.setdefault("ms", round((time.monotonic() - started) * 1000))
                writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, ValueError):
            pass
        finally:
            writer.close()

    # ----- lifecycle

    def _claim_socket(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.socket_path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(self.socket_path))
        except OSError:
            self.socket_path.unlink()
        else:
            raise SystemExit(f"phoned is already running on {self.socket_path}")
        finally:
            probe.close()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        self._claim_socket()
        unix_server = await asyncio.start_unix_server(self._client, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        logger.info("listening on %s; device list %s", self.socket_path, config.CONFIG_PATH)
        for server, label in ((self.panels, "stream server"), (self.preview, "preview")):
            if server is None:
                continue
            try:
                await server.start()
            except OSError as e:
                logger.warning("%s disabled: %s", label, e)
                if server is self.panels:
                    self.panels = None
                else:
                    self.preview = None
        discovery = asyncio.create_task(self._discovery_loop())
        try:
            await self.stop.wait()
        finally:
            logger.info("shutting down")
            unix_server.close()
            discovery.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await discovery
            await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)
            for server in (self.preview, self.panels):
                if server is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(server.close(), 5.0)
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.unlink()
            logger.info("bye")


def main() -> None:
    parser = argparse.ArgumentParser(description="devicedeck daemon: preview page, device discovery, iPhone workers.")
    parser.add_argument("--udid", default=config.env("DEVICEDECK_UDID", "PHONE_UDID"),
                        help="default iPhone for commands without @<udid> "
                             "(env DEVICEDECK_UDID, alias PHONE_UDID)")
    parser.add_argument("--socket", default=str(config.SOCKET_PATH),
                        help="command socket (env DEVICEDECK_SOCK, alias PHONED_SOCK)")
    parser.add_argument("--preview", action="store_true", help="serve the preview page")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", "--http-port", dest="http_port", type=int,
                        default=int(config.env("DEVICEDECK_PORT", default="7317")), help="preview port (env DEVICEDECK_PORT)")
    parser.add_argument("--panels-port", type=int, default=config.env("DEVICEDECK_PANELS_PORT"),
                        help="Android + simulator streams, default preview port + 1, 0 = off (env DEVICEDECK_PANELS_PORT)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    os.umask(0o077)
    config.runtime_dir()
    preview_at = (args.bind, args.http_port) if args.preview else None
    panels_port = args.http_port + 1 if args.panels_port is None else int(args.panels_port)
    asyncio.run(Hub(Path(args.socket).expanduser(), preview_at, panels_port, args.udid, args.verbose).run())


if __name__ == "__main__":
    main()
