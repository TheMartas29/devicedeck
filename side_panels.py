"""Video of Android devices and booted iOS simulators for the preview page. View only.

Runs inside phoned on its own port (127.0.0.1, preview port + 1, default 7318). preview.py lists
the devices and puts their streams into the page. Capture runs only while a page watches.
Nothing here sends input to any device.

- Android (every device `adb devices` reports ready): `adb exec-out screenrecord` (H.264)
  decoded by ffmpeg into JPEG frames, sent as MJPEG. adb is found like the deck CLI finds it:
  env ADB, then PATH, then $ANDROID_HOME (or $ANDROID_SDK_ROOT, default ~/Library/Android/sdk)
  /platform-tools/adb. ffmpeg must be on PATH.
- Simulators: `simctl io <udid> screenshot` polled (~2 fps), sent as MJPEG.
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from PIL import Image

import config
import device_frame

logger = logging.getLogger("phoned.panels")

SERIAL = os.environ.get("ANDROID_SERIAL", "")  # set = show only this adb device
RECORD_LIMIT = 180  # screenrecord stops itself after this many seconds, the loop restarts it
IDLE_STOP_SECS = 5.0
RETRY_SECS = 5.0
KEEPALIVE_SECS = 5.0
SIM_INTERVAL = 0.4  # shortest time between two simulator screenshots (one takes ~0.5 s)
SIM_LIST_TTL = 5.0
ADB_LIST_TTL = 5.0
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\-]+$")
BOUNDARY = b"frame"
AUD = b"\x00\x00\x00\x01\x09\xf0"  # H.264 access unit delimiter


def adb_path() -> Optional[str]:
    """adb as the deck CLI finds it: env ADB, PATH, then the Android SDK's platform-tools."""
    override = os.environ.get("ADB")
    if override:
        return override
    found = shutil.which("adb")
    if found:
        return found
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or str(
        Path.home() / "Library/Android/sdk")
    cand = Path(sdk) / "platform-tools/adb"
    return str(cand) if cand.exists() else None


async def stop_process(proc: asyncio.subprocess.Process) -> None:
    """Kill a child and reap it, also while the calling task is being cancelled."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), 2.0)


async def run(*args: str, timeout: float = 10.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        await stop_process(proc)
        raise RuntimeError(f"{args[0]} timed out") from None
    except BaseException:
        await stop_process(proc)
        raise
    text = out.decode("utf-8", "replace")
    if proc.returncode:
        raise RuntimeError(text.strip() or f"{args[0]} exited {proc.returncode}")
    return text


def first_line(e: Exception) -> str:
    return str(e).splitlines()[0] if str(e) else type(e).__name__


class Frames:
    """Latest JPEG of one device plus a capture task that runs only while someone watches."""

    def __init__(self, capture: Callable[["Frames"], Awaitable[None]]) -> None:
        self.frame: Optional[bytes] = None
        self.frame_no = 0
        self.new_frame = asyncio.Condition()
        self.viewers = 0
        self.error: Optional[str] = None
        self.closed = False
        self._capture = capture
        self._task: Optional[asyncio.Task[None]] = None

    def idle(self) -> bool:
        return self.viewers == 0

    async def publish(self, jpeg: bytes) -> None:
        async with self.new_frame:
            self.frame = jpeg
            self.frame_no += 1
            self.new_frame.notify_all()

    def _ensure_capture(self) -> None:
        if self.closed:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_capture())

    async def _run_capture(self) -> None:
        """The capture function returns when nobody watches any more (or its run ended)."""
        while True:
            try:
                await self._capture(self)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = first_line(e)
                logger.warning("capture: %s", self.error)
                await asyncio.sleep(RETRY_SECS)
            if self.idle():
                return

    async def stream(self, writer: asyncio.StreamWriter, head: bytes) -> None:
        """MJPEG to one viewer until it goes away or close() is called."""
        if self.closed:
            return
        writer.write(head + b"\r\n--" + BOUNDARY + b"\r\n")
        self.viewers += 1
        self._ensure_capture()
        sent = -1
        try:
            while True:
                async with self.new_frame:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            self.new_frame.wait_for(lambda: self.frame_no != sent or self.closed),
                            KEEPALIVE_SECS)
                    frame, sent = self.frame, self.frame_no
                if self.closed:
                    return
                self._ensure_capture()  # in case it was just stopping when this viewer came
                if frame is None:
                    continue
                # The closing boundary goes out right after the frame: Chrome shows a part only
                # once the next boundary arrives, and a still screen sends no next frame.
                writer.write(b"Content-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() +
                             b"\r\n\r\n" + frame + b"\r\n--" + BOUNDARY + b"\r\n")
                await writer.drain()
        finally:
            self.viewers -= 1

    async def close(self) -> None:
        """Stop capturing for good and end every viewer's stream."""
        self.closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        async with self.new_frame:
            self.new_frame.notify_all()


