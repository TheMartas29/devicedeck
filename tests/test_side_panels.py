"""Tests for side_panels.py: child processes die with their task, a closed Frames stays closed,
and adb is found like the deck CLI finds it. No device is touched."""
import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import side_panels  # noqa: E402


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class Spawned:
    """Records every process side_panels starts."""

    def __init__(self) -> None:
        self.procs: list[asyncio.subprocess.Process] = []
        self._real = asyncio.create_subprocess_exec

    async def __call__(self, *args, **kw):
        proc = await self._real(*args, **kw)
        self.procs.append(proc)
        return proc


class FakeWriter:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_kills_child(self):
        spawned = Spawned()
        with mock.patch.object(side_panels.asyncio, "create_subprocess_exec", spawned):
            task = asyncio.create_task(side_panels.run("sleep", "30", timeout=60))
            while not spawned.procs:
                await asyncio.sleep(0.01)
            pid = spawned.procs[0].pid
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(spawned.procs[0].returncode)
        self.assertFalse(alive(pid))

    async def test_timeout_kills_child(self):
        spawned = Spawned()
        with mock.patch.object(side_panels.asyncio, "create_subprocess_exec", spawned):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await side_panels.run("sleep", "30", timeout=0.2)
        self.assertIsNotNone(spawned.procs[0].returncode)
        self.assertFalse(alive(spawned.procs[0].pid))

    async def test_output_and_failure(self):
        self.assertEqual(await side_panels.run("echo", "hi"), "hi\n")
        with self.assertRaises(RuntimeError):
            await side_panels.run("false")


class FramesTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_kills_capture_and_ends_stream(self):
        spawned = Spawned()

        async def capture(frames):
            await frames.publish(b"\xff\xd8x\xff\xd9")
            await side_panels.run("sleep", "30", timeout=60)

        with mock.patch.object(side_panels.asyncio, "create_subprocess_exec", spawned):
            frames = side_panels.Frames(capture)
            writer = FakeWriter()
            viewer = asyncio.create_task(frames.stream(writer, b"HTTP/1.1 200 OK\r\n"))
            while not spawned.procs:
                await asyncio.sleep(0.01)
            await frames.close()
            await asyncio.wait_for(viewer, 2.0)
        self.assertTrue(frames.closed)
        self.assertFalse(alive(spawned.procs[0].pid))
        self.assertEqual(frames.viewers, 0)
        frames._ensure_capture()
        self.assertTrue(frames._task is None or frames._task.done())
        late = FakeWriter()
        await asyncio.wait_for(frames.stream(late, b"HTTP/1.1 200 OK\r\n"), 1.0)
        self.assertEqual(late.data, b"")
        self.assertEqual(len(spawned.procs), 1)


class SimulatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_removes_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(config, "RUNTIME_DIR", Path(tmp)):
            path = Path(tmp) / "sim-UDID.jpeg"
            started = asyncio.Event()

            async def fake_run(*args, timeout=10.0):
                Path(args[-1]).write_bytes(b"partial")
                started.set()
                await asyncio.sleep(30)

            sims = side_panels.Simulators()
            frames = side_panels.Frames(lambda f: sims._capture("UDID", f))
            frames.viewers = 1
            with mock.patch.object(side_panels, "run", fake_run):
                frames._ensure_capture()
                await asyncio.wait_for(started.wait(), 2.0)
                self.assertTrue(path.exists())
                await frames.close()
            self.assertFalse(path.exists())


class AllowlistTests(unittest.TestCase):
    def panels(self, bind: str) -> side_panels.SidePanels:
        with mock.patch.object(side_panels, "adb_path", return_value=None), \
                mock.patch.object(side_panels.shutil, "which", return_value=None):
            return side_panels.SidePanels(bind, 7318, 7317)

    def test_default_bind(self):
        p = self.panels("127.0.0.1")
        self.assertEqual(p.hosts, {"127.0.0.1:7318", "localhost:7318"})
        self.assertEqual(p.page_origins, {"http://127.0.0.1:7317", "http://localhost:7317"})

    def test_ipv6_bind(self):
        p = self.panels("::1")
        self.assertIn("[::1]:7318", p.hosts)
        self.assertIn("http://[::1]:7317", p.page_origins)
        self.assertIn("127.0.0.1:7318", p.hosts)


class AdbPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.empty_path = Path(self.tmp.name) / "bin"
        self.empty_path.mkdir()

    def env(self, **kw):
        base = {"PATH": str(self.empty_path), "HOME": str(self.home)}
        base.update(kw)
        return mock.patch.dict(os.environ, base, clear=True)

    def make_adb(self, root: Path) -> Path:
        adb = root / "platform-tools" / "adb"
        adb.parent.mkdir(parents=True)
        adb.write_text("#!/bin/sh\n")
        adb.chmod(adb.stat().st_mode | stat.S_IXUSR)
        return adb

    def test_env_override_first(self):
        on_path = self.empty_path / "adb"
        on_path.write_text("#!/bin/sh\n")
        on_path.chmod(0o755)
        with self.env(ADB="/custom/adb"):
            self.assertEqual(side_panels.adb_path(), "/custom/adb")
        with self.env():
            self.assertEqual(side_panels.adb_path(), str(on_path))

    def test_android_home_then_sdk_root_then_default(self):
        home_adb = self.make_adb(Path(self.tmp.name) / "sdk1")
        root_adb = self.make_adb(Path(self.tmp.name) / "sdk2")
        default_adb = self.make_adb(self.home / "Library/Android/sdk")
        with self.env(ANDROID_HOME=str(home_adb.parent.parent), ANDROID_SDK_ROOT=str(root_adb.parent.parent)):
            self.assertEqual(side_panels.adb_path(), str(home_adb))
        with self.env(ANDROID_SDK_ROOT=str(root_adb.parent.parent)):
            self.assertEqual(side_panels.adb_path(), str(root_adb))
        with self.env():
            self.assertEqual(side_panels.adb_path(), str(default_adb))

    def test_missing(self):
        with self.env():
            self.assertIsNone(side_panels.adb_path())


if __name__ == "__main__":
    unittest.main()
