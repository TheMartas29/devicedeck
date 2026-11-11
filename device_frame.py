"""Device bezel for the preview, drawn from the per-model data that ships with Xcode.

CoreSimulator/Profiles/DeviceTypes holds the screen mask (exact notch and corners) and the name
of the bezel; DeviceKit/Chrome holds the bezel parts and buttons. The Dynamic Island is not in
Xcode's data (Simulator draws it itself), so its size is in ISLAND_* below.

The result is a PNG with a transparent screen; the notch or island is its opaque part. Lay it
over the screen image. Models with a Home button have no mask and give None.
"""
import functools
import json
import os
import plistlib
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

import config


def _first_dir(env_name, *candidates):
    override = os.environ.get(env_name)
    if override:
        return Path(override).expanduser()
    for path in candidates:
        if path and Path(path).is_dir():
            return Path(path)
    return Path(candidates[0])


def _xcode_dir():
    try:
        return subprocess.run(["xcode-select", "-p"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


_XCODE = _xcode_dir()
# Xcode 26+ keeps these in /Library/Developer; older versions inside the Xcode bundle.
DEVICE_TYPES = _first_dir(
    "DEVICEDECK_DEVICE_TYPES", "/Library/Developer/CoreSimulator/Profiles/DeviceTypes",
    _XCODE and f"{_XCODE}/Platforms/iPhoneOS.platform/Library/Developer/CoreSimulator/Profiles/DeviceTypes")
CHROME_DIR = _first_dir(
    "DEVICEDECK_DEVICE_CHROME", "/Library/Developer/DeviceKit/Chrome",
    _XCODE and f"{_XCODE}/Platforms/iPhoneOS.platform/Library/Developer/DeviceKit/Chrome")
SCALE = 3  # px per point, like Super Retina screens
PRODUCT_RE = re.compile(r"^[A-Za-z]+[0-9]+,[0-9]+$")

# Island width and height in points. Not in Xcode's data: known sizes, not measurements.
ISLAND_SIZE = (126, 37.33)
# Distance from the top by screen width in points: iPhone 14 Pro and 15 use 11.33, newer 14.
ISLAND_TOP = {393: 11.33, 430: 11.33}
ISLAND_TOP_DEFAULT = 14


@functools.lru_cache(maxsize=64)
def find_device_type(product_type):
    if not DEVICE_TYPES.is_dir():
        return None
    for bundle in sorted(DEVICE_TYPES.glob("*.simdevicetype")):
        resources = bundle / "Contents/Resources"
        with open(resources / "profile.plist", "rb") as fp:
            profile = plistlib.load(fp)
        if product_type == profile.get("modelIdentifier") or product_type in profile.get(
            "representedModelIdentifiers", []
        ):
            with open(resources / "capabilities.plist", "rb") as fp:
                capabilities = plistlib.load(fp)
            return resources, profile, capabilities.get("capabilities", capabilities)
    return None


def rasterize(pdf, width, height, tmp):
    """sips renders the PDF as vectors at the target size instead of scaling a bitmap."""
    out = Path(tmp) / f"{abs(hash((str(pdf), width, height)))}.png"
    subprocess.run(
        ["sips", "-s", "format", "png", "-z", str(height), str(width), str(pdf), "--out", str(out)],
        check=True, capture_output=True,
    )
    return Image.open(out).convert("RGBA")


def pdf_size(pdf):
    props = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(pdf)], check=True, capture_output=True, text=True
    ).stdout.split()
    return round(float(props[props.index("pixelWidth:") + 1])), round(float(props[props.index("pixelHeight:") + 1]))


