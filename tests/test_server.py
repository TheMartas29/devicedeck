"""Offline tests for phoned, preview.py and config.py: the input lock, config saving, request
checks and subprocess cleanup. No device is touched.
Run: venv/bin/python -m unittest discover -s tests
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import phoned
import preview

PORT = 7999
HOST = f"127.0.0.1:{PORT}"


class ConfigFile(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "devices.json"
        patcher = mock.patch.object(config, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DEVICEDECK_LOCK_PHYSICAL_INPUT": ""})
        env.start()
        self.addCleanup(env.stop)

    def write(self, data) -> None:
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))


class InputLock(ConfigFile):
    def entry(self, **extra):
        return {"devices": [{"id": "00008101-ABC", "kind": "ios", **extra}]}

    def test_missing_file_allows(self):
        self.assertIsNone(config.input_lock("00008101-ABC"))

    def test_no_allow_input_key_allows(self):
        self.write(self.entry())
        self.assertIsNone(config.input_lock("00008101-ABC"))

    def test_true_values_allow(self):
        for value in (True, 1, "true", "Yes", " on ", "1"):
            self.write(self.entry(allowInput=value))
            self.assertIsNone(config.input_lock("00008101-ABC"), value)

    def test_false_like_values_lock(self):
        for value in (False, 0, "false", "no", "0", None, "", [], 2):
            self.write(self.entry(allowInput=value))
            self.assertEqual(config.input_lock("00008101-ABC"), "config", value)

    def test_id_case_insensitive_and_any_kind(self):
        self.write({"devices": [{"id": " 00008101-abc ", "kind": "android", "allowInput": False}]})
        self.assertEqual(config.input_lock("00008101-ABC"), "config")
        self.assertIsNone(config.input_lock("00008101-ABD"))

    def test_entry_dropped_by_normalize_still_counts(self):
        self.write({"devices": [{"id": "00008101-ABC", "kind": "IOS", "allowInput": "no"}]})
        self.assertEqual(config.input_lock("00008101-abc"), "config")

    def test_unreadable_file_locks(self):
        self.write("{not json")
        self.assertEqual(config.input_lock("00008101-ABC"), "config unreadable")

    def test_env_locks_physical_only(self):
        with mock.patch.dict(os.environ, {"DEVICEDECK_LOCK_PHYSICAL_INPUT": "1"}):
            self.assertEqual(config.input_lock("00008101-ABC"), "env")
            self.assertIsNone(config.input_lock("emulator-5554", physical=False))


class SaveDevices(ConfigFile):
    def test_keeps_unmanaged_keys_and_omitted_entries(self):
        self.write({"theme": "x", "devices": [
            {"id": "A1", "kind": "IOS", "name": "Mine", "allowInput": False, "wdaPort": 8101},
            {"id": "B2", "kind": "android", "allowInput": False}]})
        config.save_devices({"devices": [{"id": "a1", "kind": "ios", "name": "Renamed", "allowInput": True}]})
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["theme"], "x")
        first, second = saved["devices"]
        self.assertEqual((first["name"], first["allowInput"], first["wdaPort"]), ("Renamed", False, 8101))
        self.assertEqual((second["id"], second["allowInput"]), ("B2", False))
        self.assertEqual(config.input_lock("A1"), "config")

    def test_refuses_to_overwrite_broken_file(self):
        self.write('{"devices": [{"id": "A1", "kind": "phone"}]}')
        before = self.path.read_text()
        with self.assertRaises(config.ConfigUnreadable):
            config.save_devices({"devices": []})
        self.assertEqual(self.path.read_text(), before)

    def test_kind_case_insensitive(self):
        self.write({"devices": [{"id": "A1", "kind": " Android "}]})
        self.assertEqual(config.load_devices()[0]["kind"], "android")
        self.assertIsNone(config.config_error())


class FakeWriter:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def status(self) -> int:
        return int(self.data.split(b" ", 2)[1]) if self.data else 0


class FakeHub:
    panels = None

    def __init__(self) -> None:
        self.changed = 0

    def ios_devices(self):
        return []

    def config_changed(self) -> None:
        self.changed += 1

    def worker_http_socket(self, udid):
        return None


def request(method: str, path: str, headers: dict, body: bytes = b"") -> bytes:
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return head.encode("latin-1") + b"\r\n" + body


class PreviewRequests(ConfigFile):
    def serve(self, raw: bytes) -> FakeWriter:
        async def go() -> FakeWriter:
            server = preview.Preview(self.hub, "127.0.0.1", PORT, None)
            reader = asyncio.StreamReader(limit=2 ** 16)
            reader.feed_data(raw)
            reader.feed_eof()
            writer = FakeWriter()
            await server._client(reader, writer)
            return writer

        return asyncio.run(go())

    def setUp(self) -> None:
        super().setUp()
        self.hub = FakeHub()

    def post_config(self, body: dict, **headers) -> FakeWriter:
        data = json.dumps(body).encode()
        hdrs = {"Host": HOST, "Content-Type": "application/json", "Content-Length": len(data), **headers}
        return self.serve(request("POST", "/api/config", hdrs, data))

    def test_save_works(self):
        writer = self.post_config({"devices": [{"id": "A1", "kind": "ios"}]},
                                  Origin=f"http://{HOST}")
        self.assertEqual(writer.status(), 200)
        self.assertEqual(self.hub.changed, 1)

    def test_409_while_file_does_not_load(self):
        self.write("{broken")
        writer = self.post_config({"devices": [{"id": "A1", "kind": "ios"}]})
        self.assertEqual(writer.status(), 409)
        self.assertIn(b'"error"', writer.data)
        self.assertEqual(self.path.read_text(), "{broken")
        self.assertEqual(self.hub.changed, 0)

    def test_origin_null_post_refused(self):
        writer = self.post_config({"devices": []}, Origin="null")
        self.assertEqual(writer.status(), 403)
        self.assertFalse(self.path.exists())

    def test_origin_null_get_allowed(self):
        writer = self.serve(request("GET", "/api/config", {"Host": HOST, "Origin": "null"}))
        self.assertEqual(writer.status(), 200)

    def test_foreign_origin_refused(self):
        writer = self.post_config({"devices": []}, Origin="http://evil.example")
        self.assertEqual(writer.status(), 403)

    def test_bad_content_length(self):
        for value in ("abc", "-1", "1e3", "+5"):
            writer = self.serve(request("POST", "/api/config", {"Host": HOST, "Content-Length": value}))
            self.assertEqual(writer.status(), 400, value)

    def test_long_header(self):
        writer = self.serve(request("GET", "/", {"Host": HOST, "X-Big": "a" * 70000}))
        self.assertEqual(writer.status(), 400)

    def test_long_request_line(self):
        writer = self.serve(request("GET", "/" + "a" * 70000, {"Host": HOST}))
        self.assertEqual(writer.status(), 400)


class HubInputLock(ConfigFile):
    def make_hub(self):
        hub = phoned.Hub(Path(self.tmp.name) / "s.sock", None, 0, None)
        worker = phoned.Worker.__new__(phoned.Worker)
        worker.udid = "00008101-ABC"
        worker.status = {}
        worker.request = mock.AsyncMock(return_value={"ok": True})
        hub.workers[worker.udid] = worker
        return hub, worker

    def test_locked_device_refuses_input(self):
        self.write({"devices": [{"id": "00008101-abc", "kind": "ios", "allowInput": False}]})
        hub, worker = self.make_hub()
        for line in ("tap 1 2", "drag 1 2 3 4", 'type "x"', "key enter", "button home", "@00008101-ABC tap 1 2"):
            with self.assertRaises(phoned.InputLocked):
                asyncio.run(hub.execute(line))
        worker.request.assert_not_called()
        asyncio.run(hub.execute("shot /tmp/x.png"))
        worker.request.assert_called_once()

    def test_env_lock(self):
        hub, worker = self.make_hub()
        with mock.patch.dict(os.environ, {"DEVICEDECK_LOCK_PHYSICAL_INPUT": "1"}):
            with self.assertRaises(phoned.InputLocked) as ctx:
                asyncio.run(hub.execute("tap 1 2"))
        self.assertEqual(ctx.exception.reason, "env")
        worker.request.assert_not_called()

    def test_unlocked_device_forwards(self):
        hub, worker = self.make_hub()
        asyncio.run(hub.execute("tap 1 2"))
        worker.request.assert_called_once_with("tap 1 2")


class Subprocesses(unittest.TestCase):
    def test_run_kills_child_on_cancel(self):
        procs = []
        real = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            proc = await real(*args, **kwargs)
            procs.append(proc)
            return proc

        async def go():
            with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
                task = asyncio.create_task(phoned._run("sleep", "30"))
                while not procs:
                    await asyncio.sleep(0.01)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            return procs[0]

        proc = asyncio.run(go())
        self.assertIsNotNone(proc.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(proc.pid, 0)

    def test_run_kills_child_on_timeout(self):
        async def go():
            with self.assertRaises(asyncio.TimeoutError):
                await phoned._run("sleep", "30", timeout=0.2)

        asyncio.run(go())


class Misc(unittest.TestCase):
    def test_check_bind(self):
        for ok in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
            self.assertEqual(phoned.check_bind(ok), ok)
        for bad in ("0.0.0.0", "::", "192.168.1.2", "example.com"):
            with self.assertRaises(Exception):
                phoned.check_bind(bad)

    def test_rotate_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "w.log"
            log.write_bytes(b"x" * 100)
            phoned.rotate_log(log, limit=200)
            self.assertTrue(log.exists())
            phoned.rotate_log(log, limit=50)
            self.assertFalse(log.exists())
            self.assertEqual((Path(tmp) / "w.log.1").stat().st_size, 100)
            phoned.rotate_log(log, limit=50)


class WorkerClose(unittest.TestCase):
    def test_close_never_raises(self):
        import ios_worker

        async def go():
            session = ios_worker.Session("X", Path("/nonexistent"))
            session._close = mock.AsyncMock(side_effect=AttributeError("close_clients"))
            await session.close()

        with self.assertLogs("ios_worker", "WARNING"):
            asyncio.run(go())

    def test_close_without_close_clients(self):
        import ios_worker

        class Server:
            def close(self):
                pass

        async def go():
            session = ios_worker.Session("X", Path("/nonexistent"), screenshots=True)
            session.http = Server()
            writer = FakeWriter()
            session._frame_writers.add(writer)
            await session._close()
            return writer

        self.assertTrue(asyncio.run(go()).closed)


if __name__ == "__main__":
    unittest.main()