class Android:
    """One adb device or emulator."""

    def __init__(self, serial: str, adb: str = "adb") -> None:
        self.serial = serial
        self.adb_exe = adb
        self.size: Optional[tuple[int, int]] = None
        self.name: Optional[str] = None
        self.meta = ""
        self.emulator = serial.startswith("emulator-")
        self.frames = Frames(self._capture)

    async def adb(self, *args: str, timeout: float = 10.0) -> str:
        return await run(self.adb_exe, "-s", self.serial, *args, timeout=timeout)

    async def detect(self) -> None:
        sizes = re.findall(r"(\d+)x(\d+)", await self.adb("shell", "wm", "size"))
        if not sizes:
            raise RuntimeError("cannot read the screen size (adb shell wm size)")
        width, height = sizes[-1]  # an "Override size" line comes last and wins
        self.size = (int(width), int(height))
        if self.name is None:
            await self._describe()

    async def _describe(self) -> None:
        props = await self.adb("shell", "getprop ro.build.version.release; getprop ro.build.version.sdk; "
                                        "getprop ro.kernel.qemu; getprop ro.product.model")
        release, sdk, qemu, model = (props.splitlines() + [""] * 4)[:4]
        self.emulator = self.serial.startswith("emulator-") or qemu.strip() == "1"
        name = model.strip()
        if self.emulator:
            with contextlib.suppress(Exception):
                name = (await self.adb("emu", "avd", "name")).splitlines()[0].strip() or name
        self.meta = " · ".join([f"Android {release.strip()} (API {sdk.strip()})",
                                "emulator" if self.emulator else "physical"])
        self.name = name or self.serial

    async def _capture(self, frames: Frames) -> None:
        await self.detect()
        frames.error = None
        started = time.monotonic()
        await self._record_once(frames)
        if time.monotonic() - started < 2.0:
            raise RuntimeError("screenrecord exited right away")

    async def _record_once(self, frames: Frames) -> None:
        """One screenrecord run, until its time limit, an error, or nobody watches any more."""
        assert self.size is not None
        width, height = self.size
        out_w, out_h = width // 2 & ~1, height // 2 & ~1  # half size is plenty for the panel
        procs: list[asyncio.subprocess.Process] = []
        pump: Optional[asyncio.Task[None]] = None
        try:
            record = await asyncio.create_subprocess_exec(
                self.adb_exe, "-s", self.serial, "exec-out", "screenrecord", "--output-format=h264",
                f"--size={out_w}x{out_h}", "--bit-rate=6000000", f"--time-limit={RECORD_LIMIT}", "-",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            procs.append(record)
            # No -fflags nobuffer: it drops the first key frame and screenrecord sends few of them.
            # -threads 1 on the encoder too, a threaded mjpeg encoder holds frames back.
            decode = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-flags", "low_delay", "-threads", "1",
                "-use_wallclock_as_timestamps", "1", "-probesize", "32", "-analyzeduration", "0",
                "-f", "h264", "-i", "-", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-threads", "1",
                "-flush_packets", "1", "-fps_mode", "passthrough", "-",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            procs.append(decode)
            pump = asyncio.create_task(self._pump(record, decode))
            assert decode.stdout is not None
            buf = b""
            idle_since: Optional[float] = None
            while True:
                try:
                    chunk = await asyncio.wait_for(decode.stdout.read(1 << 16), 1.0)
                except asyncio.TimeoutError:
                    chunk = None
                if chunk == b"":
                    return  # time limit reached or adb went away; Frames starts it again
                if chunk:
                    buf += chunk
                    while (end := buf.find(b"\xff\xd9")) >= 0:
                        start = buf.find(b"\xff\xd8")
                        if 0 <= start < end:
                            await frames.publish(buf[start:end + 2])
                        buf = buf[end + 2:]
                if not frames.idle():
                    idle_since = None
                elif idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > IDLE_STOP_SECS:
                    return
        finally:
            if pump is not None:
                pump.cancel()
            for proc in procs:
                await stop_process(proc)

    @staticmethod
    async def _pump(record: asyncio.subprocess.Process, decode: asyncio.subprocess.Process) -> None:
        """Copy H.264 from adb to ffmpeg. ffmpeg only knows a frame is complete when the next
        one starts, and a still screen sends no next frame; so after every burst of data an
        access unit delimiter goes in, which closes the frame right away."""
        assert record.stdout is not None and decode.stdin is not None
        pending: Optional[asyncio.Future[bytes]] = None
        dirty = False
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(record.stdout.read(1 << 16))
                done, _ = await asyncio.wait({pending}, timeout=0.03 if dirty else None)
                if not done:
                    decode.stdin.write(AUD)
                    dirty = False
                else:
                    data, pending = pending.result(), None
                    if not data:
                        return
                    decode.stdin.write(data)
                    dirty = True
                await decode.stdin.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            if pending is not None:
                pending.cancel()
            with contextlib.suppress(Exception):
                decode.stdin.close()

    def info(self) -> dict[str, Any]:
        return {"id": self.serial, "kind": "android", "name": self.name or self.serial, "meta": self.meta,
                "size": list(self.size) if self.size else None, "frame": None,
                "error": self.frames.error, "live": self.frames.frame is not None and self.frames.error is None,
                "stream": f"/android/{self.serial}/stream.mjpg"}


class Androids:
    """Every ready adb device (or only ANDROID_SERIAL), listed on demand."""

    def __init__(self, adb: str = "adb") -> None:
        self.adb = adb
        self.devices: dict[str, Android] = {}
        self._listed_at = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self) -> None:
        async with self._lock:
            if time.monotonic() - self._listed_at < ADB_LIST_TTL:
                return
            self._listed_at = time.monotonic()
            lines = (await run(self.adb, "devices")).splitlines()[1:]
            ready = [line.split()[0] for line in lines if line.endswith("\tdevice")]
            ready = [s for s in ready if SERIAL_RE.match(s) and (not SERIAL or s == SERIAL)]
            for serial in list(self.devices):
                if serial not in ready:
                    await self.devices.pop(serial).frames.close()
            for serial in ready:
                device = self.devices.setdefault(serial, Android(serial, self.adb))
                if device.name is None:
                    try:
                        await device.detect()
                    except Exception as e:
                        device.frames.error = first_line(e)

    def info(self) -> list[dict[str, Any]]:
        return [d.info() for d in self.devices.values()]


