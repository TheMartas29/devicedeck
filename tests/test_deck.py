"""Offline tests for deck: selection, config overlay and the input lock.

No device is touched: discovery and backends are replaced by fakes.
Run: venv/bin/python -m unittest discover -s tests
"""
import base64
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

DECK = Path(__file__).resolve().parent.parent / "deck"
loader = importlib.machinery.SourceFileLoader("deck", str(DECK))
spec = importlib.util.spec_from_loader("deck", loader)
deck = importlib.util.module_from_spec(spec)
loader.exec_module(deck)


class FakeIOS(deck.IOSDevice):
    def __init__(self, *a):
        super().__init__(*a)
        self.calls = []

    def tap(self, x, y):
        self.calls.append(("tap", x, y))
        return "points"

    def key(self, name):
        self.calls.append(("key", name))
        return "pressed"


class FakeSim(deck.SimDevice):
    def __init__(self, *a):
        super().__init__(*a)
        self.calls = []

    def tap(self, x, y):
        self.calls.append(("tap", x, y))
        return "points"


class FakeAndroid(deck.AndroidDevice):
    def __init__(self, *a, physical=False):
        super().__init__(*a)
        self.physical = physical
        self.shell = []

    def sh(self, cmd, **kw):
        self.shell.append(cmd)
        return ""


def fleet():
    phone = FakeIOS("00008101-AAAA", "Owner - iPhone", "iPhone 12 mini", "iOS 27.2", "connected")
    spare = FakeIOS("00008101-BBBB", "spare - iPhone", "iPhone 12", "iOS 26.7", "unavailable")
    sim = FakeSim("51B9-SIM", "TCMOB-mock", "iPhone 17", "iOS 27.2", "booted")
    emu = FakeAndroid("emulator-5554", "iw-test", "sdk", "Android 13", "device")
    usb = FakeAndroid("R58M123", "Pixel", "Pixel 7", "Android 14", "device", physical=True)
    return [phone, spare, sim, emu, usb]


def call(argv, devices, env=None):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(deck, "discover", return_value=devices), \
            mock.patch.dict(os.environ, env or {}), redirect_stdout(out), redirect_stderr(err):
        if "DEVICEDECK_LOCK_PHYSICAL_INPUT" not in (env or {}):
            os.environ.pop("DEVICEDECK_LOCK_PHYSICAL_INPUT", None)
        try:
            code = deck.main(argv)
        except deck.DeckError as e:
            print(f"deck: {e}", file=err)
            code = e.code
    return code, out.getvalue(), err.getvalue()


class Selection(unittest.TestCase):
    def test_by_id_case_insensitive(self):
        devs = fleet()
        self.assertIs(deck.select(devs, "EMULATOR-5554"), devs[3])

    def test_by_substring(self):
        devs = fleet()
        self.assertIs(deck.select(devs, "mock"), devs[2])

    def test_ambiguous_substring_exits_2(self):
        with self.assertRaises(deck.DeckError) as cm:
            deck.select(fleet(), "iphone")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("Owner - iPhone", str(cm.exception))
        self.assertIn("spare - iPhone", str(cm.exception))

    def test_omitted_with_several_connected_exits_2(self):
        with self.assertRaises(deck.DeckError) as cm:
            deck.select(fleet(), None)
        self.assertEqual(cm.exception.code, 2)

    def test_omitted_with_one_connected(self):
        devs = fleet()
        only = [devs[1], devs[2]]
        self.assertIs(deck.select(only, None), devs[2])

    def test_hidden_only_affects_the_preview(self):
        devs = fleet()
        devs[2].hidden = True
        self.assertIs(deck.select(devs, "mock"), devs[2])
        self.assertIs(deck.select(devs, "51b9-sim"), devs[2])


