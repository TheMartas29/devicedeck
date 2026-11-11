#!/usr/bin/env python3
"""One physical iPhone: holds its CoreDevice tunnel and media stream and takes CLI commands.

pymobiledevice3 allows one userspace tunnel per process, so phoned.py starts one worker per
connected iPhone. A worker listens on two private Unix sockets in the runtime dir:
- the command socket (one JSON line per request, see `execute`), which phoned forwards CLI
  commands to;
- the HTTP socket of pymobiledevice3's stream server, which phoned's preview page reads the
  video from. phoned passes on only the read-only stream paths, never the input ones.
  Before iOS 27 the phone has no media stream (error 9021); the socket then serves
  GET /stream.mjpg instead, built from screenshots taken only while a page watches.
Run it with the venv's python; phoned does that.
"""
import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image
from pymobiledevice3.exceptions import CoreDeviceError
from pymobiledevice3.remote.core_device.display_service import (
    MEDIA_IN_USE_MESSAGE,
    DisplayService,
    is_media_in_use_error,
)
from pymobiledevice3.remote.core_device.hid_service import (
    ASCII_TO_HID,
    DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
    HID_BUTTON_STATE_DOWN,
    HID_BUTTON_STATE_UP,
    KEY_BACKSPACE,
    KEY_DOWN,
    KEY_ENTER,
    KEY_ESC,
    KEY_LEFT,
    KEY_LEFT_SHIFT,
    KEY_RIGHT,
    KEY_TAB,
    KEY_UP,
    TOUCH_AUTH_SETTLE_SECONDS,
    TOUCHSCREEN_STATE_CONTACT,
    TOUCHSCREEN_STATE_RELEASE,
    IndigoHIDService,
)
from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService
from pymobiledevice3.remote.core_device.screen_stream import (
    _MAX_STALL_RESTARTS,
    _NAMED_BUTTONS,
    _STALL_RESTART_SECS,
    ScreenStreamServer,
    _is_tunnel_dead_error,
    _SubState,
)
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
from pymobiledevice3.services.accessibilityaudit import AccessibilityAudit

import side_panels


NAMED_KEYS = {
    "backspace": KEY_BACKSPACE,
    "enter": KEY_ENTER,
    "escape": KEY_ESC,
    "tab": KEY_TAB,
    "left": KEY_LEFT,
    "right": KEY_RIGHT,
    "up": KEY_UP,
    "down": KEY_DOWN,
}

CHAR_DELAY = 0.04
INTER_DELAY = 0.02
DRAG_STEPS = 30
DRAG_DURATION = 0.6
HID_MAX = 0xFFFF
STREAM_START_TIMEOUT = 25.0
STREAM_RESTART_TIMEOUT = 10.0
READY_TIMEOUT = 20.0
GIVEN_UP_RESET_SECS = 60.0
RETRY_START_SECS = 10.0
PARENT_CHECK_SECS = 2.0
STREAM_MIN_IOS = 27
STREAM_UNSUPPORTED = 9021  # CoreDeviceError code: "Remote control requires iOS 27.0 or later"
SHOT_INTERVAL = 0.5  # shortest time between two preview screenshots (one takes ~1.5 s)
SHOT_REDUCE = 2
SHOT_QUALITY = 80

logger = logging.getLogger("ios_worker")


class CommandError(Exception):
    pass


class StreamHost(ScreenStreamServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stream_started_at = 0.0

    async def _ensure_fresh_stream(self, force: bool = False) -> None:
        before = self._active_service
        await super()._ensure_fresh_stream(force)
        if self._active_service is not None and self._active_service is not before:
            self.stream_started_at = asyncio.get_running_loop().time()
            logger.info("media stream up")


async def _bounded(coro: Awaitable[Any], label: str, timeout: float = 3.0) -> None:
    try:
        await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("shutdown: %s timed out after %.1fs", label, timeout)
    except Exception:
        logger.debug("shutdown: %s raised", label, exc_info=True)


def _ios_major(version: Optional[str]) -> int:
    try:
        return int(str(version).split(".")[0])
    except ValueError:
        return STREAM_MIN_IOS


def _jpeg(png: bytes) -> bytes:
    image = Image.open(io.BytesIO(png)).convert("RGB").reduce(SHOT_REDUCE)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=SHOT_QUALITY)
    return out.getvalue()


