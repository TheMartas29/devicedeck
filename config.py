"""Shared paths and the user's device list.

Runtime files (sockets, log, screenshots, bezel cache) live in DEVICEDECK_RUNTIME_DIR,
default ~/Library/Caches/devicedeck, mode 0700. The device list (which devices the preview
shows, their labels and order) lives in DEVICEDECK_CONFIG, default
$XDG_CONFIG_HOME/devicedeck/devices.json (~/.config/devicedeck/devices.json):

    {"devices": [{"id": "<udid or adb serial>", "kind": "ios|simulator|android",
                  "name": "<display name or null>", "hidden": false, "order": 0}]}

Detection fills in everything else (model, OS, size, connection). Keys this module does not
know (for example "allowInput", which the deck CLI reads) are kept as they are when the page
saves, and the page cannot set them. Stdlib only.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("devicedeck.config")

KINDS = ("ios", "simulator", "android")
MAX_ID = 200
MAX_NAME = 100
MAX_DEVICES = 200


def env(*names: str, default: Optional[str] = None) -> Optional[str]:
    """The first non-empty environment variable of `names` (new name first, old aliases after)."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


RUNTIME_DIR = Path(env("DEVICEDECK_RUNTIME_DIR", default="~/Library/Caches/devicedeck")).expanduser()
CONFIG_PATH = Path(env("DEVICEDECK_CONFIG", default=str(
    Path(env("XDG_CONFIG_HOME", default="~/.config")) / "devicedeck" / "devices.json"))).expanduser()
SOCKET_PATH = Path(env("DEVICEDECK_SOCK", "PHONED_SOCK", default=str(RUNTIME_DIR / "phoned.sock"))).expanduser()
FRAME_CACHE = RUNTIME_DIR / "frames"


def runtime_dir() -> Path:
    """The runtime directory, created private to this user."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)
    return RUNTIME_DIR


def normalize(data: Any) -> list[dict[str, Any]]:
    """Validated device list sorted by order, orders renumbered 0..n. Raises ValueError."""
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        raise ValueError('expected {"devices": [...]}')
    items = data["devices"]
    if len(items) > MAX_DEVICES:
        raise ValueError(f"at most {MAX_DEVICES} devices")
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"device {i}: expected an object")
        dev_id, kind = item.get("id"), item.get("kind")
        if not isinstance(dev_id, str) or not dev_id.strip() or len(dev_id) > MAX_ID:
            raise ValueError(f"device {i}: bad id")
        if kind not in KINDS:
            raise ValueError(f"device {i}: kind must be one of {', '.join(KINDS)}")
        name = item.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise ValueError(f"device {i}: name must be a string or null")
            name = " ".join(name.split())[:MAX_NAME] or None
        hidden = item.get("hidden", False)
        if not isinstance(hidden, bool):
            raise ValueError(f"device {i}: hidden must be true or false")
        order = item.get("order", i)
        if isinstance(order, bool) or not isinstance(order, (int, float)):
            raise ValueError(f"device {i}: order must be a number")
        key = (kind, dev_id.strip())
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": key[1], "kind": kind, "name": name, "hidden": hidden, "order": order})
    out.sort(key=lambda d: d["order"])
    for i, item in enumerate(out):
        item["order"] = i
    return out


def load_devices() -> list[dict[str, Any]]:
    """The saved device list; empty when the file is missing or broken (a broken file is logged)."""
    try:
        return normalize(json.loads(CONFIG_PATH.read_text()))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        logger.warning("ignoring %s: %s", CONFIG_PATH, e)
        return []


def _read_raw() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_devices(data: Any) -> list[dict[str, Any]]:
    """Validate and write the device list atomically (mode 0600). Returns what was saved.

    Unknown keys already in the file, top-level and per device, are carried over."""
    devices = normalize(data)
    raw = _read_raw()
    extras = {}
    for item in raw.get("devices") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            extra = {k: v for k, v in item.items() if k not in ("id", "kind", "name", "hidden", "order")}
            extras[(item.get("kind"), item["id"].strip())] = extra
            extras.setdefault((None, item["id"].strip()), extra)
    for item in devices:
        extra = extras.get((item["kind"], item["id"])) or extras.get((None, item["id"])) or {}
        item.update({k: v for k, v in extra.items() if k not in item})
    out = {k: v for k, v in raw.items() if k != "devices"}
    out["devices"] = devices
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fp:
        json.dump(out, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    os.replace(tmp, CONFIG_PATH)
    return devices
