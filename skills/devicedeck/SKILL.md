---
name: devicedeck
description: See and control any device on the devicedeck preview (physical iPhones, iOS simulators, Android emulators and phones) with the `deck` CLI. Use this first, before simulator-only or Android-only tools, whenever the user wants to see a device screen, test an app on a device, or tap, swipe and type on a phone, simulator or emulator.
---

# devicedeck

`deck` gives you the screen and the input of every device shown on the devicedeck preview page (`http://127.0.0.1:7317/` by default). The human watches that page while you work.

```
deck list [--json]
deck [<dev>] shot <file.png> | elements | tap x y | swipe x1 y1 x2 y2 [ms] | type <text> | key <name> | launch <id> | apps [--all] | info
```

`<dev>` is a device id, a case-insensitive substring of its name, or nothing when exactly one device is connected. When a physical phone is connected, address other devices by id so a name substring cannot match the phone. `deck --help` is the full reference.

`apps` lists user apps only; system apps such as Settings need `apps --all`. Settings is `com.apple.Preferences` on iOS and `com.android.settings` on Android.

STATE in `deck list`: `connected` (physical iPhone), `booted` (simulator) and `device` (Android) are usable; `unavailable` means paired but not reachable now.

Exit codes: `0` ok, `1` command failed, `2` usage error or no or ambiguous device (the candidates are printed), `3` input refused because the device is locked.

## 1. Pick the device

1. Run `deck list`. Choose the device the user named. When several match and the user named none, ask.
2. Run `deck <dev> info` and note the platform, the screen size in points and pixels, and the scale.
3. Decide whether it is a **personal phone**: a physical device that the user did not call a test device, or any device whose INPUT column in `deck list` shows it locked. When unsure, treat it as personal. Simulators and emulators are never personal phones. A device name containing "test" does not by itself make it a test device.
4. Send the user the preview URL so they can watch and take over. If you are a subagent, put the URL in your report instead.

Done when you hold one unambiguous `<dev>`, its platform, its screen size, and its personal-phone status.

## 2. Run the loop

Every action is one turn of the loop: **shot → decide → act → wait → shot**.

1. `deck <dev> shot <file.png>`, then read the PNG.
2. `deck <dev> elements`. Each line reads `Role "label" ="value" #id @cx,cy [x,y wxh]`, indented two spaces per tree level (at most 12 spaces). The label is a JSON string; `="value"` appears only when the value differs from the label, `#id` only when there is one. `@cx,cy` is the centre to tap and `[x,y wxh]` the frame, both in tap units and absolute on the screen; numbers are whole or have one decimal. Example: `  Button "General" #com.apple.settings.general @195,406 [16,380 358x52]`. The output is long, so filter it, for example `deck <dev> elements | grep '"General"'`. If it returns almost nothing right after a launch, wait a second and run it again. On a simulator or emulator, `elements` alone is enough to find targets between actions; take a screenshot to confirm the result of each screen change. A physical iPhone on iOS 27 and newer (the `phoned` backend; on a physical iPhone, `info` names it) returns captions only, without frames; there, use the captions to confirm what is on screen and measure the target on the screenshot. A locked physical iPhone on iOS 26 or older refuses `elements` with exit code 3, because it needs WebDriverAgent, which accepts input; work from screenshots there.
3. Act: one `tap`, `swipe`, `type`, `key` or `launch`.
4. Wait for the UI to settle: about 1.5 s after a screen change (app launch, navigation, page swipe, Home), less for a single tap in place. A tap sent during an animation is lost.
5. Take a new screenshot and judge the result from it. The exit code only says the command was delivered.

Done when the screenshot shows the state the user asked for. When two attempts at the same step leave the screen unchanged, stop and report what you see.

## Coordinates

| Platform | `tap` / `swipe` unit | Screenshot unit |
|---|---|---|
| Physical iPhone | points (UIKit), origin top left | pixels = points × scale (2 or 3) |
| iOS simulator | points (UIKit), origin top left | pixels = points × scale (2 or 3) |
| Android | pixels, origin top left, portrait | pixels |

- The image you read can be downscaled. Convert by ratio: `x = x_img / width_img * screen_width`, the same for y, where `screen_width` is in the tap unit from `deck <dev> info`.
- Points and frames from `elements` are already in the tap unit.
- On Android, `info` also prints a `dp` size; it is informational only, taps stay in pixels.

## Typing

- `type` sends key presses. On a physical iPhone they follow the phone's hardware-keyboard layout, so on a non-US layout some letters swap (on QWERTZ, y and z). Check the result on a screenshot.
- Text is ASCII only on Android, on iOS simulators and on a physical iPhone with the `phoned` backend; only the WebDriverAgent backend (physical iPhone, iOS 26 and older) also takes Unicode. Non-ASCII text exits 1 before anything is typed.
- `key` sends named keys and buttons. `home` and `enter` work everywhere; `back` is Android only. `deck --help` lists every name per platform, and an unknown name prints the supported ones.

## Input lock

Input is allowed on every device by default. The user locks a device with `"allowInput": false` in the config file, or every physical device with `DEVICEDECK_LOCK_PHYSICAL_INPUT=1`; a config file that does not load locks every device (`deck list` shows `locked (config unreadable)`). A locked device refuses `tap`, `swipe`, `type`, `key`, `launch` and `wda-setup` with exit code 3, and a locked physical iPhone on iOS 26 or older also refuses `elements`. `shot` always works. When a command is refused, tell the user and wait: unlocking is the user's decision. Never edit the config file and never call the phoned socket, `phone` or `phonec` to get around the lock.

## WebDriverAgent (physical iPhone, iOS 26 and older)

`tap`, `swipe`, `type`, `key`, `launch` and `elements` start WebDriverAgent on the phone. While it runs, any local process can send input to the phone through it without authentication. When the task is done, run `deck <dev> wda-stop`.

## Personal phone rules

These apply whenever step 1 marked the device as a personal phone, whether or not it is locked.

- Take a fresh screenshot immediately before every input command.
- Stay inside the app the task names. Leave every other app closed, including notifications that offer to open one.
- Typing passwords, payment data and 2FA codes is the user's job: stop and hand over.
- Send messages, mail and invitations only to recipients the user named in this conversation.
- Ask before installing or replacing any build on the phone, including `deck <dev> wda-setup`, which installs WebDriverAgent.
- Delete every screenshot of the phone once the task is done.

## The preview page

The page at `http://127.0.0.1:7317/` is the human's view. Read devices only through `deck`; leave the page, its gear menu, its `/api/config` endpoint and the config file to the human.

## When something fails

See the Troubleshooting section of the devicedeck README: device missing from `deck list`, the developer disk image, the macOS Local Network permission, a frozen stream.
