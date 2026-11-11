# devicedeck

devicedeck shows every mobile device on your Mac on one local web page, and gives Claude Code (or any agent that can run shell commands, or you) the `deck` command to see and control them. Physical iPhones and iPads, booted iOS simulators, Android emulators and Android phones appear side by side at `http://127.0.0.1:7317/`, each in a frame drawn for its model. The page is view only by design: you watch on it, and the gear menu only shows, hides, renames and reorders devices. Control goes only through `deck` in a terminal: screenshots, the on-screen elements, taps, swipes, text, keys and app launches. Everything runs on your Mac (macOS only) and listens on `127.0.0.1` only.

Out of the box, `deck` can control every device on the page, physical phones included: once a device appears in `deck list`, it accepts taps, swipes, text, keys and app launches. An input lock exists as an optional opt-in, described under [Input lock](#input-lock).

## Quick start

### Requirements

| What | Needed for | Notes |
|---|---|---|
| macOS (tested on Apple silicon) | everything | |
| Xcode 27.x with command-line tools | iPhones and simulators | `xcrun devicectl`, `xcrun simctl`, `xcodebuild`, and the device data used to draw frames. |
| Python 3.11 or newer | the daemon and the page | Tested with 3.13. The stock macOS `python3` is 3.9 and too old: `requirements.txt` pins `pillow==13.0.0`, which needs 3.11. |
| ffmpeg 5.1 or newer | Android video on the page | `brew install ffmpeg`. It must be on `PATH`. |
| adb (Android platform-tools) | Android | `brew install --cask android-platform-tools`, or the copy in Android Studio. devicedeck finds `adb` through `ADB`, then `PATH`, then `$ANDROID_HOME`, `$ANDROID_SDK_ROOT` or `~/Library/Android/sdk` (`platform-tools/adb`). |
| RocketSim and its CLI (optional) | simulator input and `elements` | Install [RocketSim](https://www.rocketsim.app), then RocketSim › Settings › CLI & Agent › Install Command Line Tool. `deck` also finds the CLI inside `RocketSim.app` (in `/Applications` or `~/Applications`) when it is not on `PATH`. Without it, simulators are view only: `shot`, `launch` and `apps` still work through `simctl`. |
| A WebDriverAgent checkout | iPhones on iOS 26 or older | `git clone https://github.com/appium/WebDriverAgent`, or Appium's copy (`npm install -g appium && appium driver install xcuitest`), which `deck` finds by itself. See [iPhone or iPad on iOS 26 and older](#iphone-or-ipad-on-ios-26-and-older). |
| An Apple developer team | WebDriverAgent | `deck` builds WebDriverAgent signed with your team. A paid team is the reliable choice: free personal teams may fail to register the device, and their profiles expire after 7 days. |

### Install and start

```bash
git clone git@github.com:TheMartas29/devicedeck.git
cd devicedeck
python3.13 -m venv venv           # any python3.11 or newer; stock macOS python3 is 3.9 and too old
venv/bin/python --version         # must print 3.11 or newer
venv/bin/pip install -r requirements.txt
export PATH="$PWD:$PATH"          # puts deck and phone on PATH; add it to your shell profile
                                  # (a symlink to deck also works: it finds the venv next to itself)

phone start                       # starts the phoned daemon and the page in the background
open http://127.0.0.1:7317/       # the page
deck list                         # every device deck can reach
deck shot ~/Desktop/screen.png    # works without <dev> when exactly one device is connected
```

If the venv was made with an older Python, delete the `venv` folder and create it again with a named version (`python3.13`, from Homebrew or python.org).

`phone start` runs `venv/bin/python phoned.py --preview` in the background and writes its output to `~/Library/Caches/devicedeck/phoned.log`. You can also run that command yourself in a terminal tab. `phone stop` stops the daemon, `phone status` checks it.

Then pair each device as described in [Device types](#device-types). A device shows up on the page and in `deck list` only after its one-time setup.

## How it works

```
 Browser  http://127.0.0.1:7317/   (view only)
   │ page, /api/devices, /api/config, /frame/<model>.png, /ios/<udid>/ (iPhone video only)
   │ simulator and Android video from http://127.0.0.1:7318/
   ▼
 phoned.py  (the hub; `phone start` runs it)
   ├─ preview.py       the page server, port 7317 (DEVICEDECK_PORT)
   ├─ side_panels.py   simulator stills (simctl) and Android video (adb screenrecord → ffmpeg), port 7318
   ├─ Unix socket      ~/Library/Caches/devicedeck/phoned.sock  ◄── deck, phone, phonec
   └─ ios_worker.py    one process per connected iPhone: CoreDevice tunnel, screen stream or
                       screenshot polling, keep-awake, input and accessibility

 deck  (stdlib Python, runs with the venv next to it)
   ├─ iPhone, iOS 27+   → phoned socket → worker  (falls back to pymobiledevice3 without phoned)
   │                      + xcrun devicectl for apps, launch and the screen size
   ├─ iPhone, iOS ≤ 26  → WebDriverAgent on the phone, through a 127.0.0.1 port forward
   │                      (input, elements); screenshots through phoned or pymobiledevice3
   ├─ iOS simulator     → xcrun simctl (shot, launch, apps) + RocketSim CLI (input, elements)
   └─ Android           → adb (everything)
```

Why it is built this way:

- **One interface for every device.** `deck` takes the same commands for a physical iPhone, a simulator and an Android device. An agent learns one loop (screenshot, decide, act) instead of four tools.
- **Any agent.** `deck` is a plain command with text output and exit codes. Claude Code gets a skill for it, and any other agent that can run a shell command can use it too.
- **The hub keeps the connections open.** A connection to an iPhone (a CoreDevice tunnel) takes seconds to open. `phoned` holds one per phone, so a `deck tap` is fast. pymobiledevice3 allows one tunnel per process, so `phoned` starts one `ios_worker.py` per iPhone.
- **Viewing is separate from control.** The page can only watch, so leaving it open in a browser tab cannot change a device. Every input goes through `deck`, where the input lock is checked.
- **Official tools only.** devicedeck uses Xcode (`devicectl`, `simctl`, `xcodebuild`), Android platform-tools (`adb`), pymobiledevice3, WebDriverAgent and the RocketSim CLI. Nothing is jailbroken or patched on a device, and nothing is installed on an iPhone except WebDriverAgent, only on iOS 26 and older, and only when you run `wda-setup`.

## Device types

| Device | What the page shows | How `deck` controls it | Tap units | One-time setup |
|---|---|---|---|---|
| iPhone or iPad, iOS 27 and newer | live video | `phoned` (pymobiledevice3) | points | Trust, Developer Mode, UI Automation, `phone mount` |
| iPhone or iPad, iOS 26 and older | screenshots, up to 2 per second | WebDriverAgent | points | the above, plus `deck <dev> wda-setup` |
| iOS simulator | screenshots, about 2 per second | `simctl` + RocketSim CLI | points | install the RocketSim CLI |
| Android emulator | live video | `adb` | pixels | none |
| Android phone | live video | `adb` | pixels | USB debugging or wireless debugging |

`deck` picks the iPhone backend from the iOS version: `phoned` from iOS 27, WebDriverAgent below. `DEVICEDECK_IOS_BACKEND=wda|phoned` (config key `"iosBackend"`) forces one. `deck <dev> info` shows which one a phone uses.

**iPad:** the code treats iPadOS devices exactly like iPhones (same kind `ios`, same backends, same steps). Everything below that says "iPhone" applies to an iPad too.

### iPhone or iPad on iOS 27 and newer

**What you see.** The phone's own screen stream, as live video, inside a frame drawn from Xcode's data for the model. The label shows the name, model, iOS version and the connection (USB or Wi-Fi).

**How control works.** `phoned` starts an `ios_worker.py` for the phone. The worker holds the CoreDevice tunnel and the screen stream, and sends touches and key presses as HID events through that stream. `deck` talks to the worker over the `phoned` socket. Screenshots come from the screen capture service, `elements` from the accessibility service, and `apps` and `launch` from `xcrun devicectl`. Without `phoned`, `deck` calls pymobiledevice3 directly for each command, which is slower.

**Pair it with devicedeck.**

1. Connect the phone by USB and unlock it. Tap **Trust** on the "Trust This Computer?" prompt and enter the passcode.
2. Open Xcode › Window › Devices and Simulators once, so Xcode prepares the device.
3. Turn on **Developer Mode**: Settings › Privacy & Security › Developer Mode. The phone restarts; after the restart, confirm with **Turn On** and the passcode. (The switch appears only after the phone has been connected to Xcode.)
4. Turn on Settings › Developer › **UI Automation**.
5. Mount the developer disk image: `phone mount`. Repeat this after every restart of the phone.
6. `phone start`. The phone appears on the page and in `deck list` with state `connected`. Several iPhones can be connected at once; `DEVICEDECK_UDID` sets the default one for the daemon's socket commands (`phone`, `phonec`; `PHONE_UDID` is an older alias). `deck` always names the phone itself.
7. Check: `deck <dev> info` prints `backend  phoned`, and `deck <dev> shot /tmp/t.png` saves a screenshot. `deck` can now control the phone.

**Recommendations.**

- Use a USB cable. It gives the most reliable stream.
- Keep `phoned` running (`phone start`) while you work: commands are much faster through it.
- Keep the phone unlocked. A locked phone ignores touches. While `phoned` runs, it keeps the phone from auto-locking.
- Set Settings › General › Keyboard › Hardware Keyboard to U.S. before you let an agent type (see Limits).
- Close other viewers of the phone (RocketSim's device preview, Xcode or other mirroring tools) and run only one agent per phone.

**Limits.**

- The phone allows one screen stream at a time, and touch input needs that stream. `phoned` holds it and serves the page from it.
- `elements` prints accessibility captions only, without frames. Take coordinates from a screenshot: pixels divided by the scale from `info`.
- `type` takes ASCII only. It sends hardware key codes, which the phone maps through its hardware-keyboard layout, so on a non-US layout some letters swap (on QWERTZ, y and z).
- `launch` restarts the app if it is already running.
- Keys: `home lock volume-up volume-down mute siri enter backspace escape tab up down left right`.

### iPhone or iPad on iOS 26 and older

**What you see.** These phones have no screen stream (they answer error 9021, "requires iOS 27.0 or later"). The worker polls screenshots instead, at most 2 per second (one screenshot can take over a second, so often fewer), shrinks them to half size and sends a new picture only when the screen changed. It takes them only while a page is open. The worker also holds a keep-awake assertion so the phone does not auto-lock while connected.

**How control works.** The phone also rejects remote input from pymobiledevice3 (error 9021). `deck` uses WebDriverAgent (WDA) instead: an XCUITest runner from the Appium project that you build with your own Apple team and install once. `deck` starts the runner with `xcodebuild test-without-building` and forwards its port 8100 to a local port on `127.0.0.1` with `pymobiledevice3 usbmux forward`. Input and `elements` go to WDA. `shot` does not need WDA: it goes through `phoned`, else pymobiledevice3, and uses WDA only when both fail and the device is not locked. `apps` uses `xcrun devicectl`.

**Pair it with devicedeck.**

1. Do steps 1 to 3 and step 5 of [iOS 27 and newer](#iphone-or-ipad-on-ios-27-and-newer) (Trust, Xcode, Developer Mode, `phone mount`), and add your Apple ID in Xcode › Settings › Accounts.
2. `phone start`, so the phone appears on the page and `shot` is fast.
3. Get the WDA source, one of:
   - `git clone https://github.com/appium/WebDriverAgent` and point `DEVICEDECK_WDA_PROJECT` at it;
   - clone it into the repository as `WebDriverAgent/` (git ignores that folder), which `deck` finds by itself;
   - install Appium's copy (`npm install -g appium && appium driver install xcuitest`), which `deck` finds in `~/.appium` by itself.
4. Tell `deck` your team and a bundle id that is unique to you, in the environment or in the device's config entry:
   ```bash
   export DEVICEDECK_WDA_TEAM=<TEAM_ID>                                 # config: "wdaTeam"
   export DEVICEDECK_WDA_BUNDLE_ID=com.<you>.WebDriverAgentRunner       # config: "wdaBundleId"
   ```
   The Team ID is in Xcode › Settings › Accounts › your team, or on the Membership page of your developer account.
5. With the phone unlocked, run `deck <dev> wda-setup`. It builds WDA (into `~/Library/Caches/devicedeck/wda/`), installs it, starts it and forwards its port.
6. The first start fails until the phone trusts your certificate. `wda-setup` prints the steps. On the phone:
   - Settings › General › VPN & Device Management › your developer account › **Trust**;
   - Settings › Developer › **Enable UI Automation**;
   - keep the phone unlocked while WDA starts.
7. Run `deck <dev> wda-setup` again. It ends with "WebDriverAgent is ready at http://127.0.0.1:<port>".
8. Check: `deck <dev> info` prints `backend  wda` and `wda  ready at ...`.

After that, every input or `elements` command on that phone starts the runner and the forward by itself (it waits up to `DEVICEDECK_WDA_TIMEOUT` seconds, default 120). `deck <dev> wda-stop` stops them.

**Recommendations.**

- Run `deck <dev> wda-stop` when you are done. While WDA runs, it listens on `127.0.0.1` (a port from 8200 to 8799) without authentication: any process on your Mac can tap and type on the phone through it, even if the phone is locked in the config. `deck` itself never starts WDA on a locked device.
- Use a paid team. With a free personal team the profile expires after 7 days: run `wda-setup` again when WDA stops launching.
- Ask before running `wda-setup` on a personal phone: it installs an app. A locked device refuses it (exit 3).
- More settings, each also a config key on the device:

  | Variable | Config key | Use |
  |---|---|---|
  | `DEVICEDECK_WDA_PROJECT` | `wdaProject` | A WebDriverAgent checkout (the folder with `WebDriverAgent.xcodeproj`). |
  | `DEVICEDECK_WDA_XCTESTRUN` | `wdaXctestrun` | A prebuilt `.xctestrun` to run instead of the `wda-setup` build. |
  | `DEVICEDECK_WDA_URL` | `wdaUrl` | A WDA you run yourself; `deck` then starts nothing. |
  | `DEVICEDECK_WDA_PORT` | `wdaPort` | The local port of the forward (default: derived from the UDID, between 8200 and 8799). |

**Limits.**

- The page shows half-size stills, not video.
- `elements` needs WDA, which accepts input, so a locked device refuses `elements` (exit 3). Work from screenshots there.
- `elements` prints frames in points, like the simulator.
- `type` also takes Unicode on this backend.
- `launch` restarts the app if it is already running.
- Keys: `home lock volume-up volume-down enter backspace tab escape`.

### iPhone over Wi-Fi

The daemon reaches phones through usbmuxd, which also lists phones paired for network use, and through `xcrun devicectl`. To use a phone without a cable:

1. Pair it over USB first (all the steps above for its iOS version).
2. In Xcode › Window › Devices and Simulators, select the phone and turn on **Connect via network**.
3. Keep the Mac and the phone on the same network. Give your terminal app the macOS **Local Network** permission (see [Troubleshooting](#troubleshooting)).

The page label then says Wi-Fi. Wi-Fi control has not been tested as thoroughly as USB, and WebDriverAgent over Wi-Fi has not been tested at all. Use USB when you need a reliable stream.

### iOS simulator

**What you see.** The page's video server takes `xcrun simctl io <udid> screenshot` about twice a second while the page is open, and sends a picture when the screen changed. The frame is drawn from Xcode's data for the simulated model.

**How control works.** `shot`, `launch` and `apps` use `xcrun simctl`. Input (`tap`, `swipe`, `type`, `key`) and `elements` use the RocketSim CLI.

**Pair it with devicedeck.**

1. Install the RocketSim CLI (see [Requirements](#requirements)). Skip this for a view-only simulator.
2. Boot a simulator: open Simulator.app, or `xcrun simctl boot "<device name>"`.
3. It appears on the page within a few seconds, and in `deck list` with state `booted`.
4. Check: `deck <dev> info` prints a `control` line with the RocketSim path.

**Recommendations.**

- Use `elements` to find targets: it prints frames and centre points in points.
- When a physical phone is also connected, address the simulator by id, so a name substring cannot match the phone.

**Limits.**

- `type` takes ASCII only.
- Keys: `home lock side siri apple-pay enter`.
- `launch` restarts the app if it is already running.
- `DEVICEDECK_LOCK_PHYSICAL_INPUT` never locks a simulator. `"allowInput": false` in its config entry does.

AXe is an optional alternative for simulator input outside `deck`; `deck` uses RocketSim only.

### Android emulator and Android phone

**What you see.** Live video: `adb exec-out screenrecord` (H.264, half the screen size, 6 Mbit/s) decoded by ffmpeg into JPEG frames. Capture runs only while a page watches and stops 5 seconds after the last viewer leaves. screenrecord stops itself after 180 seconds and devicedeck starts it again. Android tiles have no device frame.

**How control works.** Everything goes through `adb -s <serial>`: `screencap` for `shot`, `uiautomator dump` for `elements`, `input tap|swipe|text|keyevent` for input, `monkey` or `am start` for `launch`, `pm list packages` for `apps`.

**Pair an emulator.**

1. Create and start an emulator in Android Studio › Device Manager, or run `emulator -avd <name>`.
2. Check that `adb devices` lists it as `device`.
3. It appears on the page and in `deck list`.

**Pair a phone over USB.**

1. Turn on Developer options: Settings › About phone › tap **Build number** seven times (on some phones it is under Software information).
2. Turn on Settings › Developer options › **USB debugging**.
3. Connect the phone by USB, unlock it, and accept the "Allow USB debugging?" prompt for this Mac (tick "Always allow from this computer").
4. Check that `adb devices` lists it as `device` (not `unauthorized`).

**Pair a phone over Wi-Fi** (Android 11 and newer, wireless debugging):

1. Do steps 1 and 2 above. Keep the Mac and the phone on the same network.
2. On the phone, Settings › Developer options › **Wireless debugging** › turn it on, then tap **Pair device with pairing code**.
3. On the Mac: `adb pair <ip>:<pairing port>` with the address shown in that dialog, and enter the six-digit code.
4. Then `adb connect <ip>:<port>` with the address shown on the Wireless debugging screen (a different port from the pairing one).
5. Check that `adb devices` lists it as `device`. Its serial is now the address, or an `adb-...` name.
6. Give your terminal app the macOS **Local Network** permission (see [Troubleshooting](#troubleshooting)).

On Android 10 and older: connect by USB, run `adb tcpip 5555`, unplug, then `adb connect <phone ip>:5555`.

**Recommendations.**

- Taps are in pixels, not dp. `deck <dev> info` prints the pixel size and, for reference only, a dp size and scale.
- Use `elements` to find targets: it prints frames and centre points in pixels.
- Prefer USB for a phone: the video stream is 6 Mbit/s and Wi-Fi adds delay.
- `ANDROID_SERIAL=<serial>` before `phone start` limits the page to that one Android device (`deck` is not affected).

**Limits.**

- `type` takes ASCII only (`adb shell input text`).
- Keys: `home back enter backspace tab escape up down left right menu power volume-up volume-down app-switch search`, and any `KEYCODE_*` name or number.
- `launch <package>` starts the launcher activity and does not restart a running app; `launch <package>/<activity>` starts that activity.
- Android blacks out secure windows (for example banking apps) in screenshots and screen recordings.
- A phone (not an emulator) is physical, so `DEVICEDECK_LOCK_PHYSICAL_INPUT=1` locks it.

## The page

`http://127.0.0.1:7317/` shows every connected device that is not hidden, side by side, each as tall as the window allows. It checks for devices every 3 seconds.

The **gear** (top right) opens the device list. It lists every device the page has seen, connected or not:

- the checkbox shows or hides a device;
- the text field sets a label (empty: the detected name);
- ▲ and ▼ move a device;
- you can also drag a device on the page by its label to a new place.

Every change saves at once. **Hidden means hidden on the page only**: `deck` still lists the device, marked `(hidden)`, and still controls it, and `phoned` keeps its iPhone worker running.

### Config file

The page saves to `~/.config/devicedeck/devices.json` (`$XDG_CONFIG_HOME/devicedeck/devices.json` when `XDG_CONFIG_HOME` is set), or to the path in `DEVICEDECK_CONFIG`. The page reads and writes it through `GET`/`POST /api/config`, and lists the devices it sees at `GET /api/devices` (both on the page port). The file is written atomically with mode 0600.

Devices are matched by id: the UDID for iPhones and simulators, the adb serial for Android. `deck list` shows the ids. `devices.example.json` shows the shape:

```json
{
  "devices": [
    { "id": "00008110-0000000000000001", "kind": "ios", "name": "My iPhone", "hidden": false, "order": 0, "allowInput": true },
    { "id": "emulator-5554", "kind": "android", "name": "Pixel emulator", "hidden": false, "order": 1 }
  ]
}
```

| Key | Values | Set by |
|---|---|---|
| `id` | UDID or adb serial | the page |
| `kind` | `ios`, `simulator` or `android` | the page |
| `name` | label, or `null` for the detected name (at most 100 characters) | the page |
| `hidden` | `true` or `false` | the page |
| `order` | a number; the page renumbers from 0 | the page |
| `allowInput` | `false` locks the device; missing or `true` allows input | you, by hand |
| `iosBackend`, `wdaTeam`, `wdaBundleId`, `wdaProject`, `wdaXctestrun`, `wdaUrl`, `wdaPort` | see [iOS 26 and older](#iphone-or-ipad-on-ios-26-and-older) | you, by hand |

The page cannot set `allowInput` or the WDA keys. When it saves, it keeps every key it does not manage and every entry it did not send.

Use plain JSON `true` and `false`. A file that does not load (bad JSON, an unknown `kind`, `hidden` that is not a boolean, more than 200 devices) locks input to every device until you fix it, and the page refuses to save over it (HTTP 409, "Not saved" in the gear panel). Keep the file out of version control: it holds your device ids and names.

### Ports

| Variable | Default | Serves |
|---|---|---|
| `DEVICEDECK_PORT` | `7317` | The page, and the iPhone video under `/ios/<udid>/`. |
| `DEVICEDECK_PANELS_PORT` | `DEVICEDECK_PORT + 1` (`7318`) | Simulator and Android video used by the page. `0` turns it off, and the page then shows no simulators or Android devices. Device input goes through `deck`, not through this port. |

```bash
DEVICEDECK_PORT=9000 phone start      # page on 9000, video on 9001
```

`phoned.py` also takes `--port`, `--panels-port` and `--bind`. `--bind` accepts only a loopback address (`127.0.0.1`, `::1`, `localhost`), because the page has no login.

## Using it with Claude Code

The repository is also a Claude Code plugin (`.claude-plugin/plugin.json`) and its own one-plugin marketplace (`.claude-plugin/marketplace.json`). The plugin carries the `devicedeck` skill (`skills/devicedeck/SKILL.md`), which teaches Claude the screenshot → decide → act loop, the coordinate units and the safety rules. Claude Code loads the skill when you ask it to look at, test or control a device.

1. **Install the skill**, in one of three ways:
   - **From GitHub, as a plugin.** In a Claude Code session:
     ```
     /plugin marketplace add TheMartas29/devicedeck
     /plugin install devicedeck@devicedeck
     ```
     or from your shell: `claude plugin marketplace add TheMartas29/devicedeck`, then `claude plugin install devicedeck@devicedeck`. The repository is private, so Claude Code clones it with the git credentials already on your machine: an SSH key loaded in `ssh-agent`, or, over HTTPS, a stored credential (`gh auth login`, then `gh auth setup-git`). Without a GitHub SSH key, set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1`.
   - **From your local clone, as a plugin.** `claude plugin marketplace add /path/to/devicedeck`, then `claude plugin install devicedeck@devicedeck`. Claude Code then reads the skill straight from your clone, so your edits apply at the next session start or after `/reload-plugins`. For one session only: `claude --plugin-dir /path/to/devicedeck`.
   - **As a plain skill**, without the plugin system:
     ```bash
     mkdir -p ~/.claude/skills
     ln -s "$PWD/skills/devicedeck" ~/.claude/skills/devicedeck    # or cp -R to copy it
     ```

   The plugin installs only the skill. You still clone the repository, create the venv and put `deck` on your `PATH`, as in [Quick start](#quick-start). The plugin version is pinned in `plugin.json` (`0.1.0`): raise it with each release, or users who installed from GitHub keep their cached copy.
2. **Put `deck` on the `PATH`** of the shell that starts Claude Code.
3. **Allow the commands** you do not want to approve one by one, in `.claude/settings.json` of your project or in `~/.claude/settings.json`:
   ```json
   { "permissions": { "allow": ["Bash(deck list)", "Bash(deck:*)"] } }
   ```
   `Bash(deck:*)` also allows input, so Claude can drive every device without asking. Leave it out if you would rather approve each input command.
4. **Start devicedeck** (`phone start`) and open the page, so you see what Claude does and can take over.
5. Ask, for example: "Use deck to open Settings on the simulator and take a screenshot."

Inside this repository Claude Code also reads `AGENTS.md` (through `CLAUDE.md`). `claude plugin validate .` checks both manifests; it warns that a plugin does not load `CLAUDE.md`, which is expected here.

### Other agents

Any agent that can run shell commands can use `deck`. Point it at `AGENTS.md` (many agents read it by themselves) and `skills/devicedeck/SKILL.md`, which hold the loop, the units and the safety rules in plain Markdown. `deck list --json` gives machine-readable device data, and the [exit codes](#deck-commands) tell success, usage errors and a refused input apart.

## Safety

### Input lock

Input commands (`tap`, `swipe`, `type`, `key`, `launch`) are allowed on every device by default. That is the normal state: nothing needs to be set for `deck` to control a device.

If you want `deck` to refuse input to a device, the lock is an optional opt-in:

- one device, permanently: `"allowInput": false` on that device in the [config file](#config-file);
- every physical device, for one shell: `export DEVICEDECK_LOCK_PHYSICAL_INPUT=1`.

The rules of the opt-in lock:

- Input is allowed only when `allowInput` is missing or is `true`, `1`, `"true"`, `"yes"`, `"on"` or `"1"`. Anything else locks: `false`, `0`, `"false"`, `"no"`, `null`, a typo.
- The id matches case-insensitively, and the lock applies whatever the entry's `kind` says.
- A config file that exists but does not load (bad JSON, an unknown `kind`, a wrong type) locks every device until you fix it. `deck` prints why.
- `DEVICEDECK_LOCK_PHYSICAL_INPUT=1` never locks simulators and emulators.

A locked device refuses input with exit code `3`, and also refuses `wda-setup`, and `elements` on an iPhone with iOS 26 or older (it needs WebDriverAgent, which accepts input). `shot`, `apps` and `info` always work, and so does `elements` everywhere else. `deck list`, `deck <dev> info` and `deck list --json` show why a device is locked: `config`, `config unreadable` or `env`.

The lock is enforced in three places: in `deck`, in the `phoned` socket (so `phone` and `phonec` cannot bypass it for iPhones on iOS 27 and newer), and in the `phone` helper itself. It does not cover tools you run directly: `adb`, `xcrun simctl`, the RocketSim CLI, pymobiledevice3, or a running WebDriverAgent on its local port.

### Personal phones

- Run `deck <dev> wda-stop` when you are done with a phone on iOS 26 or older.
- Screenshots of a personal phone can contain private data. `deck shot` writes where you tell it; `phone shot` writes to the runtime directory by default. Delete them when you are done.
- The skill tells Claude to take a fresh screenshot before every input on a personal phone, stay inside the named app, never type passwords, payment data or 2FA codes, and ask before installing a build.

### What the page can and cannot do

- It shows video and pictures, and it reads and writes the device list (labels, order, hidden). That is all.
- It cannot send input: the device frames ignore pointer events, the embedded iPhone viewer blocks every event, and the server passes to an iPhone's worker only the read-only video paths (it may ask for a key frame or restart the video stream). Everything else, including the viewer's touch, key and button paths, gets 404.
- It cannot change `allowInput` or any key it does not manage, and it does not save over a config file that does not load.

### Local only

- Both servers bind to `127.0.0.1` only (`--bind` accepts only loopback addresses). Every request must name the server in `Host`, and a browser `Origin`, when sent, must be the page itself; this stops other websites and DNS rebinding. `POST /api/config` also needs `Content-Type: application/json`.
- Do not expose the ports on the network: anyone who reaches them can see your devices.
- The runtime directory is mode 0700 and the sockets are mode 0600, so only your macOS user can talk to `phoned` and the workers.
- The WebDriverAgent port forward also binds to `127.0.0.1`, but WDA itself has no authentication.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `pip install` fails, for example on `pillow` | The venv was made with Python older than 3.11 (stock macOS `python3` is 3.9). Delete `venv` and create it with `python3.13 -m venv venv`. |
| The iPhone is missing from `deck list` | Check `xcrun devicectl list devices`: the phone must be "available (paired)". Unlock it, accept Trust, check the cable. |
| Error about the developer disk image or DDI, or no screenshot from the iPhone | `phone mount`. Needed once after every restart of the phone. |
| `code 9021 ... requires iOS 27.0` | The phone runs iOS 26 or older and is on the `phoned` backend. Run `deck <dev> wda-setup` (see [iOS 26 and older](#iphone-or-ipad-on-ios-26-and-older)), or check `DEVICEDECK_IOS_BACKEND` and `"iosBackend"`. |
| `wda-setup` reports missing settings | Set `DEVICEDECK_WDA_TEAM`, `DEVICEDECK_WDA_BUNDLE_ID` and, without Appium's copy or a `WebDriverAgent/` folder in the repository, `DEVICEDECK_WDA_PROJECT`. |
| WDA build fails with a signing error | Your Apple ID is missing in Xcode › Settings › Accounts, the Team ID is wrong, or the bundle id belongs to someone else: pick another `DEVICEDECK_WDA_BUNDLE_ID`. A free team cannot always register the device: use a paid team. The build log is in `~/Library/Caches/devicedeck/wda/build.log`. |
| WDA does not launch: "untrusted developer" | Trust the certificate: Settings › General › VPN & Device Management. |
| WDA did not become ready, or the runner exited | Read `~/Library/Caches/devicedeck/wda/<udid>.run.log` and `<udid>.forward.log`. Keep the phone unlocked; raise `DEVICEDECK_WDA_TIMEOUT` on a slow first start. |
| A tap returns success but nothing happens | The phone is locked, UI Automation is off, or the tap landed during an animation. Check a screenshot, wait 1.5 s, try again. |
| A device refuses input with exit code 3 | It is locked: `"allowInput": false` in the config, `DEVICEDECK_LOCK_PHYSICAL_INPUT=1` for a physical device, or the config file does not load. `deck list` shows which. See [Input lock](#input-lock). |
| Every device shows `locked (config unreadable)` | The config file does not load. `deck` prints why; fix or move the file by hand. |
| The gear panel says "Not saved" | The config file does not load (HTTP 409), so the page will not overwrite it. Fix or move the file by hand. |
| `elements` on an iPhone with iOS 26 or older exits 3 | The device is locked, and `elements` needs WebDriverAgent, which accepts input. Use `deck <dev> shot` instead. |
| `No route to host`, or a Wi-Fi iPhone or wireless adb device cannot be reached | macOS **Local Network** privacy blocks your terminal app. System Settings › Privacy & Security › Local Network › turn on your terminal (Terminal, iTerm, Warp, VS Code...). Restart the terminal afterwards. Loopback (`127.0.0.1`) and USB need no permission. |
| The page shows "Stream offline" or the stream freezes | Something else took the phone's screen stream: close RocketSim's device preview and other mirroring tools. `phone stop`, then `phone start`. |
| `phone start` says the daemon did not start | Read `~/Library/Caches/devicedeck/phoned.log`. Usually the phone is locked or the disk image is not mounted. |
| One iPhone keeps showing an error on the page | Read its worker log, `~/Library/Caches/devicedeck/w-<udid>.log`. |
| Commands return "device not connected" | The daemon is reconnecting (cable pulled, phone restarted). Wait, then `phone status`. |
| An Android device shows `unauthorized` in `adb devices` | Unlock the phone and accept the USB debugging prompt. |
| No Android panel on the page | `adb` was not found (`ADB`, `PATH`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `~/Library/Android/sdk`), `ffmpeg` is not on `PATH`, `adb devices` is empty, `DEVICEDECK_PANELS_PORT` is `0`, or `ANDROID_SERIAL` names another device. `phoned.log` says which. |
| Typed text comes out with swapped letters on an iPhone | `type` sends hardware key codes, and the phone maps them through its hardware-keyboard layout. Set Settings › General › Keyboard › Hardware Keyboard to U.S., or check the result on a screenshot. |
| A simulator refuses `tap`, `type` or `elements` | The RocketSim CLI is missing. Install RocketSim, then RocketSim › Settings › CLI & Agent › Install Command Line Tool, or point `ROCKETSIM` at the binary. |

## Reference

### deck commands

```
deck list [--json]
deck [<dev>] shot <file.png> | elements | tap x y | swipe x1 y1 x2 y2 [ms] | type <text> | key <name> | launch <id> | apps [--all] | info | wda-setup | wda-stop
```

`<dev>` is the device id (UDID or adb serial), a case-insensitive substring of the device name, or nothing when exactly one device is connected. An exact name wins over a substring; an ambiguous `<dev>` lists the candidates. `deck --help` is the full reference.

| Command | What it does |
|---|---|
| `list` | Lists devices: id, kind (`ios`, `simulator`, `android`), name, model, OS, state and INPUT (`yes` or `locked (<reason>)`). Hidden devices are listed, marked `(hidden)`. `--json` prints JSON, including `inputAllowed` and `lockReason`. Usable states: `connected` (iPhone), `booted` (simulator), `device` (Android). |
| `shot <file.png>` | Saves a screenshot and prints its path and pixel size; on iOS also the point size and scale (`1170x2532 px = 390x844 pt @3x`). |
| `elements` | The accessibility or UI tree as compact text: `role "label" ="value" #id @x,y [x,y wxh]`, where `@x,y` is the centre to tap and `[x,y wxh]` the frame, in tap units. An iPhone on iOS 27 and newer prints captions only, without frames. |
| `tap x y` | Taps a point. |
| `swipe x1 y1 x2 y2 [ms]` | Drags from one point to another over `ms` milliseconds (default 300). |
| `type <text>` | Types text into the focused field. ASCII only, except on WebDriverAgent. |
| `key <name>` | Presses a named key or button (names per device type above). An unknown name prints the supported ones. |
| `launch <id>` | Starts an app by bundle id (iOS) or package (Android; `package/activity` also works). On iOS it restarts a running app. |
| `apps [--all]` | Lists installed apps. By default user apps (on a physical iPhone, development-signed apps only); `--all` adds system apps (and App Store apps on a physical iPhone). |
| `info` | Model, OS, screen size in pixels and points, scale, tap units, the input lock; on a physical iPhone also the backend and the state of `phoned` or WDA; on a simulator the RocketSim path. |
| `wda-setup` | Physical iPhone: builds WebDriverAgent with your team, installs and starts it, and prints the steps that need a human on the phone. |
| `wda-stop` | Physical iPhone: stops the WebDriverAgent runner and its port forward. |

**Exit codes:** `0` ok, `1` the command failed, `2` usage error or no or ambiguous device, `3` refused by the input lock.

**Coordinates:** iOS devices (physical and simulator) take points, Android devices take pixels. Screenshots are always in pixels, so on iOS divide by the scale that `info` prints (2 or 3). The origin is the top left corner.

### phone and phonec

`phone` is a small helper for humans. It starts and stops the daemon, and it can drive one iPhone by hand. Agents use `deck`.

| Command | What it does |
|---|---|
| `phone start`, `phone preview` | Starts the daemon and the page in the background. |
| `phone stop`, `phone status` | Stops the daemon, or prints its state as JSON. |
| `phone mount` | Mounts the developer disk image (`pymobiledevice3 mounter auto-mount`). Once after every restart of the phone. |
| `phone info` | iOS version, Developer Mode, disk image services and connection of the phone. |
| `phone shot [file.png]` | Screenshot. Without a file it saves `shot-<time>.png` to `PHONE_OUT_DIR` (default: the runtime directory). |
| `phone tap X Y [--px]`, `phone drag X1 Y1 X2 Y2 [--px]` | Tap or drag, in points; with `--px`, in pixels of the last screenshot. |
| `phone type "text"`, `phone key NAME [count]`, `phone button NAME` | Keyboard text, keys and hardware buttons. |
| `phone clip "text"` | Puts text on the phone's clipboard. |
| `phone elements` | Accessibility captions, without coordinates. |
| `phone apps [all]`, `phone launch BUNDLE [URL]`, `phone kill BUNDLE` | Lists, starts or quits apps. |

`phone` drives the phone through the `phoned` socket, or pymobiledevice3 directly when the daemon is down, so its touch and keyboard commands need iOS 27 or newer. With several iPhones connected, set `DEVICEDECK_UDID`. It refuses `tap`, `drag`, `type`, `key`, `button`, `clip`, `launch` and `kill` on a locked device with exit code `3`, also when the daemon is down.

`phonec` is the raw socket client behind `phone`: `phonec ping`, `phonec shot PATH`, `phonec tap X Y` (0 to 65535) and so on; `phonec --udid <udid|name> ...` picks the phone. `phonec --help` lists the commands. The socket refuses input to a locked iPhone.

### Environment variables

| Variable | Used by | Default | Meaning |
|---|---|---|---|
| `DEVICEDECK_CONFIG` | all | `$XDG_CONFIG_HOME/devicedeck/devices.json`, else `~/.config/devicedeck/devices.json` | The config file. |
| `XDG_CONFIG_HOME` | all | `~/.config` | Base folder of the default config path. |
| `DEVICEDECK_LOCK_PHYSICAL_INPUT` | `deck`, `phoned`, `phone` | unset | `1` refuses input to every physical device. |
| `DEVICEDECK_RUNTIME_DIR` | all | `~/Library/Caches/devicedeck` | Runtime files (see below). |
| `DEVICEDECK_SOCK` (`PHONED_SOCK`) | all | `<runtime dir>/phoned.sock` | The `phoned` socket. `PHONED_SOCK` is used when `DEVICEDECK_SOCK` is empty; `~` is expanded. `deck`, `phone`, `phonec` and the daemon all use the same rule. |
| `DEVICEDECK_PORT` | `phoned`, `phone` | `7317` | The page port. |
| `DEVICEDECK_PANELS_PORT` | `phoned` | page port + 1 | Simulator and Android video; `0` turns it off. |
| `DEVICEDECK_UDID` (`PHONE_UDID`, `PHONE_DEVICE` for `phone` only) | `phoned`, `phone`, `phonec` | the only connected iPhone | Default iPhone for socket commands without `@<udid>`. `deck` does not use it. |
| `DEVICEDECK_IOS_BACKEND` | `deck` | from the iOS version | `wda` or `phoned`. Config: `iosBackend`. |
| `DEVICEDECK_WDA_TEAM` | `deck` | none | Apple team id for `wda-setup`. Config: `wdaTeam`. |
| `DEVICEDECK_WDA_BUNDLE_ID` | `deck` | none | Runner bundle id, unique to you. Config: `wdaBundleId`. |
| `DEVICEDECK_WDA_PROJECT` | `deck` | Appium's copy in `~/.appium`, else `WebDriverAgent/` in the repository | WebDriverAgent checkout. Config: `wdaProject`. |
| `DEVICEDECK_WDA_XCTESTRUN` | `deck` | the newest from the `wda-setup` build | Prebuilt `.xctestrun`. Config: `wdaXctestrun`. |
| `DEVICEDECK_WDA_URL` | `deck` | none | A WDA you run yourself; `deck` starts nothing. Config: `wdaUrl`. |
| `DEVICEDECK_WDA_PORT` | `deck` | 8200 to 8799, from the UDID | Local port of the forward. Config: `wdaPort`. |
| `DEVICEDECK_WDA_TIMEOUT` | `deck` | `120` | Seconds to wait for WDA to start. |
| `ADB` | `deck`, `phoned` | `PATH`, then the Android SDK | Path to `adb`. |
| `ANDROID_HOME`, `ANDROID_SDK_ROOT` | `deck`, `phoned` | `~/Library/Android/sdk` | Android SDK, searched for `platform-tools/adb` when `adb` is not on `PATH`. |
| `ANDROID_SERIAL` | `phoned` (page only) | unset | Show only this Android device on the page. `deck` ignores it: name the device as `<dev>`. |
| `ROCKETSIM` | `deck` | `PATH`, then inside `RocketSim.app` | Path to the RocketSim CLI. |
| `PYMOBILEDEVICE3` | `deck` | `venv/bin/pymobiledevice3`, then `PATH` | Path to pymobiledevice3 (fallback and WDA port forward). |
| `DEVICEDECK_DEVICE_TYPES` | `phoned` | `/Library/Developer/CoreSimulator/Profiles/DeviceTypes`, else inside Xcode | Xcode device types, used to draw frames. |
| `DEVICEDECK_DEVICE_CHROME` | `phoned` | `/Library/Developer/DeviceKit/Chrome`, else inside Xcode | Xcode bezel parts, used to draw frames. |
| `DEVICEDECK_TIMEOUT` (`PHONEC_TIMEOUT`) | `phonec` | `120` | Seconds to wait for a reply. |
| `PHONE_OUT_DIR` | `phone` | the runtime directory | Folder for `phone shot` without a file name. |
| `PHONE_PT_W`, `PHONE_PT_H`, `PHONE_PX_W`, `PHONE_PX_H` | `phone` | from `phoned` and the last `phone shot` | Screen size overrides for `phone tap` and `phone drag`. |

### Files and paths

| Path | What |
|---|---|
| `~/.config/devicedeck/devices.json` | The config file (mode 0600). |
| `~/Library/Caches/devicedeck/` | The runtime directory (mode 0700). |
| `…/phoned.sock`, `…/phoned.log` | The daemon socket and log. `phone start` replaces the log each time. |
| `…/w-<udid>.sock`, `…/w-<udid>.http`, `…/w-<udid>.log` | One iPhone worker: command socket, video socket, log (rotated at 5 MB). |
| `…/frames/` | Cache of drawn device frames. |
| `…/wda/` | WebDriverAgent build (`DerivedData/`, `build.log`) and per-phone state and logs (`<udid>.json`, `<udid>.run.log`, `<udid>.forward.log`). |
| `…/shot-<time>.png` | `phone shot` without a file name. |
| `devices.example.json` | Example config file. |
| `skills/devicedeck/SKILL.md`, `AGENTS.md` | Agent instructions. |
| `.claude-plugin/` | Claude Code plugin and marketplace manifests. |

## License

No license is granted. Copyright © Roman Martínek, all rights reserved. You may view the code, but you may not copy, modify or redistribute it without permission.

devicedeck uses [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) (GPL-3.0-or-later), installed separately through `requirements.txt`.
