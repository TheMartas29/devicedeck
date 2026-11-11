---
name: devicedeck
description: Drive physical iPhones, iOS simulators and Android devices with the devicedeck `deck` CLI. Use when the user wants to see a device screen, test an app on a device, or tap, swipe and type on one.
---

# devicedeck

`deck` gives you the screen and the input of every device shown on the devicedeck preview page (`http://127.0.0.1:7317/` by default). The human watches that page while you work.

```
deck list [--json]
deck [<dev>] shot <file.png> | elements | tap x y | swipe x1 y1 x2 y2 [ms] | type <text> | key <name> | launch <id> | apps [--all] | info
```

`<dev>` is a device id, a case-insensitive substring of its name, or nothing when exactly one device is connected. `deck --help` is the full reference.

Exit codes: `0` ok, `1` command failed, `2` usage error or no or ambiguous device (the candidates are printed), `3` input refused because the device is locked.

## 1. Pick the device

1. Run `deck list`. Choose the device the user named. When several match and the user named none, ask.
2. Run `deck <dev> info` and note the platform, the screen size in points and pixels, and the scale.
3. Decide whether it is a **personal phone**: a physical device that the user did not call a test device, or any device whose INPUT column in `deck list` shows it locked. When unsure, treat it as personal.
4. Send the user the preview URL so they can watch and take over.

Done when you hold one unambiguous `<dev>`, its platform, its screen size, and its personal-phone status.

## 2. Run the loop

Every action is one turn of the loop: **shot → decide → act → wait → shot**.

1. `deck <dev> shot <file.png>`, then read the PNG.
2. `deck <dev> elements`. Each line starts with the centre point to tap, then the `[x,y wxh]` frame, both in tap units: tap that centre. A physical iPhone on iOS 27 and newer (the `phoned` backend; `info` names the backend) returns captions only, without frames; there, use the captions to confirm what is on screen and measure the target on the screenshot.
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

## Typing

- `type` sends key presses. On a physical iPhone they follow the phone's hardware-keyboard layout, so on a non-US layout some letters swap (on QWERTZ, y and z). Check the result on a screenshot.
- Text is ASCII only on Android and on a physical iPhone with the `phoned` backend; the WebDriverAgent backend (iOS 26 and older) also takes Unicode.
- `key` sends named keys and buttons such as `home`, `enter` and `backspace`; the names differ per platform, and an unknown name prints the supported ones.

## Input lock

Input is allowed on every device by default. The user locks a device with `"allowInput": false` in the config file, or every physical device with `DEVICEDECK_LOCK_PHYSICAL_INPUT=1`. A locked device refuses `tap`, `swipe`, `type`, `key` and `launch` with exit code 3. When that happens, tell the user and wait: unlocking is the user's decision.

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