class Simulators:
    """Booted iOS simulators, each with its own screenshot loop."""

    def __init__(self) -> None:
        self.devices: dict[str, dict[str, Any]] = {}
        self.frames: dict[str, Frames] = {}
        self._listed_at = 0.0
        self._lock = asyncio.Lock()
        self._names: dict[str, str] = {}
        self._models: dict[str, str] = {}  # device type -> model identifier, e.g. iPhone18,3
        self.frames_meta: dict[str, Optional[dict[str, Any]]] = {}  # model identifier -> bezel

    async def refresh(self) -> None:
        async with self._lock:
            if time.monotonic() - self._listed_at < SIM_LIST_TTL:
                return
            self._listed_at = time.monotonic()
            await self._refresh()

    async def _refresh(self) -> None:
        if not self._names:
            for kind in ("devicetypes", "runtimes"):
                for item in json.loads(await run("xcrun", "simctl", "list", kind, "-j"))[kind]:
                    self._names[item["identifier"]] = item["name"]
                    if item.get("modelIdentifier"):
                        self._models[item["identifier"]] = item["modelIdentifier"]
        booted = json.loads(await run("xcrun", "simctl", "list", "devices", "booted", "-j"))["devices"]
        devices = {}
        for runtime, items in booted.items():
            for sim in items:
                udid = sim["udid"]
                model = self._names.get(sim.get("deviceTypeIdentifier", ""), "")
                model_id = self._models.get(sim.get("deviceTypeIdentifier", ""))
                if model_id and model_id not in self.frames_meta:
                    self.frames_meta[model_id] = await self._frame(model_id)
                os_name = self._names.get(runtime, runtime.rsplit(".", 1)[-1])
                old = self.devices.get(udid, {})
                devices[udid] = {"id": udid, "kind": "simulator", "name": sim["name"],
                                 "meta": " · ".join(p for p in (model, f"{os_name} simulator") if p),
                                 "frame": self.frames_meta.get(model_id or ""), "size": old.get("size"),
                                 "stream": f"/sim/{udid}/stream.mjpg"}
                if udid not in self.frames:
                    self.frames[udid] = Frames(lambda frames, udid=udid: self._capture(udid, frames))
        for udid in list(self.frames):
            if udid not in devices:
                await self.frames.pop(udid).close()
                self._shot_path(udid).unlink(missing_ok=True)
        self.devices = devices

    @staticmethod
    def _shot_path(udid: str) -> Path:
        return config.runtime_dir() / f"sim-{udid}.jpeg"

    async def _frame(self, model_id: str) -> Optional[dict[str, Any]]:
        try:
            return await asyncio.to_thread(device_frame.frame_meta, model_id)
        except Exception as e:
            logger.warning("frame for %s: %s", model_id, first_line(e))
            return None

    async def _capture(self, udid: str, frames: Frames) -> None:
        path = self._shot_path(udid)
        try:
            while not frames.idle() and not frames.closed:
                started = time.monotonic()
                await run("xcrun", "simctl", "io", udid, "screenshot", "--type=jpeg", str(path), timeout=15.0)
                jpeg = path.read_bytes()
                frames.error = None
                if jpeg != frames.frame:
                    if udid in self.devices and not self.devices[udid].get("size"):
                        self.devices[udid]["size"] = list(Image.open(io.BytesIO(jpeg)).size)
                    await frames.publish(jpeg)
                await asyncio.sleep(max(0.1, SIM_INTERVAL - (time.monotonic() - started)))
        finally:
            path.unlink(missing_ok=True)

    def info(self) -> list[dict[str, Any]]:
        out = []
        for udid, device in self.devices.items():
            frames = self.frames[udid]
            out.append({**device, "error": frames.error,
                        "live": frames.frame is not None and frames.error is None})
        return out


