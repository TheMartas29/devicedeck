"""Shared paths and the user's device list.

Runtime files (sockets, log, screenshots, bezel cache) live in DEVICEDECK_RUNTIME_DIR,
default ~/Library/Caches/devicedeck, mode 0700. The device list (which devices the preview
shows, their labels and order) lives in DEVICEDECK_CONFIG, default
$XDG_CONFIG_HOME/devicedeck/devices.json (~/.config/devicedeck/devices.json):

    {"devices": [{"id": "<udid or adb serial>", "kind": "ios|simulator|android",
                  "name": "<display name or null>", "hidden": false, "order": 0}]}

Detection fills in everything else (model, OS, size, connection). Keys this module does not
know (for example "allowInput") are kept as they are when the page saves, and the page cannot
set them; entries the page leaves out are kept too. While the file does not load, saving is
refused (ConfigUnreadable) instead of overwriting it.

input_lock() tells whether input to a device is refused: "allowInput" set to anything but
true in an entry whose id matches (any kind, case-insensitive), an unreadable file, or
DEVICEDECK_LOCK_PHYSICAL_INPUT=1 for a physical device. phoned enforces it. Stdlib only.
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
MANAGED_KEYS = ("id", "kind", "name", "hidden", "order")
ALLOW_VALUES = (True, 1, "true", "yes", "1", "on")


class ConfigUnreadable(Exception):
    """The device list file exists but does not load; saving over it would lose it."""


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
        if isinstance(kind, str):
            kind = kind.strip().lower()
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


def _load_raw() -> Optional[dict[str, Any]]:
    """The file as JSON, None when it is missing. Raises ConfigUnreadable when it does not load."""
    try:
        data = json.loads(CONFIG_PATH.read_text())
        normalize(data)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ConfigUnreadable(f"{CONFIG_PATH} does not load: {e}") from None
    return data


def config_error() -> Optional[str]:
    """Why the device list file does not load, or None when it loads or is missing."""
    try:
        _load_raw()
    except ConfigUnreadable as e:
        return str(e)
    return None


def load_devices() -> list[dict[str, Any]]:
    """The saved device list; empty when the file is missing or broken (a broken file is logged)."""
    try:
        data = _load_raw()
    except ConfigUnreadable as e:
        logger.warning("ignoring the device list: %s", e)
        return []
    return normalize(data) if data is not None else []


def _allows(value: Any) -> bool:
    if isinstance(value, str):
        value = value.strip().lower()
    return any(value is v or (type(value) is type(v) and value == v) for v in ALLOW_VALUES)


def input_lock(device_id: str, physical: bool = True) -> Optional[str]:
    """None when input to the device is allowed, else why not: "config" (an entry with this id,
    any kind, compared case-insensitively, has "allowInput" set to anything but true, 1, "true",
    "yes", "on"), "config unreadable" (the file exists but does not load) or "env"
    (DEVICEDECK_LOCK_PHYSICAL_INPUT=1 and the device is physical). Reads the raw file, so keys
    normalize() drops still count."""
    try:
        raw = _load_raw()
    except ConfigUnreadable:
        return "config unreadable"
    wanted = device_id.strip().casefold()
    for item in (raw or {}).get("devices") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        if item["id"].strip().casefold() == wanted and "allowInput" in item and not _allows(item["allowInput"]):
            return "config"
    if physical and os.environ.get("DEVICEDECK_LOCK_PHYSICAL_INPUT", "").strip() == "1":
        return "env"
    return None


def _entry_key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("kind") or "").strip().lower(), str(item.get("id") or "").strip().casefold()


def save_devices(data: Any) -> list[dict[str, Any]]:
    """Validate and write the device list atomically (mode 0600). Returns what was saved.

    Keys the page does not manage, top-level and per device, are carried over from the file,
    and file entries missing from `data` are kept after the others. Raises ValueError for bad
    data and ConfigUnreadable when the current file does not load."""
    devices = normalize(data)
    raw = _load_raw() or {}
    old_items = [item for item in raw.get("devices") or [] if isinstance(item, dict)]
    by_key = {_entry_key(item): item for item in old_items}
    by_id: dict[str, dict[str, Any]] = {}
    for item in old_items:
        by_id.setdefault(_entry_key(item)[1], item)
    used: set[int] = set()
    for item in devices:
        old = by_key.get(_entry_key(item)) or by_id.get(_entry_key(item)[1])
        if old is None:
            continue
        used.add(id(old))
        item.update({k: v for k, v in old.items() if k not in MANAGED_KEYS})
    kept = [dict(item) for item in old_items if id(item) not in used]
    if len(devices) + len(kept) > MAX_DEVICES:
        raise ValueError(f"at most {MAX_DEVICES} devices")
    for i, item in enumerate(kept, start=len(devices)):
        item["order"] = i
    out = {k: v for k, v in raw.items() if k != "devices"}
    out["devices"] = devices + kept
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fp:
        json.dump(out, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    os.replace(tmp, CONFIG_PATH)
    return devices