async def _cancel(task: Optional[asyncio.Task[Any]]) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


class Session:
    def __init__(self, udid: str, http_socket: Path, screenshots: bool = False) -> None:
        self.udid = udid
        self.http_socket = http_socket
        self.screenshots = screenshots
        self.stream_unsupported = False
        self.tunnel: Optional[UserspaceRsdTunnel] = None
        self.rsd: Optional[RemoteServiceDiscoveryService] = None
        self.server: Optional[StreamHost] = None
        self.frames: Optional[side_panels.Frames] = None
        self.frame_at = 0.0
        self.http: Optional[asyncio.Server] = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._sink: Optional[asyncio.Queue[bytes]] = None
        self._last_start_attempt = 0.0
        self._shot_lock = asyncio.Lock()

    async def open(self) -> None:
        self.tunnel = UserspaceRsdTunnel(serial=self.udid)
        self.rsd = await asyncio.wait_for(self.tunnel.aopen(), timeout=30.0)
        logger.info("tunnel up: %s (iOS %s)", self.rsd.udid, self.rsd.product_version)
        if self.screenshots or _ios_major(self.rsd.product_version) < STREAM_MIN_IOS:
            self.screenshots = True
            self.frames = side_panels.Frames(self._poll)
            logger.info("no media stream on this phone; the preview polls screenshots")
            await self._listen(self._serve_frames)
            return
        server = StreamHost(
            self.rsd,
            bind="127.0.0.1",
            http_port=0,
            audio_default_on=False,
        )
        self.server = server
        try:
            await asyncio.wait_for(server._eager_stream_start(), timeout=STREAM_START_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("initial stream start timed out; will retry")
        except CoreDeviceError as e:
            if is_media_in_use_error(e):
                raise CoreDeviceError(MEDIA_IN_USE_MESSAGE, code=e.code) from None
            raise

        self._sink = asyncio.Queue(maxsize=64)
        server._subscribers[self._sink] = _SubState()
        server._rctl_task = asyncio.create_task(server._rctl_feedback_loop())
        server._keep_awake_task = asyncio.create_task(server._keep_awake_loop())
        self._tasks = [
            asyncio.create_task(server._stall_watchdog()),
            asyncio.create_task(server._decoder_refresh_loop()),
            asyncio.create_task(self._drain_sink()),
            server._rctl_task,
            server._keep_awake_task,
        ]
        await self._listen(server._handle_http)

    async def _listen(self, handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.http_socket.unlink()
        self.http = await asyncio.start_unix_server(handler, path=str(self.http_socket))
        os.chmod(self.http_socket, 0o600)
        logger.info("stream server on %s", self.http_socket)

    async def screenshot(self) -> bytes:
        assert self.rsd is not None
        async with self._shot_lock:
            async with ScreenCaptureService(self.rsd) as service:
                return (await service.capture_screenshot())["image"]

    async def _poll(self, frames: side_panels.Frames) -> None:
        loop = asyncio.get_running_loop()
        while not frames.idle():
            started = loop.time()
            jpeg = await asyncio.to_thread(_jpeg, await self.screenshot())
            frames.error = None
            self.frame_at = loop.time()
            if jpeg != frames.frame:
                await frames.publish(jpeg)
            await asyncio.sleep(max(0.05, SHOT_INTERVAL - (loop.time() - started)))

    async def _serve_frames(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert self.frames is not None
        try:
            request = (await reader.readline()).split()
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            if request[:2] == [b"GET", b"/stream.mjpg"]:
                await self.frames.stream(writer, side_panels.SidePanels._stream_head(None))
            else:
                side_panels.SidePanels._reply(writer, 404, b"not found")
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def _drain_sink(self) -> None:
        assert self._sink is not None
        while True:
            await self._sink.get()

    def dead(self) -> bool:
        assert self.tunnel is not None
        return self.tunnel.rsd is None or (self.server is not None and self.server._reconnect_signal.is_set())

    async def maintain(self) -> None:
        server = self.server
        if server is None:
            return
        now = asyncio.get_running_loop().time()
        if server._consecutive_restarts >= _MAX_STALL_RESTARTS and now - server._last_restart_t > GIVEN_UP_RESET_SECS:
            logger.info("re-arming stall watchdog after %d failed restarts", server._consecutive_restarts)
            server._consecutive_restarts = 0
        if server._active_service is None and now - self._last_start_attempt > RETRY_START_SECS:
            self._last_start_attempt = now
            try:
                await asyncio.wait_for(server._ensure_fresh_stream(), timeout=STREAM_RESTART_TIMEOUT)
            except Exception as e:
                if isinstance(e, CoreDeviceError) and e.code == STREAM_UNSUPPORTED:
                    self.stream_unsupported = True
                    return
                logger.warning("media stream start failed: %s", e)
                if _is_tunnel_dead_error(e):
                    server._reconnect_signal.set()

    async def close(self) -> None:
        server = self.server
        if self.http is not None:
            self.http.close()
            if self.frames is not None:
                self.http.close_clients()
        if self.frames is not None:
            await self.frames.close()
        for task in self._tasks:
            await _cancel(task)
        if server is not None:
            await asyncio.sleep(0)
            for task in list(server._http_tasks):
                await _cancel(task)
            if self.http is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.http.wait_closed(), timeout=2.0)
            await _bounded(server._stop_hid(), "_stop_hid")
            await _cancel(server._hid_worker_task)
            server._hid_worker_task = None

            async def _stop_video() -> None:
                async with server._stream_lock:
                    await server._stop_active_stream()

            async def _stop_audio() -> None:
                async with server._audio_lock:
                    await server._stop_audio_stream()

            await _bounded(_stop_video(), "_stop_active_stream")
            await _bounded(_stop_audio(), "_stop_audio_stream")
            if self.tunnel is not None and self.tunnel.rsd is not None:
                await _bounded(DisplayService.stop_all_streams(server._rsd), "stop_all_streams", timeout=6.0)
            await _bounded(server._stop_clipboard_monitor(), "_stop_clipboard_monitor")
            for task in list(server._pli_tasks):
                await _cancel(task)
        if self.tunnel is not None:
            await _bounded(self.tunnel.aclose(), "tunnel close", timeout=5.0)


def _coord(value: str) -> int:
    v = int(value)
    if not 0 <= v <= HID_MAX:
        raise CommandError(f"coordinate {v} out of range 0..{HID_MAX}")
    return v


def _text_arg(rest: str) -> str:
    if rest.startswith('"'):
        try:
            value = json.loads(rest)
        except json.JSONDecodeError as e:
            raise CommandError(f"bad JSON string: {e}") from None
        if not isinstance(value, str):
            raise CommandError("expected a JSON string")
        return value
    return rest


def _png_size(data: bytes) -> Optional[tuple[int, int]]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


class Daemon:
    def __init__(self, udid: str, socket_path: Path, http_socket: Path, parent: Optional[int] = None) -> None:
        self.udid = udid
        self.socket_path = socket_path
        self.http_socket = http_socket
        self.parent = parent
        self.product_type: Optional[str] = None
        self.product_version: Optional[str] = None
        self.session: Optional[Session] = None
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.rebuild = asyncio.Event()
        self.hid_lock = asyncio.Lock()
        self.started_at = time.time()
        self.last_error: Optional[str] = None
        self.screenshots = False

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        self._claim_socket()
        unix_server = await asyncio.start_unix_server(self._client, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        logger.info("listening on %s", self.socket_path)
        supervisor = asyncio.create_task(self._supervise())
        watcher = asyncio.create_task(self._watch_parent()) if self.parent else None
        try:
            await self.stop.wait()
        finally:
            logger.info("shutting down")
            unix_server.close()
            try:
                await asyncio.wait_for(asyncio.shield(supervisor), timeout=20.0)
            except asyncio.TimeoutError:
                await _cancel(supervisor)
            await _cancel(watcher)
            for path in (self.socket_path, self.http_socket):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            logger.info("bye")

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
            raise SystemExit(f"a worker is already running on {self.socket_path}")
        finally:
            probe.close()

    async def _watch_parent(self) -> None:
        """Stop when phoned goes away (it is then our parent no more), so no stream is left open."""
        while not self.stop.is_set():
            if os.getppid() != self.parent:
                logger.warning("phoned (pid %s) is gone; stopping", self.parent)
                self.stop.set()
                return
            await asyncio.sleep(PARENT_CHECK_SECS)

    async def _wait_stop(self, timeout: float) -> bool:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=timeout)
        return self.stop.is_set()

    async def _supervise(self) -> None:
        delay = 2.0
        while not self.stop.is_set():
            session = Session(self.udid, self.http_socket, self.screenshots)
            opening = asyncio.create_task(session.open())
            stopping = asyncio.create_task(self.stop.wait())
            await asyncio.wait({opening, stopping}, return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
            if not opening.done():
                await _cancel(opening)
                await session.close()
                return
            if opening.exception() is not None:
                self.last_error = f"connect failed: {opening.exception()}"
                logger.warning("%s; retrying in %.0fs", self.last_error, delay)
                await session.close()
                if await self._wait_stop(delay):
                    return
                delay = min(delay * 2, 30.0)
                continue
            delay = 2.0
            self.last_error = None
            if session.rsd is not None:
                self.product_type = session.rsd.product_type
                self.product_version = session.rsd.product_version
            self.rebuild.clear()
            self.session = session
            self.ready.set()
            try:
                await self._monitor(session)
            finally:
                self.ready.clear()
                self.session = None
                await session.close()

    async def _monitor(self, session: Session) -> None:
        while not self.stop.is_set():
            if self.rebuild.is_set() or session.dead():
                self.last_error = "tunnel lost"
                logger.warning("tunnel lost; reconnecting")
                return
            if session.stream_unsupported:
                logger.info("the phone has no media stream (error %d); reconnecting for screenshots",
                            STREAM_UNSUPPORTED)
                self.screenshots = True
                return
            try:
                await session.maintain()
            except Exception:
                logger.exception("maintenance failed")
            if await self._wait_stop(2.0):
                return

    async def _session(self) -> Session:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=READY_TIMEOUT)
        except asyncio.TimeoutError:
            raise CommandError(f"device not connected ({self.last_error or 'connecting'})") from None
        assert self.session is not None
        return self.session

    async def _prepare_stream(self, server: StreamHost) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        stalled = server._active_service is not None and now - server._last_good_au_t > _STALL_RESTART_SECS
        if server._active_service is None or stalled:
            if stalled:
                logger.warning("stream stalled %.1fs; restarting before input", now - server._last_good_au_t)
                server._last_restart_t = now
            await asyncio.wait_for(server._ensure_fresh_stream(force=stalled), timeout=STREAM_RESTART_TIMEOUT)
        if server._active_service is None:
            raise CommandError("media stream is not running")
        wait = server.stream_started_at + TOUCH_AUTH_SETTLE_SECONDS - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)

    async def _hid(self, op: Callable[[StreamHost], Awaitable[None]]) -> None:
        async with self.hid_lock:
            for attempt in range(2):
                session = await self._session()
                server = session.server
                if server is None:
                    raise CommandError("input needs the media stream (iOS 27 or later)")
                await self._prepare_stream(server)
                stream = server._active_service
                try:
                    await server._ensure_hid()
                    await op(server)
                    return
                except CommandError:
                    raise
                except Exception as e:
                    if _is_tunnel_dead_error(e):
                        self.rebuild.set()
                        raise
                    if attempt or server._active_service is stream:
                        raise
                    logger.info("stream restarted during input (%s); retrying once", e)

    async def _tap(self, x: int, y: int) -> None:
        async def op(server: StreamHost) -> None:
            uhs = server._uhs
            assert uhs is not None
            await uhs.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y, service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN)
            await asyncio.sleep(0.05)
            await uhs.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, x, y, service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN)

        await self._hid(op)

    async def _drag(self, x1: int, y1: int, x2: int, y2: int, duration: float, steps: int) -> None:
        async def op(server: StreamHost) -> None:
            uhs = server._uhs
            assert uhs is not None
            frame_interval = duration / max(steps, 1)
            for i in range(steps):
                t = i / steps
                x = round(x1 + (x2 - x1) * t)
                y = round(y1 + (y2 - y1) * t)
                await uhs.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y, service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN)
                await asyncio.sleep(frame_interval)
            await uhs.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x2, y2, service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN)
            await uhs.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, x2, y2, service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN)

        await self._hid(op)

    async def _keys(self, strokes: list[tuple[int, bool]]) -> None:
        async def op(server: StreamHost) -> None:
            await server._ensure_keyboard()
            uhs, kb = server._uhs, server._kb_service_id
            assert uhs is not None and kb is not None
            for usage, needs_shift in strokes:
                if needs_shift:
                    await uhs.send_keyboard(kb, (KEY_LEFT_SHIFT,))
                    await asyncio.sleep(CHAR_DELAY)
                await uhs.send_keyboard(kb, (KEY_LEFT_SHIFT, usage) if needs_shift else (usage,))
                await asyncio.sleep(CHAR_DELAY)
                await uhs.send_keyboard(kb, ())
                await asyncio.sleep(INTER_DELAY)

        await self._hid(op)

    async def _button(self, name: str) -> None:
        if name not in _NAMED_BUTTONS:
            raise CommandError(f"unknown button {name!r} (one of {', '.join(_NAMED_BUTTONS)})")
        usage_page, usage_code, hold = _NAMED_BUTTONS[name]
        async with self.hid_lock:
            server = (await self._session()).server
            if server is None:
                raise CommandError("input needs the media stream (iOS 27 or later)")
            async with server._hid_lock:
                if server._indigo is None:
                    indigo = IndigoHIDService(server._rsd)
                    await indigo.connect()
                    server._indigo = indigo
                indigo = server._indigo
            try:
                await indigo.send_button(usage_page, usage_code, HID_BUTTON_STATE_DOWN)
                await asyncio.sleep(hold)
                await indigo.send_button(usage_page, usage_code, HID_BUTTON_STATE_UP)
            except Exception as e:
                if _is_tunnel_dead_error(e):
                    self.rebuild.set()
                elif server._indigo is indigo:
                    server._indigo = None
                    with contextlib.suppress(Exception):
                        await indigo.close()
                raise

    async def _shot(self, path: Path) -> dict[str, Any]:
        image = await (await self._session()).screenshot()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
        reply: dict[str, Any] = {"path": str(path), "bytes": len(image)}
        size = _png_size(image)
        if size:
            reply["width"], reply["height"] = size
        return reply

    async def _elements(self) -> dict[str, Any]:
        session = await self._session()
        assert session.rsd is not None
        items: list[dict[str, Any]] = []
        async with AccessibilityAudit(session.rsd) as service:
            async for element in service.iter_elements():
                items.append(element.to_dict())
        captions = [e.get("caption") or e.get("spoken_description") or "" for e in items]
        return {"captions": captions, "items": items}

    async def _restart(self) -> None:
        session = await self._session()
        server = session.server
        if server is None:
            raise CommandError("no media stream to restart on this phone")
        server._last_restart_t = asyncio.get_running_loop().time()
        await asyncio.wait_for(server._ensure_fresh_stream(force=True), timeout=STREAM_RESTART_TIMEOUT)

    def _status(self) -> dict[str, Any]:
        session = self.session
        reply: dict[str, Any] = {
            "connected": session is not None,
            "udid": self.udid,
            "uptime": round(time.time() - self.started_at, 1),
        }
        if self.product_type:
            reply["product_type"] = self.product_type
            reply["ios"] = self.product_version
        if self.last_error:
            reply["last_error"] = self.last_error
        if session is None:
            return reply
        now = asyncio.get_running_loop().time()
        if session.frames is not None:
            reply["preview"] = "screenshots"
            reply["stream"] = session.frames.frame is not None
            if session.frame_at:
                reply["last_frame_age"] = round(now - session.frame_at, 2)
            reply["viewers"] = session.frames.viewers
            return reply
        if session.server is None:
            return reply
        server = session.server
        streaming = server._active_service is not None
        reply["stream"] = streaming
        if streaming:
            reply["stream_age"] = round(now - server.stream_started_at, 1)
            reply["last_frame_age"] = round(now - server._last_good_au_t, 2)
        reply["viewers"] = max(len(server._subscribers) - 1, 0)
        return reply

    async def execute(self, line: str) -> dict[str, Any]:
        cmd, _, rest = line.strip().partition(" ")
        rest = rest.strip()
        args = rest.split()
        if cmd == "ping":
            return self._status()
        if cmd == "quit":
            asyncio.get_running_loop().call_later(0.1, self.stop.set)
            return {}
        if cmd == "tap" and len(args) == 2:
            await self._tap(_coord(args[0]), _coord(args[1]))
            return {}
        if cmd == "drag" and 4 <= len(args) <= 6:
            duration = float(args[4]) if len(args) >= 5 else DRAG_DURATION
            steps = int(args[5]) if len(args) >= 6 else DRAG_STEPS
            if duration < 0 or steps < 1:
                raise CommandError("duration must be >= 0 and steps >= 1")
            await self._drag(*(_coord(a) for a in args[:4]), duration, steps)
            return {}
        if cmd == "type" and rest:
            text = _text_arg(rest)
            unsupported = sorted({ch for ch in text if ch not in ASCII_TO_HID})
            if unsupported:
                raise CommandError(f"unsupported characters: {''.join(unsupported)!r}")
            await self._keys([ASCII_TO_HID[ch] for ch in text])
            return {}
        if cmd == "key" and 1 <= len(args) <= 2:
            usage = NAMED_KEYS.get(args[0])
            if usage is None:
                raise CommandError(f"unknown key {args[0]!r} (one of {', '.join(NAMED_KEYS)})")
            count = int(args[1]) if len(args) == 2 else 1
            if count < 1:
                raise CommandError("count must be >= 1")
            await self._keys([(usage, False)] * count)
            return {}
        if cmd == "button" and len(args) == 1:
            await self._button(args[0])
            return {}
        if cmd == "shot":
            path = Path(_text_arg(rest)) if rest else self.socket_path.parent / f"shot-{time.strftime('%H%M%S')}.png"
            if not path.is_absolute():
                raise CommandError("shot PATH must be absolute")
            return await self._shot(path)
        if cmd == "elements" and not args:
            return await asyncio.wait_for(self._elements(), timeout=90.0)
        if cmd == "restart" and not args:
            await self._restart()
            return {}
        raise CommandError(f"unknown command or wrong arguments: {line.strip()!r}")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                started = time.monotonic()
                try:
                    reply = {"ok": True, "error": None, **await self.execute(line)}
                except CommandError as e:
                    reply = {"ok": False, "error": str(e)}
                except asyncio.TimeoutError:
                    reply = {"ok": False, "error": "timed out"}
                except Exception as e:
                    logger.warning("command %r failed", line, exc_info=True)
                    reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                reply["ms"] = round((time.monotonic() - started) * 1000)
                writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Keep one CoreDevice tunnel + media stream to one iPhone open.")
    parser.add_argument("--udid", required=True)
    parser.add_argument("--socket", required=True, help="command socket")
    parser.add_argument("--http-socket", required=True, help="stream server socket for the preview")
    parser.add_argument("--parent", type=int, help="stop when this process is no longer the parent")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=f"%(asctime)s %(levelname)s %(name)s[{args.udid[-6:]}]: %(message)s",
    )
    os.umask(0o077)
    asyncio.run(Daemon(args.udid, Path(args.socket), Path(args.http_socket), args.parent).run())


if __name__ == "__main__":
    main()