class SidePanels:
    """The stream server. GET only: /android/<serial>/ and /sim/<udid>/ followed by stream.mjpg
    (MJPEG while the client stays) or frame.jpg (the latest frame, 404 before the first one)."""

    def __init__(self, bind: str, port: int, page_port: int) -> None:
        self.bind, self.port = bind, port
        host = (f"[{bind}]" if ":" in bind else bind).lower()
        names = {"127.0.0.1", "localhost", host}
        self.page_origins = {f"http://{name}:{page_port}" for name in names}
        self.hosts = {f"{name}:{port}" for name in names}
        adb = adb_path()
        self.androids: Optional[Androids] = Androids(adb) if adb and shutil.which("ffmpeg") else None
        self.sims: Optional[Simulators] = Simulators() if shutil.which("xcrun") else None
        self._server: Optional[asyncio.Server] = None
        self._refreshing: dict[int, asyncio.Task[None]] = {}
        self._listed: dict[int, bool] = {}

    async def start(self) -> None:
        if self.androids is None:
            logger.warning("android panels disabled: adb not found (ADB, PATH, ANDROID_HOME) or ffmpeg not on PATH")
        self._server = await asyncio.start_server(self._client, self.bind, self.port)
        logger.info("android and simulator streams at http://%s:%s/", self.bind, self.port)

    async def devices(self) -> list[dict[str, Any]]:
        """Detected simulators and Android devices. Listing them takes a second or two, so after
        the first time it runs in the background and this answers with the last result."""
        out: list[dict[str, Any]] = []
        for source in (self.sims, self.androids):
            if source is None:
                continue
            task = self._refreshing.get(id(source))
            if task is None or task.done():
                task = asyncio.create_task(self._refresh(source))
                self._refreshing[id(source)] = task
            if not self._listed.get(id(source)):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(task), 30.0)
            out += source.info()
        return out

    async def _refresh(self, source: Any) -> None:
        try:
            await source.refresh()
        except Exception as e:
            logger.warning("%s list: %s", type(source).__name__, first_line(e))
        self._listed[id(source)] = True

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in self._refreshing.values():
            task.cancel()
        all_frames = [d.frames for d in self.androids.devices.values()] if self.androids else []
        all_frames += list(self.sims.frames.values()) if self.sims else []
        for frames in all_frames:
            await frames.close()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("stream request failed")
        finally:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        parts = (await reader.readline()).decode("latin-1").split()
        headers: dict[str, str] = {}
        while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1].split("?", 1)[0]
        origin = headers.get("origin")
        # Only the preview page may use this; also stops DNS-rebinding pages.
        if headers.get("host", "").lower() not in self.hosts or (
                origin not in (None, "null") and origin not in self.page_origins):
            return self._reply(writer, 403, b"forbidden")
        cors = origin if origin in self.page_origins else None
        if method != "GET":
            return self._reply(writer, 405, b"method not allowed", cors=cors)
        kind, _, rest = path.lstrip("/").partition("/")
        dev_id, _, leaf = rest.partition("/")
        if kind == "android" and self.androids is not None and dev_id in self.androids.devices:
            frames = self.androids.devices[dev_id].frames
        elif kind == "sim" and self.sims is not None and dev_id in self.sims.devices:
            frames = self.sims.frames[dev_id]
        else:
            return self._reply(writer, 404, b"not found", cors=cors)
        if leaf == "stream.mjpg":
            return await frames.stream(writer, self._stream_head(cors))
        if leaf == "frame.jpg" and frames.frame is not None:
            return self._reply(writer, 200, frames.frame, b"image/jpeg", cors)
        return self._reply(writer, 404, b"not found", cors=cors)

    @staticmethod
    def _stream_head(cors: Optional[str]) -> bytes:
        head = (b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=" + BOUNDARY +
                b"\r\nCache-Control: no-store\r\nConnection: close\r\n")
        if cors:
            head += b"Access-Control-Allow-Origin: " + cors.encode() + b"\r\n"
        return head

    @staticmethod
    def _reply(writer: asyncio.StreamWriter, status: int, body: bytes,
               content_type: bytes = b"text/plain", cors: Optional[str] = None) -> None:
        head = (f"HTTP/1.1 {status} X\r\nContent-Type: {content_type.decode()}\r\n"
                f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n")
        if cors:
            head += f"Access-Control-Allow-Origin: {cors}\r\n"
        writer.write(head.encode() + b"\r\n" + body)