def render(product_type):
    """(PNG image, metadata), or None when there is no bezel for the model."""
    found = find_device_type(product_type)
    if found is None:
        return None
    resources, profile, capabilities = found
    if not profile.get("framebufferMask"):
        return None  # Home-button models use another bezel; the page draws a plain border
    screen = capabilities["ScreenDimensionsCapability"]
    px_scale = screen["main-screen-scale"]
    points_w = screen["main-screen-width"] / px_scale
    points_h = screen["main-screen-height"] / px_scale
    chrome_name = profile["chromeIdentifier"].rsplit(".", 1)[-1]
    chrome_res = CHROME_DIR / f"{chrome_name}.devicechrome/Contents/Resources"
    chrome = json.loads((chrome_res / "chrome.json").read_text())
    images = chrome["images"]
    bezel = images["sizing"]["leftWidth"] - 1
    gutter = images["devicePadding"]["left"]

    sw, sh = round(points_w * SCALE), round(points_h * SCALE)
    b, g = bezel * SCALE, gutter * SCALE
    fw, fh = sw + 2 * b, sh + 2 * b
    canvas = Image.new("RGBA", (fw + 2 * g, fh), (0, 0, 0, 0))

    with tempfile.TemporaryDirectory() as tmp:
        def part(name, width=None, height=None):
            pdf = chrome_res / f"{name}.pdf"
            w, h = pdf_size(pdf)
            return rasterize(pdf, width or w * SCALE, height or h * SCALE, tmp)

        # The buttons sit under the body and show only at the sides.
        for spec in chrome["inputs"]:
            if not spec.get("image"):
                continue
            pdf = chrome_res / f"{spec['image']}.pdf"
            if not pdf.exists():
                continue
            image = part(spec["image"])
            offset = spec["offsets"]["normal"]
            y = round(offset["y"] * SCALE)
            if spec["anchor"] == "left":
                x = g + round(offset["x"] * SCALE) - image.width
            elif spec["anchor"] == "right":
                x = g + fw + round(offset["x"] * SCALE)
            else:
                continue
            canvas.alpha_composite(image, (max(x, 0), y))

        tl = part(images["topLeft"])
        cw, ch = tl.size
        body = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        ImageDraw.Draw(body).rounded_rectangle(
            (b // 2, b // 2, fw - b // 2 - 1, fh - b // 2 - 1), radius=cw, fill=(0, 0, 0, 255)
        )
        body.alpha_composite(part(images["top"], fw - 2 * cw, ch), (cw, 0))
        body.alpha_composite(part(images["bottom"], fw - 2 * cw, ch), (cw, fh - ch))
        body.alpha_composite(part(images["left"], cw, fh - 2 * ch), (0, ch))
        body.alpha_composite(part(images["right"], cw, fh - 2 * ch), (fw - cw, ch))
        body.alpha_composite(tl, (0, 0))
        body.alpha_composite(part(images["topRight"]), (fw - cw, 0))
        body.alpha_composite(part(images["bottomLeft"]), (0, fh - ch))
        body.alpha_composite(part(images["bottomRight"]), (fw - cw, fh - ch))

        # Transparent screen. What the mask does not cover stays black: notch and corners.
        hole = Image.new("L", (sw, sh), 255)
        if profile.get("framebufferMask"):
            mask = rasterize(resources / f"{profile['framebufferMask']}.pdf", sw, sh, tmp)
            hole = mask.getchannel("A")
        body.paste((0, 0, 0, 0), (b, b), hole)

    island = False
    if capabilities.get("DeviceSupportsDynamicIsland"):
        iw, ih = (v * SCALE for v in ISLAND_SIZE)
        x0 = b + (sw - iw) / 2
        y0 = b + ISLAND_TOP.get(round(points_w), ISLAND_TOP_DEFAULT) * SCALE
        ImageDraw.Draw(body).rounded_rectangle((x0, y0, x0 + iw, y0 + ih), radius=ih / 2, fill=(0, 0, 0, 255))
        island = True

    canvas.alpha_composite(body, (g, 0))
    meta = {
        "product": product_type,
        "name": capabilities["ArtworkTraits"].get("ArtworkDeviceProductDescription", product_type),
        "screen": [sw, sh],
        "frame": list(canvas.size),
        "offset": [g + b, b],
        "island": island,
        "notch": bool(profile.get("framebufferMask")) and not capabilities.get("DeviceSupportsDynamicIsland"),
    }
    return canvas, meta


def render_mask(product_type):
    """The display shape (rounded corners, notch) as a white PNG whose alpha is the visible
    screen, at the screen size `render` uses; None when the model has no mask. The page clips
    the screen image with it, so square screenshot corners do not show past the bezel."""
    found = find_device_type(product_type)
    if found is None or not found[1].get("framebufferMask"):
        return None
    resources, profile, capabilities = found
    screen = capabilities["ScreenDimensionsCapability"]
    px_scale = screen["main-screen-scale"]
    sw = round(screen["main-screen-width"] / px_scale * SCALE)
    sh = round(screen["main-screen-height"] / px_scale * SCALE)
    with tempfile.TemporaryDirectory() as tmp:
        alpha = rasterize(resources / f"{profile['framebufferMask']}.pdf", sw, sh, tmp).getchannel("A")
    mask = Image.new("RGBA", (sw, sh), (255, 255, 255, 0))
    mask.putalpha(alpha)
    return mask


def frame_files(product_type):
    """(PNG path, metadata) of the model's bezel, drawn on first use and then cached; None when
    the model has no bezel. The display mask is cached next to it as <model>.mask.png."""
    if not product_type or not PRODUCT_RE.match(product_type):
        return None
    cache = config.runtime_dir() / "frames"
    png, info = cache / f"{product_type}.png", cache / f"{product_type}.json"
    mask = cache / f"{product_type}.mask.png"
    if not (png.exists() and info.exists() and mask.exists()):
        rendered = render(product_type)
        if rendered is None:
            return None
        image, meta = rendered
        cache.mkdir(parents=True, exist_ok=True)
        image.save(png, optimize=True)
        shape = render_mask(product_type)
        if shape is not None:
            shape.save(mask, optimize=True)
        meta["mask"] = shape is not None
        info.write_text(json.dumps(meta))
    return png, json.loads(info.read_text())


def mask_file(product_type):
    """Path of the cached display mask PNG, or None."""
    found = frame_files(product_type)
    if found is None:
        return None
    mask = found[0].with_name(f"{product_type}.mask.png")
    return mask if mask.exists() else None


def frame_meta(product_type):
    """Bezel geometry for the page (no image), or None."""
    found = frame_files(product_type)
    if found is None:
        return None
    meta = found[1]
    out = {k: meta[k] for k in ("product", "name", "screen", "frame", "offset", "island", "notch")}
    out["mask"] = bool(meta.get("mask"))
    return out
