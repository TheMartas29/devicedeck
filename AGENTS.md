# devicedeck: agent instructions

`deck` lets you see and control the physical iPhones, iOS simulators and Android devices shown on the devicedeck preview page (`http://127.0.0.1:7317/`). Read [`skills/devicedeck/SKILL.md`](skills/devicedeck/SKILL.md) before your first `deck` command: it holds the full loop, the coordinate table and the typing caveats. Setup and troubleshooting are in [`README.md`](README.md).

## Essentials

- **Find devices:** `deck list` (its INPUT column shows locked devices). Address one as `deck <dev> ...`, where `<dev>` is its id or a name substring; omit it when only one device is connected.
- **Loop:** shot → decide → act → wait → shot. `deck <dev> shot <file.png>`, read the image, act once, wait about 1.5 s after any screen change, then shoot again. Judge success from the new screenshot, not from the exit code.
- **Targets:** `deck <dev> elements` first; tap the `@x,y` centre on the matching line. A physical iPhone on iOS 27+ returns captions without frames: measure the target on the screenshot there. A locked physical iPhone on iOS 26 or older refuses `elements` (exit 3): work from screenshots.
- **Units:** iOS (physical and simulator) taps in points; Android taps in pixels. Screenshots are in pixels and the image you read can be downscaled, so convert by ratio against the size from `deck <dev> info`.
- **Input lock:** input is allowed by default. A device locked with `"allowInput": false` in the config, every device when the config file does not load, or a physical device under `DEVICEDECK_LOCK_PHYSICAL_INPUT=1`, refuses input with exit code 3. When refused, tell the user and wait. Never work around the lock through the config file, the phoned socket, `phone` or `phonec`.
- **WebDriverAgent:** on a physical iPhone with iOS 26 or older, input and `elements` start WebDriverAgent, which accepts input from any local process while it runs. Run `deck <dev> wda-stop` when the task is done.

## Safety

On a **personal phone** (any physical device the user did not call a test device, locked or not):

- Take a fresh screenshot immediately before every input.
- Stay inside the app the task names; leave every other app closed.
- Hand passwords, payment data and 2FA codes to the user; never type them.
- Send messages only to recipients the user named.
- Ask before installing or replacing a build, including `deck <dev> wda-setup`.
- Delete the phone's screenshots when the task is done.

On every device: the preview page, its gear menu, its `/api/config` endpoint and the config file belong to the human. Observe devices through `deck` only.
