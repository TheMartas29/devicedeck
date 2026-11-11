"""View-only copy of pymobiledevice3's stream viewer, one per iPhone, shown in the preview page.

The page embeds /ios/<udid>/ in an iframe. That document is pymobiledevice3's viewer.html with
- every control hidden, so only the video canvas shows, filling the frame. The "Stream offline"
  overlay is hidden too: a still screen sends no frames for a while, which the viewer calls
  offline, and the worker restarts a stalled stream by itself. The page shows its own notice
  when the worker reports no stream;
- its resource and stream paths made relative, so they resolve under /ios/<udid>/;
- every pointer, mouse, touch, wheel and key event stopped before viewer.js sees it.

The real guarantee is on the server: preview.py passes only the read-only stream paths to the
worker (VIEWER_PROXY_PATHS) and answers 404 to everything else, including /touch, /key and
/button. The files in the venv are not changed.
"""
import importlib.resources
import re

import pymobiledevice3.resources

_DIR = importlib.resources.files(pymobiledevice3.resources) / "serve_web"
LOGO_PNG = (importlib.resources.files(pymobiledevice3.resources) / "webinspector" / "logo.png").read_bytes()

# Paths of the worker's stream server that the viewer may reach, with their method. All read
# the video; /pli asks the phone for a key frame and /restart restarts the video stream (the
# viewer's own recovery when frames stop). No input path is here.
VIEWER_PROXY_PATHS = {("GET", "codec"), ("GET", "stream.bin"), ("POST", "pli"), ("POST", "restart")}

BARE_CSS = """
html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:#000 !important}
body *{visibility:hidden !important}
/* will-change or transform on an ancestor would make it the box #c is fixed to */
#workspace,#stage-wrap,#stage,#device-frame{will-change:auto !important;transform:none !important;
  filter:none !important;contain:none !important}
#c{visibility:visible !important;position:fixed !important;inset:0 !important;margin:0 !important;
  width:100vw !important;height:100vh !important;object-fit:contain;border-radius:0 !important;
  transform:none !important;pointer-events:none !important;cursor:default !important}
#offline-overlay{display:none !important}
"""

# Runs before viewer.js: capture-phase listeners on window see every event first and stop it.
BLOCK_JS = r"""
(() => {
  const stop = (e) => { e.stopImmediatePropagation(); if (e.cancelable) e.preventDefault(); };
  for (const type of ['pointerdown', 'pointerup', 'pointermove', 'pointercancel', 'mousedown', 'mouseup',
                      'mousemove', 'click', 'dblclick', 'auxclick', 'contextmenu', 'wheel', 'touchstart',
                      'touchmove', 'touchend', 'keydown', 'keyup', 'keypress', 'paste', 'copy', 'cut',
                      'focusin', 'drop', 'dragover']) {
    window.addEventListener(type, stop, {capture: true, passive: false});
  }
  // Tell the page the video size, so it can size the frame when the model has no bezel data.
  let last = '';
  setInterval(() => {
    const c = document.getElementById('c');
    const size = c && c.width && c.height ? c.width + 'x' + c.height : '';
    if (size && size !== last && window.parent !== window) {
      last = size;
      window.parent.postMessage({deckSize: [c.width, c.height]}, location.origin);
    }
  }, 1000);
})();
"""


def _relative(text: bytes) -> bytes:
    # "/viewer.js" -> "viewer.js", fetch('/codec') -> fetch('./codec'), and so on.
    text = re.sub(rb'(href|src)="/(?!/)', rb'\1="', text)
    return text.replace(b"fetch('/", b"fetch('./")


def html() -> bytes:
    page = _relative((_DIR / "viewer.html").read_bytes())
    page = page.replace(b"<title>pymobiledevice3 screen</title>", b"<title>devicedeck iPhone</title>", 1)
    page = page.replace(b"</head>", b"<style>" + BARE_CSS.encode() + b"</style></head>", 1)
    return page.replace(b'<script src="viewer.js">', b"<script>" + BLOCK_JS.encode() + b'</script><script src="viewer.js">', 1)


def js() -> bytes:
    body = (_DIR / "viewer.js").read_bytes()
    body = body.replace(b"__AUDIO_DEFAULT_ON__", b"false").replace(b"__COMPENSATE_DEFAULT__", b"true")
    return _relative(body)


def css() -> bytes:
    return (_DIR / "viewer.css").read_bytes()
