# devicedeck

devicedeck shows all your mobile devices side by side on one local web page and gives an AI agent (or you) a command-line tool to see and control them.

- **The page** at `http://127.0.0.1:7317/` shows physical iPhones, booted iOS simulators, and Android emulators or phones next to each other. Each device has a label (name, model, OS, kind) and a frame drawn for its model. The page is a preview: you watch on it, and the gear menu lets you show, hide, rename and reorder devices.
- **The CLI** `deck` takes screenshots, lists on-screen elements, and sends taps, swipes, text, keys and app launches to any of those devices. It is built so that an agent such as Claude Code can drive a device in a screenshot → decide → act loop while you watch the page.

Everything runs on your Mac and binds to `127.0.0.1` only. Nothing is installed on an iPhone except WebDriverAgent, and only when you choose that path.

`deck` sends input to every device by default, physical phones included. If you connect a personal phone, lock it first: see [Input lock](#input-lock).

## Requirements

| What | Needed for | Notes |
|---|---|---|
| macOS (tested on Apple silicon) | everything | |
| Xcode 27.x with command-line tools | iPhones and simulators | `xcrun devicectl`, `xcrun simctl`, `xcodebuild`, and the device data used to draw frames. |
| Python 3.11 or newer | the daemon and the page | Tested with 3.13. Dependencies in `requirements.txt`. |
| ffmpeg 5.1 or newer | Android video | `brew install ffmpeg` |
| adb (Android platform-tools) | Android | `brew install --cask android-platform-tools`, or the copy that ships with Android Studio. |
| RocketSim and its CLI (optional) | simulator input and `elements` | Install [RocketSim](https://www.rocketsim.app), then RocketSim › Settings › CLI & Agent › Install Command Line Tool. `deck` also finds the CLI inside `RocketSim.app` when it is not on `PATH`. Without it, simulators are view only: `shot`, `launch` and `apps` still work through `simctl`. |
| A WebDriverAgent checkout | iPhones on iOS 26 or older | `git clone https://github.com/appium/WebDriverAgent`, or Appium's copy (`npm install -g appium && appium driver install xcuitest`), which `deck` finds by itself. See [iPhone on iOS 26 or older](#iphone-on-ios-26-or-older-webdriveragent). |
| An Apple developer team | WebDriverAgent | `deck` builds WebDriverAgent signed with your team. A paid team is the reliable choice: free personal teams may fail to register the device, and their profiles expire after 7 days. |

`deck` controls a physical iPhone in one of two ways, picked per device from its iOS version: on **iOS 27 and newer** through the `phoned` daemon (pymobiledevice3), on **iOS 26 and older** through WebDriverAgent, which you build and install once with `deck <dev> wda-setup`.

## Quick start

```bash
git clone git@github.com:TheMartas29/devicedeck.git
cd devicedeck
python3 -m venv venv
venv/bin/pip install -r requirements.txt
export PATH="$PWD:$PATH"          # puts deck and phone on PATH; add it to your shell profile
                                  # (a symlink to deck also works: it finds the venv next to itself)

phone start                       # starts the phoned daemon and the page in the background
open http://127.0.0.1:7317/       # the page
deck list                         # every device deck can reach
deck shot ~/Desktop/screen.png    # works without <dev> when exactly one device is connected
```

`phone start` runs `venv/bin/python phoned.py --preview` in the background; you can also run that command yourself in a terminal tab. `phone stop` stops the daemon, `phone status` checks it. Set up each kind of device as described below before you expect it on the page.

## The `deck` CLI

```
deck list [--json]
deck [<dev>] shot <file.png> | elements | tap x y | swipe x1 y1 x2 y2 [ms] | type <text> | key <name> | launch <id> | apps [--all] | info
```

`deck --help` is the full reference, including the key names per platform.

| Command | What it does |
|---|---|
| `list` | Lists devices: id, kind (`ios`, `simulator`, `android`), name, model, OS, state, and an INPUT column that shows whether input is allowed. Devices hidden in the preview are still listed, marked `(hidden)`, and `deck` still controls them; `--json` prints JSON. |
| `shot <file.png>` | Saves a screenshot and prints its path and pixel size. |
| `elements` | Prints the accessibility tree as compact text: the centre point to tap, then the `[x,y wxh]` frame, in tap units. A physical iPhone on the `phoned` backend (iOS 27+) prints captions only, without frames: take coordinates from `shot` there. On the WebDriverAgent backend it prints frames. |
| `tap x y` | Taps a point. |
| `swipe x1 y1 x2 y2 [ms]` | Drags from one point to another, optionally over `ms` milliseconds. |
| `type <text>` | Types ASCII text into the focused field (the WebDriverAgent backend also takes Unicode). |
| `key <name>` | Presses a named key or button. The names differ per platform (`home`, `enter`, `backspace`, Android `back` and any `KEYCODE_*`); an unknown name prints the supported ones. |
| `launch <id>` | Starts an app by bundle id (iOS) or package (Android; `package/activity` also works). |
| `apps [--all]` | Lists installed apps. By default user apps (on a physical iPhone, development-signed apps only); `--all` adds system apps (and App Store apps on a physical iPhone). |
| `info` | Shows the model, OS, screen size in pixels and points, the scale, and the iOS backend. |
| `wda-setup` | Physical iPhone: builds WebDriverAgent with your team, installs and starts it, and prints the steps that need a human on the phone. See [iPhone on iOS 26 or older](#iphone-on-ios-26-or-older-webdriveragent). |
| `wda-stop` | Physical iPhone: stops the WebDriverAgent runner and its port forward. |

`<dev>` is the device id (UDID or adb serial), a case-insensitive substring of the device name, or nothing when exactly one device is connected. An ambiguous `<dev>` lists the candidates.

**Exit codes:** `0` ok, `1` the command failed, `2` usage error or no or ambiguous device, `3` refused by the input lock.

**Coordinates:** iOS devices (physical and simulator) take points, Android devices take pixels. Screenshots are always in pixels, so on iOS divide by the scale that `info` prints (2 or 3).

**Backends:**

| Device | Screen and input |
|---|---|
| Physical iPhone, iOS 26 and older | WebDriverAgent over a USB port forward (`wda` backend). After `wda-setup`, `deck` starts the runner and the forward by itself. |
| Physical iPhone, iOS 27 and newer | `xcrun devicectl` plus the `phoned` daemon that `phone start` launches (pymobiledevice3). `phoned` is a hub: it starts one `ios_worker.py` process per connected iPhone, because pymobiledevice3 allows one device tunnel per process. Without the daemon, `deck` falls back to calling pymobiledevice3 directly. |
| iOS simulator | `xcrun simctl` for `shot`, `launch` and `apps`; the RocketSim CLI for input and `elements`. |
| Android emulator or phone | `adb`. |

`ADB`, `ROCKETSIM` and `PYMOBILEDEVICE3` override the paths of those tools.

### Input lock

Input commands (`tap`, `swipe`, `type`, `key`, `launch`) are allowed on every device by default. To protect a personal phone, lock it:

- one device, permanently: `"allowInput": false` on that device in the config file (see [Device management](#device-management)). **Recommended for any personal phone.**
- every physical device, for one shell: `export DEVICEDECK_LOCK_PHYSICAL_INPUT=1`

A locked device refuses input with exit code `3`. `shot`, `elements`, `apps` and `info` always work. Simulators and emulators are never locked by the environment variable.

## Device setup

### Physical iPhone

1. Connect the iPhone by USB and unlock it. Tap **Trust** on the "Trust This Computer?" prompt and enter the passcode.
2. Open Xcode › Window › Devices and Simulators once, so Xcode prepares the device.
3. Turn on **Developer Mode**: Settings › Privacy & Security › Developer Mode. The phone restarts and asks you to confirm.
4. On iOS 27 and newer, turn on Settings › Developer › **UI Automation**.
5. Mount the developer disk image: `phone mount`. Repeat this after every restart of the phone.
6. Keep the phone unlocked while you control it. A locked phone ignores touches. While the daemon runs, it keeps the phone awake.
7. `phone start`. The phone appears on the page and in `deck list`. Several iPhones can be connected at once; `DEVICEDECK_UDID` sets the default one for the daemon's socket commands (`PHONE_UDID` is an older alias).

The phone allows one screen stream at a time, and touch input needs that stream. The daemon holds it and serves the page from it. Close other viewers of the phone (RocketSim's device preview, Xcode or other mirroring tools) while devicedeck runs, and run only one agent per phone.

#### iPhone on iOS 26 or older (WebDriverAgent)

On iOS 26 and older the phone rejects remote input from pymobiledevice3 (error 9021, "requires iOS 27.0 or later"). For these phones `deck` uses WebDriverAgent (WDA), an XCUITest runner from the Appium project that you build with your own Apple team and install on the phone once.

1. Do steps 1 to 3 above (Trust, Xcode, Developer Mode) and add your Apple ID in Xcode › Settings › Accounts.
2. Get the WDA source: `git clone https://github.com/appium/WebDriverAgent` and point `DEVICEDECK_WDA_PROJECT` at it, or install Appium's copy (`npm install -g appium && appium driver install xcuitest`), which `deck` finds in `~/.appium` by itself.
3. Tell `deck` your team and a bundle id that is unique to you, in the environment or in the device's config entry:
   ```bash
   export DEVICEDECK_WDA_TEAM=<TEAM_ID>                                 # config: "wdaTeam"
   export DEVICEDECK_WDA_BUNDLE_ID=com.<you>.WebDriverAgentRunner       # config: "wdaBundleId"
   ```
   The Team ID is in Xcode › Settings › Accounts › your team, or on the Membership page of your developer account.
4. With the phone unlocked, run `deck <dev> wda-setup`. It builds WDA (the build output goes to the runtime directory, `~/Library/Caches/devicedeck/wda/`), installs it, starts it, and forwards its port to `127.0.0.1`.
5. The first start fails until the phone trusts your certificate. `wda-setup` prints the steps; on the phone:
   - Settings › General › VPN & Device Management › your developer account › **Trust**;
   - Settings › Developer › **Enable UI Automation**;
   - keep the phone unlocked while WDA starts.

   Then run `deck <dev> wda-setup` again.

After that, every `deck` command on that phone starts the runner and the forward by itself. `deck <dev> wda-stop` stops them. `deck <dev> info` shows which backend a phone uses; `DEVICEDECK_IOS_BACKEND=wda|phoned` (config: `"iosBackend"`) forces one.

More settings, each also a config key on the device:

| Variable | Config key | Use |
|---|---|---|
| `DEVICEDECK_WDA_PROJECT` | `wdaProject` | A WebDriverAgent checkout (the folder with `WebDriverAgent.xcodeproj`). |
| `DEVICEDECK_WDA_XCTESTRUN` | `wdaXctestrun` | A prebuilt `.xctestrun` to run instead of building. |
| `DEVICEDECK_WDA_URL` | `wdaUrl` | A WDA you run yourself; `deck` then starts nothing. |
| `DEVICEDECK_WDA_PORT` | `wdaPort` | The local port of the forward (default: derived from the UDID). |

With a free personal team the profile expires after 7 days: run `wda-setup` again when WDA stops launching. A locked device (see [Input lock](#input-lock)) also refuses `wda-setup`.

#### Wi-Fi pairing

The daemon reaches the phone through usbmuxd, which also lists phones paired for network use. To use the phone without a cable:

1. Pair it over USB first (steps 1 to 5 above).
2. In Xcode › Window › Devices and Simulators, select the phone and turn on **Connect via network**.
3. Keep the Mac and the phone on the same network. Give your terminal app the macOS **Local Network** permission (see [Troubleshooting](#troubleshooting)).

Wi-Fi control has not been tested as thoroughly as USB. Use USB when you need a reliable stream.

### iOS simulator

1. Boot a simulator: open Simulator.app, or `xcrun simctl boot "<device name>"`.
2. It appears on the page within a few seconds and in `deck list`. Screenshots work with Xcode alone.
3. For input (`tap`, `swipe`, `type`, `key`) and `elements`, install the RocketSim CLI (see [Requirements](#requirements)). Without it, the simulator is view only: `shot`, `launch` and `apps` still work.

AXe is an optional alternative for simulator input outside `deck`; `deck` uses RocketSim only.

### Android emulator

1. Create and start an emulator in Android Studio › Device Manager, or run `emulator -avd <name>`.
2. Check that `adb devices` lists it as `device`.
3. It appears on the page and in `deck list`.

### Android phone

1. Turn on Developer options: Settings › About phone › tap **Build number** seven times.
2. Turn on Settings › Developer options › **USB debugging**.
3. Connect the phone by USB, unlock it, and accept the "Allow USB debugging?" prompt for this Mac.
4. Check that `adb devices` lists it as `device` (not `unauthorized`).

The Android page view decodes `adb screenrecord` with ffmpeg. Text input goes through `adb shell input text`, which takes ASCII only. The page shows Android in portrait.

## Device management

The gear menu on the page shows, hides, renames and reorders devices. You can also drag a device by its label to a new place. Hiding only removes the device from the page; `deck` still lists and controls it. Changes save at once to:

- `~/.config/devicedeck/devices.json`, or
- the path in `DEVICEDECK_CONFIG`.

The page reads and writes it through `GET`/`POST /api/config`, and lists the devices it sees at `GET /api/devices` (both on the page port).

Devices are matched by id: the UDID for iPhones and simulators, the adb serial for Android. `deck list` shows the ids. `devices.example.json` shows the shape:

```json
{
  "devices": [
    { "id": "00008110-0000000000000001", "kind": "ios", "name": "Personal iPhone", "hidden": false, "order": 0, "allowInput": false },
    { "id": "emulator-5554", "kind": "android", "name": "Pixel emulator", "hidden": false, "order": 1 }
  ]
}
```

`allowInput: false` locks that device against input (see [Input lock](#input-lock)); a device without the key accepts input. Set it on every personal phone. Keep the file out of version control: it holds your device ids and names.

## Ports

| Variable | Default | Serves |
|---|---|---|
| `DEVICEDECK_PORT` | `7317` | The page. |
| `DEVICEDECK_PANELS_PORT` | `DEVICEDECK_PORT + 1` (`7318`) | Simulator and Android video streams used by the page. `0` turns them off. Device input goes through `deck`, not through this port. |

Both servers bind to `127.0.0.1` only and reject requests whose `Origin` or `Host` belongs to another site. Do not expose them on the network: anyone who reaches them can see your devices.

```bash
DEVICEDECK_PORT=9000 phone start      # page on 9000, panels on 9001
```

## Runtime files

The daemon socket, its log (`phoned.log`), default screenshots and the cache of device frames live in `~/Library/Caches/devicedeck` (mode 0700, readable only by your user).

| Variable | Default |
|---|---|
| `DEVICEDECK_RUNTIME_DIR` | `~/Library/Caches/devicedeck` |
| `DEVICEDECK_SOCK` | the daemon socket inside the runtime directory (`PHONED_SOCK` is an older alias) |

Screenshots of a personal phone can contain private data: delete them when you are done.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The iPhone is missing from `deck list` | Check `xcrun devicectl list devices`: the phone must be "available (paired)". Unlock it, accept Trust, check the cable. |
| Error about the developer disk image or DDI, or no screenshot from the iPhone | `phone mount`. Needed once after every restart of the phone. |
| `code 9021 ... requires iOS 27.0` | The phone runs iOS 26 or older and is on the `phoned` backend. Run `deck <dev> wda-setup` (see [WebDriverAgent](#iphone-on-ios-26-or-older-webdriveragent)), or check `DEVICEDECK_IOS_BACKEND`. |
| `wda-setup` reports missing settings | Set `DEVICEDECK_WDA_TEAM`, `DEVICEDECK_WDA_BUNDLE_ID` and, without Appium, `DEVICEDECK_WDA_PROJECT`. |
| A tap returns success but nothing happens | The phone is locked, UI Automation is off (iOS 27), or the tap landed during an animation. Check a screenshot, wait 1.5 s, try again. |
| A device refuses input with exit code 3 | It is locked: `"allowInput": false` in the config, or `DEVICEDECK_LOCK_PHYSICAL_INPUT=1` for a physical device. See [Input lock](#input-lock). |
| `No route to host`, or a Wi-Fi iPhone or wireless adb device cannot be reached | macOS **Local Network** privacy blocks your terminal app. System Settings › Privacy & Security › Local Network › turn on your terminal (Terminal, iTerm, Warp, VS Code...). Restart the terminal afterwards. Loopback (`127.0.0.1`) and USB need no permission. |
| The page shows "Stream offline" or the stream freezes | Something else took the phone's screen stream: close RocketSim's device preview and other mirroring tools. `phone stop`, then `phone start`. |
| `phone start` says the daemon did not start | Read `~/Library/Caches/devicedeck/phoned.log`. Usually the phone is locked or the disk image is not mounted. |
| Commands return "device not connected" | The daemon is reconnecting (cable pulled, phone restarted). Wait, then `phone status`. |
| An Android device shows `unauthorized` in `adb devices` | Unlock the phone and accept the USB debugging prompt. |
| No Android panel on the page | `adb` or `ffmpeg` is not on `PATH`, or `adb devices` is empty. |
| Typed text comes out with swapped letters on an iPhone | `type` sends hardware key codes, and the phone maps them through its hardware-keyboard layout. Set Settings › General › Keyboard › Hardware Keyboard to U.S., or check the result on a screenshot. |
| A simulator refuses `tap`, `type` or `elements` | The RocketSim CLI is missing. Install RocketSim, then RocketSim › Settings › CLI & Agent › Install Command Line Tool, or point `ROCKETSIM` at the binary. |
| WDA build fails with a signing error | Your Apple ID is missing in Xcode › Settings › Accounts, the Team ID is wrong, or the bundle id belongs to someone else: pick another `DEVICEDECK_WDA_BUNDLE_ID`. The build log is in `~/Library/Caches/devicedeck/wda/build.log`. |
| WDA does not launch: "untrusted developer" | Trust the certificate: Settings › General › VPN & Device Management. |

## Connecting Claude Code

The repository is also a Claude Code plugin (`.claude-plugin/plugin.json`) and its own one-plugin marketplace (`.claude-plugin/marketplace.json`). The plugin carries the `devicedeck` skill, which teaches Claude the screenshot → decide → act loop, the coordinate units and the safety rules. Claude Code loads the skill when you ask it to look at, test or control a device.

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
2. **Put `deck` on the `PATH`** of the shell that starts Claude Code (see [Quick start](#quick-start)).
3. **Allow the commands** you do not want to approve one by one, in `.claude/settings.json` of your project or in `~/.claude/settings.json`:
   ```json
   { "permissions": { "allow": ["Bash(deck list)", "Bash(deck:*)"] } }
   ```
   `Bash(deck:*)` also allows input. Lock a personal phone with `"allowInput": false` (see [Input lock](#input-lock)), or leave input commands to be approved one by one.
4. **Start devicedeck** (`phone start`) and open the page, so you see what Claude does and can take over.
5. Ask, for example: "Use deck to open Settings on the simulator and take a screenshot."

Inside this repository Claude Code also reads `AGENTS.md` (through `CLAUDE.md`). Other agents that read `AGENTS.md` can use the same instructions. `claude plugin validate .` checks both manifests; it warns that a plugin does not load `CLAUDE.md`, which is expected here.

## License

No license is granted. Copyright © Roman Martínek, all rights reserved. You may view the code, but you may not copy, modify or redistribute it without permission.

devicedeck uses [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) (GPL-3.0-or-later), installed separately through `requirements.txt`.