class Guard(unittest.TestCase):
    def test_physical_input_allowed_by_default(self):
        devs = fleet()
        code, out, _ = call(["owner", "tap", "1", "2"], devs)
        self.assertEqual(code, 0)
        self.assertEqual(devs[0].calls, [("tap", 1.0, 2.0)])
        self.assertIn("(points)", out)
        self.assertEqual(call(["pixel", "tap", "1", "2"], devs)[0], 0)
        self.assertEqual(devs[4].shell, ["input tap 1 2"])

    def test_config_allow_input_false_locks(self):
        devs = fleet()
        devs[0].allow_input = False
        for argv in (["owner", "tap", "1", "2"], ["owner", "key", "home"], ["owner", "type", "x"],
                     ["owner", "swipe", "1", "2", "3", "4"], ["owner", "launch", "com.x"]):
            code, _, err = call(argv, devs)
            self.assertEqual(code, 3, err)
            self.assertIn("allowInput", err)
        self.assertEqual(devs[0].calls, [])

    def test_config_lock_applies_to_emulator_too(self):
        devs = fleet()
        devs[3].allow_input = False
        self.assertEqual(call(["iw-test", "tap", "1", "2"], devs)[0], 3)
        self.assertEqual(devs[3].shell, [])

    def test_env_locks_physical_only(self):
        devs = fleet()
        env = {"DEVICEDECK_LOCK_PHYSICAL_INPUT": "1"}
        code, _, err = call(["owner", "tap", "1", "2"], devs, env)
        self.assertEqual(code, 3)
        self.assertIn("DEVICEDECK_LOCK_PHYSICAL_INPUT", err)
        self.assertEqual(call(["pixel", "tap", "1", "2"], devs, env)[0], 3)
        self.assertEqual(devs[0].calls, [])
        self.assertEqual(devs[4].shell, [])
        self.assertEqual(call(["mock", "tap", "5", "6"], devs, env)[0], 0)
        self.assertEqual(call(["iw-test", "tap", "5", "6"], devs, env)[0], 0)

    def test_env_other_value_does_not_lock(self):
        devs = fleet()
        self.assertEqual(call(["owner", "tap", "1", "2"], devs, {"DEVICEDECK_LOCK_PHYSICAL_INPUT": "yes"})[0], 0)

    def test_read_only_commands_ignore_lock(self):
        devs = fleet()
        devs[3].allow_input = False
        with mock.patch.object(FakeAndroid, "info", return_value=[("id", "emulator-5554")]):
            self.assertEqual(call(["iw-test", "info"], devs)[0], 0)

    def test_list_input_column(self):
        devs = fleet()
        devs[0].allow_input = False
        table = deck.table(devs)
        row = next(line for line in table.splitlines() if line.startswith("00008101-AAAA"))
        self.assertTrue(row.endswith("locked"))
        self.assertTrue(next(line for line in table.splitlines() if line.startswith("R58M123")).endswith("yes"))

    def test_bad_args_fail_before_device(self):
        devs = fleet()
        code, _, _ = call(["mock", "tap", "x", "2"], devs)
        self.assertEqual(code, 2)
        self.assertEqual(devs[2].calls, [])


class Android(unittest.TestCase):
    def test_type_escapes_for_device_shell(self):
        emu = fleet()[3]
        emu.type_text("a b;c'd")
        self.assertEqual(emu.shell, ["input text 'a%sb;c'\"'\"'d'"])

    def test_type_rejects_non_ascii(self):
        with self.assertRaises(deck.DeckError):
            fleet()[3].type_text("čau")

    def test_launch_rejects_shell_injection(self):
        with self.assertRaises(deck.DeckError):
            fleet()[3].launch("com.x; reboot")


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (390).to_bytes(4, "big") + (844).to_bytes(4, "big") + b"rest"
SOURCE = {"type": "XCUIElementTypeApplication", "label": "Settings", "name": "Settings",
          "rect": {"x": 0, "y": 0, "width": 390, "height": 844}, "children": [
              {"type": "XCUIElementTypeButton", "label": "General", "name": "com.apple.settings.general",
               "rect": {"x": 16, "y": 380, "width": 358, "height": 52}, "isVisible": "1", "children": []},
              {"type": "XCUIElementTypeTextField", "label": None, "name": None, "value": "Search",
               "rect": {"x": 28, "y": 808, "width": 334, "height": 28}, "children": []},
              {"type": "XCUIElementTypeOther", "label": "hidden", "rect": {"x": 0, "y": 0, "width": 0, "height": 0}},
          ]}


class FakeWDAHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, value, session=None, code=200):
        body = json.dumps({"value": value, "sessionId": session}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        srv = self.server
        srv.calls.append(("GET", self.path, None))
        if self.path == "/status":
            self.reply({"ready": True}, srv.sid)
        elif self.path == "/screenshot":
            self.reply(base64.b64encode(PNG).decode())
        elif self.path.startswith("/source"):
            self.reply(SOURCE)
        elif self.path == "/window/size":
            self.reply({"width": 390, "height": 844})
        else:
            self.reply({"error": "unknown command", "message": self.path}, code=404)

    def do_POST(self):
        srv = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        srv.calls.append(("POST", self.path, body))
        if self.path == "/session":
            srv.sid = "S1"
            self.reply({"sessionId": "S1", "capabilities": {}}, "S1")
        elif self.path.endswith("/wda/apps/launch") and body.get("bundleId") == "com.missing":
            self.reply({"error": "invalid argument", "message": "app com.missing is not installed"}, code=400)
        else:
            self.reply(None, srv.sid)


class WDABackend(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWDAHandler)
        self.server.calls, self.server.sid = [], None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.dev = deck.IOSDevice("00008101-TEST", "test phone", "iPhone 12", "iOS 26.7.2", "connected")
        self.dev.config = {"wdaUrl": self.url}
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("DEVICEDECK_IOS_BACKEND", "DEVICEDECK_WDA_URL", "DEVICEDECK_LOCK_PHYSICAL_INPUT"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()

    def posts(self):
        return [(p, b) for m, p, b in self.server.calls if m == "POST"]

    def test_backend_choice_by_os_version(self):
        self.assertEqual(self.dev.backend(), "wda")
        new = deck.IOSDevice("x", "n", "m", "iOS 27.2", "connected")
        self.assertEqual(new.backend(), "phoned")
        os.environ["DEVICEDECK_IOS_BACKEND"] = "phoned"
        self.assertEqual(self.dev.backend(), "phoned")
        os.environ["DEVICEDECK_IOS_BACKEND"] = "wda"
        self.assertEqual(new.backend(), "wda")
        del os.environ["DEVICEDECK_IOS_BACKEND"]
        new.config = {"iosBackend": "wda"}
        self.assertEqual(new.backend(), "wda")

    def test_tap_creates_session_once_and_sends_points(self):
        self.assertEqual(self.dev.tap(201, 406), "points")
        self.dev.tap(10, 20)
        posts = self.posts()
        self.assertEqual([p for p, _ in posts].count("/session"), 1)
        acts = [b for p, b in posts if p == "/session/S1/actions"]
        self.assertEqual(len(acts), 2)
        first = acts[0]["actions"][0]
        self.assertEqual(first["parameters"], {"pointerType": "touch"})
        self.assertEqual(first["actions"][0], {"type": "pointerMove", "duration": 0, "x": 201, "y": 406})
        self.assertEqual([a["type"] for a in first["actions"]], ["pointerMove", "pointerDown", "pause", "pointerUp"])

    def test_swipe_duration(self):
        self.dev.swipe(200, 700, 200, 300, 450)
        acts = [b for p, b in self.posts() if p.endswith("/actions")][0]["actions"][0]["actions"]
        self.assertEqual(acts[3], {"type": "pointerMove", "duration": 450, "x": 200, "y": 300})

    def test_type_unicode(self):
        self.dev.type_text("čau 1")
        self.assertIn(("/session/S1/wda/keys", {"value": list("čau 1")}), self.posts())

    def test_keys(self):
        for name in ("home", "lock", "volume-up", "volume-down", "enter", "backspace"):
            self.dev.key(name)
        paths = [(p, b) for p, b in self.posts() if p != "/session" and not p.endswith("/appium/settings")]
        self.assertEqual(paths, [
            ("/wda/homescreen", None), ("/wda/lock", None),
            ("/session/S1/wda/pressButton", {"name": "volumeup"}),
            ("/session/S1/wda/pressButton", {"name": "volumedown"}),
            ("/session/S1/wda/keys", {"value": ["\n"]}), ("/session/S1/wda/keys", {"value": ["\b"]})])
        with self.assertRaises(deck.DeckError) as cm:
            self.dev.key("back")
        self.assertEqual(cm.exception.code, 2)

    def test_shot_writes_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.png"
            self.dev.shot(path)
            self.assertEqual(deck.png_size(path), (390, 844))

    def test_cli_shot_prints_points_and_scale(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(deck.WDA, "window_size", return_value=(130.0, 281.33)):
            code, out, err = call(["test phone", "shot", str(Path(tmp) / "s.png")], [self.dev])
        self.assertEqual(code, 0, err)
        self.assertIn("390x844 px = 130x281.3 pt @3x", out)

    def test_elements_frames_in_points(self):
        lines = self.dev.elements()
        self.assertIn('  Button "General" #com.apple.settings.general @195,406 [16,380 358x52]', lines)
        self.assertIn('  TextField ="Search" @195,822 [28,808 334x28]', lines)
        self.assertFalse(any("hidden" in line for line in lines))
        self.assertTrue(lines[-1].startswith("# physical iOS via WebDriverAgent"))

    def test_launch_and_error_message(self):
        self.assertEqual(self.dev.launch("com.apple.Preferences"), "launched com.apple.Preferences")
        app = [p for p, b in self.posts() if b == {"bundleId": "com.apple.Preferences"}]
        self.assertEqual(app, ["/session/S1/wda/apps/terminate", "/session/S1/wda/apps/launch"])
        with self.assertRaises(deck.DeckError) as cm:
            self.dev.launch("com.missing")
        self.assertIn("not installed", str(cm.exception))

    def test_existing_session_reused(self):
        self.server.sid = "LIVE"
        self.dev.tap(1, 2)
        self.assertNotIn("/session", [p for p, _ in self.posts()])
        self.assertIn("/session/LIVE/actions", [p for p, _ in self.posts()])

    def test_info_reports_wda(self):
        with mock.patch.object(deck, "ios_display", return_value=None):
            info = dict(self.dev.info())
        self.assertEqual(info["backend"], "wda")
        self.assertEqual(info["wda points"], "390x844 pt")
        self.assertTrue(info["wda"].startswith("ready at http://127.0.0.1:"))

    def test_external_url_down_is_clear_error(self):
        self.dev.config = {"wdaUrl": "http://127.0.0.1:9"}
        with self.assertRaises(deck.DeckError) as cm:
            self.dev.tap(1, 2)
        self.assertIn("no WebDriverAgent answers at http://127.0.0.1:9", str(cm.exception))

    def test_managed_without_build_points_to_setup(self):
        self.dev.config = {}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"DEVICEDECK_RUNTIME_DIR": tmp}):
            wda = deck.WDA(self.dev)
            wda.port = 9  # nothing listens there
            wda.url = "http://127.0.0.1:9"
            with self.assertRaises(deck.DeckError) as cm:
                wda.ensure()
        self.assertIn("wda-setup", str(cm.exception))

    def test_port_is_derived_from_udid_not_fixed(self):
        a = deck.WDA(deck.IOSDevice("AAA", "", "", "iOS 26", "connected"))
        b = deck.WDA(deck.IOSDevice("BBB", "", "", "iOS 26", "connected"))
        self.assertNotEqual(a.port, b.port)
        self.assertTrue(8200 <= a.port < 8800)
        self.assertEqual(deck.WDA(deck.IOSDevice("AAA", "", "", "iOS 26", "connected")).port, a.port)

    def test_cli_tap_through_main(self):
        code, out, err = call(["test phone", "tap", "5", "6"], [self.dev])
        self.assertEqual(code, 0, err)
        self.assertIn("(points)", out)


class WDASetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dev = deck.IOSDevice("00008101-TEST", "test phone", "iPhone 12", "iOS 26.7.2", "connected")
        self.env = mock.patch.dict(os.environ, {"DEVICEDECK_RUNTIME_DIR": self.tmp.name})
        self.env.start()
        for k in ("DEVICEDECK_WDA_TEAM", "DEVICEDECK_WDA_BUNDLE_ID", "DEVICEDECK_WDA_PROJECT",
                  "DEVICEDECK_WDA_XCTESTRUN", "DEVICEDECK_WDA_URL", "DEVICEDECK_LOCK_PHYSICAL_INPUT"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_missing_settings_listed(self):
        with mock.patch.object(deck.WDA, "project", return_value=None):
            code, _, err = call(["test phone", "wda-setup"], [self.dev])
        self.assertEqual(code, 2)
        for name in ("DEVICEDECK_WDA_TEAM", "DEVICEDECK_WDA_BUNDLE_ID", "DEVICEDECK_WDA_PROJECT"):
            self.assertIn(name, err)

    def test_build_command_uses_config_values(self):
        project = Path(self.tmp.name) / "WDA"
        (project / "WebDriverAgent.xcodeproj").mkdir(parents=True)
        self.dev.config = {"wdaTeam": "TEAM123", "wdaBundleId": "com.example.wda", "wdaProject": str(project)}
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            products = Path(self.tmp.name) / "wda/DerivedData/Build/Products"
            products.mkdir(parents=True, exist_ok=True)
            (products / "WebDriverAgentRunner_iphoneos26.0-arm64.xctestrun").write_text("x")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(deck, "run", side_effect=fake_run), \
                mock.patch.object(deck.WDA, "start") as start, mock.patch.object(deck.WDA, "stop"):
            code, out, err = call(["test phone", "wda-setup"], [self.dev])
        self.assertEqual(code, 0, err)
        start.assert_called_once()
        cmd = seen["cmd"]
        self.assertEqual(cmd[:2], ["xcodebuild", "build-for-testing"])
        for arg in ("-allowProvisioningUpdates", "DEVELOPMENT_TEAM=TEAM123",
                    "PRODUCT_BUNDLE_IDENTIFIER=com.example.wda", "id=00008101-TEST"):
            self.assertIn(arg, cmd)
        self.assertIn("WebDriverAgent is ready", out)

    def test_untrusted_certificate_prints_steps(self):
        wda = deck.WDA(self.dev)
        products = Path(self.tmp.name) / "wda/DerivedData/Build/Products"
        products.mkdir(parents=True)
        (products / "WebDriverAgentRunner_iphoneos26.0-arm64.xctestrun").write_text("x")
        wda.url = "http://127.0.0.1:9"

        def fake_spawn(cmd, log):
            log.write_text("The application could not be launched because the Developer App Certificate "
                           "is not trusted.")
            return 4242

        with mock.patch.object(wda, "spawn", side_effect=fake_spawn), \
                mock.patch.object(deck, "pid_alive", return_value=False), \
                mock.patch.object(deck, "which", return_value="/bin/echo"):
            with self.assertRaises(deck.DeckError) as cm:
                wda.start()
        self.assertIn("VPN & Device Management", str(cm.exception))
        self.assertIn("Enable UI Automation", str(cm.exception))

    def test_setup_refused_on_locked_device(self):
        self.dev.allow_input = False
        code, _, err = call(["test phone", "wda-setup"], [self.dev])
        self.assertEqual(code, 3)

    def test_setup_only_for_ios(self):
        sim = FakeSim("SIM", "sim", "iPhone 17", "iOS 27.2", "booted")
        self.assertEqual(call(["sim", "wda-setup"], [sim])[0], 2)


class ShotSize(unittest.TestCase):
    def test_portrait_and_landscape(self):
        self.assertEqual(deck.shot_size((1170, 2532), (390, 844), "n"), "1170x2532 px = 390x844 pt @3x")
        self.assertEqual(deck.shot_size((2532, 1170), (390, 844), "n"), "2532x1170 px = 844x390 pt @3x")
        self.assertEqual(deck.shot_size((1206, 2622), (402, 874), "n"), "1206x2622 px = 402x874 pt @3x")

    def test_unknown_points_keeps_the_note(self):
        self.assertEqual(deck.shot_size((1080, 2400), None, "tap units are these pixels"),
                         "1080x2400 px  (tap units are these pixels)")


class IOSState(unittest.TestCase):
    def test_idle_tunnel_on_cable_is_connected(self):
        cp = {"pairingState": "paired", "transportType": "wired", "tunnelState": "disconnected"}
        self.assertEqual(deck.ios_state(cp), "connected")

    def test_unplugged_device_stays_unavailable(self):
        self.assertEqual(deck.ios_state({"pairingState": "paired", "tunnelState": "unavailable"}), "unavailable")
        self.assertEqual(deck.ios_state({"pairingState": "paired", "tunnelState": "disconnected"}), "disconnected")

    def test_connected_tunnel_passes_through(self):
        cp = {"pairingState": "paired", "transportType": "wired", "tunnelState": "connected"}
        self.assertEqual(deck.ios_state(cp), "connected")


class Config(unittest.TestCase):
    def test_overlay_name_hidden_order_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "devices.json"
            cfg.write_text(json.dumps({"devices": [
                {"id": "emulator-5554", "kind": "android", "name": "Test Pixel", "order": -1},
                {"id": "51B9-SIM", "kind": "simulator", "hidden": True},
                {"id": "00008101-AAAA", "kind": "android", "allowInput": False},
                {"id": "R58M123", "allowInput": False},
                {"id": "00008101-BBBB", "name": None},
            ]}))
            devs = fleet()
            with mock.patch.dict(os.environ, {"DEVICEDECK_CONFIG": str(cfg)}), \
                    mock.patch.object(deck, "detect_ios", return_value=devs[:2]), \
                    mock.patch.object(deck, "detect_sims", return_value=[devs[2]]), \
                    mock.patch.object(deck, "detect_android", return_value=devs[3:]):
                found = deck.discover()
        self.assertEqual(found[0].name, "Test Pixel")
        self.assertTrue(devs[2].hidden)
        self.assertTrue(devs[0].allow_input, "kind mismatch must not apply")
        self.assertFalse(devs[4].allow_input)
        self.assertTrue(devs[1].allow_input, "allowInput defaults to true")
        self.assertEqual(devs[1].name, "spare - iPhone", "name null keeps the detected name")


if __name__ == "__main__":
    unittest.main()
