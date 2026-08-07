#!/usr/bin/env python3
"""Multi-clipboard manager for macOS – rich text, visual previews, pinned clips,
   privacy controls, Instant In-Line Quick Look (hover a slot to peek),
   and Translate controls inside Quick Look (Apple language detection + MyMemory).
   (Network sync removed)
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import objc
import rumps
from AppKit import (
    NSAppearance,
    NSApplication,
    NSBackingStoreBuffered,
    NSBitmapImageRep,
    NSButton,
    NSColor,
    NSEvent,
    NSFont,
    NSImage,
    NSImageView,
    NSMakeRect,
    NSMakeSize,
    NSPanel,
    NSPasteboard,
    NSPasteboardTypeHTML,
    NSPasteboardTypePNG,
    NSPasteboardTypeRTF,
    NSPasteboardTypeString,
    NSPasteboardTypeTIFF,
    NSPopUpButton,
    NSRunningApplication,
    NSScreen,
    NSScrollView,
    NSSlider,
    NSTextField,
    NSTextView,
    NSTrackingArea,
    NSTrackingActiveAlways,
    NSTrackingInVisibleRect,
    NSTrackingMouseEnteredAndExited,
    NSVisualEffectView,
    NSVisualEffectMaterialHUDWindow,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive,
    NSView,
    NSWindow,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWorkspace,
)
from Foundation import NSData, NSObject
from quickmachotkey import quickHotKey, mask
from quickmachotkey.constants import kVK_ANSI_V, cmdKey, optionKey, shiftKey

# ---------------------------------------------------------------------------
# Const
# ---------------------------------------------------------------------------
NUM_SLOTS = 9
PREVIEW_LEN = 55
TOOLTIP_LEN = 800
DATA_FILE = Path.home() / ".multi-clipboard.json"
SETTINGS_FILE = Path.home() / ".pastedeck-settings.json"
CACHE_DIR = Path.home() / ".multi-clipboard-cache"
CACHE_DIR.mkdir(exist_ok=True)

PANEL_WIDTH = 400
SLOT_HEIGHT = 36
SLOT_GAP = 3
PANEL_PADDING = 12
TITLE_TOP_INSET = 14 
TITLE_HEIGHT = 48 

PANEL_HEIGHT = (
    TITLE_TOP_INSET
    + TITLE_HEIGHT
    + PANEL_PADDING
    + NUM_SLOTS * SLOT_HEIGHT
    + (NUM_SLOTS - 1) * SLOT_GAP
    + PANEL_PADDING
)

SLOT_KEYS = frozenset("1234567890")
KEY_DOWN_MASK = 1 << 10
MOUSE_DOWN_MASK = (1 << 1) | (1 << 3) | (1 << 25)
ESCAPE_KEYCODE = 53
DEFAULT_SENSITIVE_EXPIRE_SECONDS = 45


QL_MAX_WIDTH = 520
QL_MAX_HEIGHT = 420
QL_MIN_WIDTH = 280
QL_PADDING = 14
QL_IMAGE_MAX = 480

_active_panel = None
_previous_app = None
_settings_window = None

# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "sensitive_expire_seconds": DEFAULT_SENSITIVE_EXPIRE_SECONDS,
    "show_notifications": True,
    "poll_interval": 0.4,
}


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                settings.update({k: data[k] for k in DEFAULT_SETTINGS if k in data})
        except (OSError, json.JSONDecodeError):
            pass
    return settings


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass

# ---------------------------------------------------------------------------
# detection for sensitive stuff
# ---------------------------------------------------------------------------
SENSITIVE_BUNDLE_IDS = {
    "com.1password.1password",
    "com.1password.1password-launcher",
    "com.agilebits.onepassword7",
    "com.agilebits.onepassword4",
    "com.bitwarden.desktop",
    "com.lastpass.LastPass",
    "org.keepassxc.keepassxc",
    "com.apple.keychainaccess",
    "com.dashlane.dashlanephonefinal",
    "com.nordpass.macos",
    "com.enpass.desktop",
    "com.roboform.mac",
    "com.keepassx.keepassx",
    "org.keepassx.keepassxc",
}
SENSITIVE_NAME_KEYWORDS = {
    "1password", "bitwarden", "lastpass", "keepass", "keychain",
    "dashlane", "nordpass", "enpass", "roboform",
}

def is_sensitive_source(bundle_id: str | None, app_name: str | None = None) -> bool:
    if bundle_id and bundle_id in SENSITIVE_BUNDLE_IDS:
        return True
    if app_name and any(k in app_name.lower() for k in SENSITIVE_NAME_KEYWORDS):
        return True
    return False

def looks_like_secret(text: str) -> bool:
    """Strict heuristic – never flags URLs, file paths, or normal text."""
    if not text:
        return False
    text = text.strip()
    length = len(text)

    # reject url
    if text.lower().startswith(("http://", "https://", "ftp://", "www.")):
        return False
    if is_url(text):
        return False

    # reject local file paths / path-like strings
    # (absolute, ~, file://, multiple slashes, or extension after a slash)
    # These often have mixed case + digits + symbols and were falsely flagged.
    lower = text.lower()
    if (
        text.startswith(("/", "~"))
        or lower.startswith("file://")
        or text.count("/") >= 2          # e.g. /Users/…/file.py or a/b/c
        or ("/" in text and any(lower.endswith(ext) for ext in (
            ".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".rs", ".go",
            ".java", ".kt", ".c", ".cpp", ".h", ".hpp", ".m", ".mm",
            ".rb", ".php", ".sh", ".bash", ".zsh", ".fish",
            ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".xml",
            ".html", ".htm", ".css", ".scss", ".less", ".sql", ".log",
            ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".gif", ".webp",
            ".svg", ".pdf", ".zip", ".tar", ".gz", ".dmg", ".app",
            ".plist", ".entitlements", ".xcodeproj", ".xcworkspace",
            ".storyboard", ".xib",
        )))
    ):
        return False

    if length < 12 or length > 128:
        return False
    if any(c.isspace() for c in text):
        return False

    if lower in {"password", "secret", "token", "apikey", "api_key"}:
        return False

    has_upper = any(c.isupper() for c in text)
    has_lower = any(c.islower() for c in text)
    has_digit = any(c.isdigit() for c in text)
    has_symbol = any(not c.isalnum() for c in text)
    if sum([has_upper, has_lower, has_digit, has_symbol]) < 3:
        return False

    counts = Counter(text)
    entropy = 0.0
    for cnt in counts.values():
        p = cnt / length
        entropy -= p * math.log2(p)
    if entropy < 3.2:
        return False
    if counts.most_common(1)[0][1] > length * 0.45:
        return False
    return True

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def remember_frontmost_app():
    global _previous_app
    try:
        current = NSWorkspace.sharedWorkspace().frontmostApplication()
        our = NSRunningApplication.currentApplication().bundleIdentifier()
        if current and current.bundleIdentifier() != our:
            _previous_app = current
        else:
            # most recent non hidden app
            candidates = [
                app for app in NSWorkspace.sharedWorkspace().runningApplications()
                if (app.activationPolicy() == 0
                    and app.bundleIdentifier() != our
                    and not app.isHidden())
            ]
            _previous_app = candidates[0] if candidates else None
    except Exception:
        _previous_app = None

def restore_frontmost_app():
    global _previous_app
    if _previous_app is not None:
        try:
            _previous_app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
            time.sleep(0.05)  # settle
        except Exception:
            pass
        _previous_app = None

def get_frontmost_bundle_id() -> str | None:
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.bundleIdentifier() if app else None
    except Exception:
        return None

def slot_label(n: int) -> str:
    return str(n)

def key_to_slot(key: str) -> int | None:
    return int(key) if key in "123456789" else None

def truncate(text: str, length: int = PREVIEW_LEN) -> str:
    text = text.replace("\n", " ↵ ").replace("\t", " → ")
    return text if len(text) <= length else text[: length - 1] + "…"

def tooltip_text(text: str) -> str:
    if not text:
        return "Empty slot"
    return text if len(text) <= TOOLTIP_LEN else text[:TOOLTIP_LEN] + "…"

HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)

def parse_hex_color(text: str):
    text = text.strip()
    m = HEX_COLOR_RE.match(text)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        r, g, b = (int(c * 2, 16) / 255.0 for c in h)
        a = 1.0
    elif len(h) == 6:
        r, g, b = (int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))
        a = 1.0
    else:
        r, g, b, a = (int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4, 6))
    return r, g, b, a

def is_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))

def url_host(text: str) -> str:
    try:
        p = urlparse(text.strip())
        return p.netloc or p.path or text
    except Exception:
        return text


# File-path preview support: when clipboard text is a local file path,
# Quick Look can show content or a thumbnail for these extensions.
TEXT_PREVIEW_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".css",
    ".html", ".htm", ".xml", ".yaml", ".yml", ".csv", ".log",
    ".json", ".swift", ".rs", ".go", ".sh", ".rb", ".php",
    ".c", ".cpp", ".h", ".hpp", ".java", ".kt", ".sql",
})
IMAGE_PREVIEW_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".svg",
})
# Exactly 10 primary extensions highlighted for rich content preview
# (subset of TEXT + IMAGE that get special handling beyond plain text).
PRIMARY_FILE_PREVIEW_EXTS = (
    ".md", ".py", ".js", ".ts", ".css",
    ".html", ".xml", ".yaml", ".csv", ".log",
)


def try_local_file_path(text: str) -> Path | None:
    """Return a Path if *text* looks like an existing local file path."""
    if not text or len(text) > 1024 or "\n" in text or "\r" in text:
        return None
    raw = text.strip()
    # file:// URL
    if raw.lower().startswith("file://"):
        try:
            from urllib.parse import unquote
            parsed = urlparse(raw)
            if parsed.scheme.lower() != "file":
                return None
            path_str = unquote(parsed.path)
            # On macOS, path is usually absolute
            p = Path(path_str)
        except Exception:
            return None
    else:
        # Expand ~ and resolve relative-looking paths carefully
        if raw.startswith("~"):
            p = Path(raw).expanduser()
        elif raw.startswith("/"):
            p = Path(raw)
        else:
            # Avoid treating ordinary short text as relative paths
            return None
    try:
        if p.is_file() and p.stat().st_size >= 0:
            return p.resolve()
    except (OSError, RuntimeError):
        return None
    return None


def read_text_preview(path: Path, max_bytes: int = 48_000) -> str | None:
    """Read a text file for Quick Look, with size and encoding safety."""
    try:
        size = path.stat().st_size
        if size == 0:
            return "(empty file)"
        if size > 2_000_000:  # hard cap
            return f"(file too large to preview: {size:,} bytes)"
        data = path.read_bytes()[:max_bytes]
        # Detect encoding lightly
        for enc in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
            try:
                text = data.decode(enc)
                if len(data) == max_bytes and size > max_bytes:
                    text += "\n\n… (truncated)"
                return text
            except UnicodeDecodeError:
                continue
        return "(binary or unknown encoding)"
    except OSError:
        return None


def detect_language(text: str) -> str | None:
    """Apple NaturalLanguage framework – dominant language BCP-47 tag."""
    if not text or not text.strip():
        return None
    try:
        from NaturalLanguage import NLLanguageRecognizer
        recognizer = NLLanguageRecognizer.alloc().init()
        recognizer.processString_(text[:4000])
        lang = recognizer.dominantLanguage()
        return str(lang) if lang else None
    except Exception:
        return None


# Common target languages for the Quick Look dropdown (code, display name)
TRANSLATE_LANGS = [
    ("en", "English"),
    ("ar", "Arabic"),
    ("zh", "Chinese"),
    ("nl", "Dutch"),
    ("fr", "French"),
    ("de", "German"),
    ("hi", "Hindi"),
    ("id", "Indonesian"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("pl", "Polish"),
    ("pt", "Portuguese"),
    ("ru", "Russian"),
    ("es", "Spanish"),
    ("sv", "Swedish"),
    ("th", "Thai"),
    ("tr", "Turkish"),
    ("uk", "Ukrainian"),
    ("vi", "Vietnamese"),
]


def preferred_target_language() -> str:
    """Default target language is always English."""
    return "en"


def lang_display_name(code: str | None) -> str:
    """Human-readable name for a BCP-47 / ISO language code."""
    if not code or code.lower() in ("auto", "und", "unknown"):
        return "—"
    base = code.lower().split("-")[0].split("_")[0]
    for c, name in TRANSLATE_LANGS:
        if c == base:
            return name
    # Common extras from NLLanguageRecognizer
    extras = {
        "zh": "Chinese", "yue": "Cantonese", "wuu": "Wu Chinese",
        "nb": "Norwegian", "nn": "Norwegian", "no": "Norwegian",
        "he": "Hebrew", "iw": "Hebrew", "el": "Greek", "cs": "Czech",
        "da": "Danish", "fi": "Finnish", "hu": "Hungarian", "ro": "Romanian",
        "bg": "Bulgarian", "hr": "Croatian", "sk": "Slovak", "sl": "Slovenian",
        "ms": "Malay", "fa": "Persian", "ur": "Urdu", "bn": "Bengali",
        "ta": "Tamil", "te": "Telugu", "mr": "Marathi", "gu": "Gujarati",
    }
    return extras.get(base, base.upper())


def _ssl_context():
    """SSL context that works even when macOS Python certificates are broken."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        pass
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def translate_text(text: str, source: str = "auto", target: str | None = None) -> tuple[str | None, str | None]:
    """Translate text. Returns (translated_text, short_error).
    Primary: Google free endpoint. Fallback: MyMemory.
    Handles the common macOS Python SSL certificate problem.
    """
    if not text or not text.strip():
        return None, "Empty text"
    target = (target or preferred_target_language()).lower()
    src = (source or "auto").lower()
    if src != "auto" and src == target:
        return text, None

    def _short(err: Exception | str) -> str:
        s = str(err)
        if "CERTIFICATE_VERIFY_FAILED" in s or "certificate verify failed" in s.lower():
            return "SSL cert error"
        if "HTTP Error" in s:
            return s.split(":", 1)[-1].strip()[:60]
        if "urlopen error" in s.lower() or "nodename nor servname" in s.lower():
            return "Network error"
        if "timed out" in s.lower():
            return "Timed out"
        return s[:60]

    ctx = _ssl_context()
    google_err = "Empty response"

    # --- Primary: Google Translate free endpoint ---
    try:
        import urllib.request
        import urllib.parse
        params = urllib.parse.urlencode({
            "client": "gtx",
            "sl": src if src != "auto" else "auto",
            "tl": target,
            "dt": "t",
            "q": text[:4500],
        })
        url = f"https://translate.googleapis.com/translate_a/single?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(data, list) and data and isinstance(data[0], list):
                parts = []
                for item in data[0]:
                    if item and isinstance(item, list) and item[0]:
                        parts.append(str(item[0]))
                result = "".join(parts).strip()
                if result:
                    return result, None
    except Exception as e:
        google_err = _short(e)

    # --- Fallback: MyMemory ---
    try:
        import urllib.request
        import urllib.parse
        langpair = f"{src}|{target}" if src != "auto" else f"autodetect|{target}"
        params = urllib.parse.urlencode({
            "q": text[:4500],
            "langpair": langpair,
        })
        url = f"https://api.mymemory.translated.net/get?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "PasteDeck/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if data.get("responseStatus") == 200:
                result = (data.get("responseData") or {}).get("translatedText")
                if isinstance(result, str) and result.strip():
                    return result, None
            return None, f"Service error ({data.get('responseStatus')})"
    except Exception:
        return None, f"Translation failed ({google_err})"


def read_pasteboard() -> dict:
    pb = NSPasteboard.generalPasteboard()
    result = {
        "text": pb.stringForType_(NSPasteboardTypeString),
        "rtf": None, "html": None, "image": None,
        "changeCount": pb.changeCount(),
        "bundle_id": get_frontmost_bundle_id(),
    }
    rtf_data = pb.dataForType_(NSPasteboardTypeRTF)
    if rtf_data:
        result["rtf"] = bytes(rtf_data)
    html_data = pb.dataForType_(NSPasteboardTypeHTML)
    if html_data:
        result["html"] = bytes(html_data)
    for t in (NSPasteboardTypePNG, NSPasteboardTypeTIFF, "public.png", "public.tiff"):
        data = pb.dataForType_(t)
        if data:
            img = NSImage.alloc().initWithData_(data)
            if img and img.size().width > 0:
                result["image"] = img
                break
    return result

def write_pasteboard(slot: dict) -> None:
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    if slot.get("kind") == "image" and slot.get("image_hash"):
        path = CACHE_DIR / f"{slot['image_hash']}.png"
        if path.exists():
            data = NSData.dataWithBytes_length_(path.read_bytes(), path.stat().st_size)
            pb.setData_forType_(data, NSPasteboardTypePNG)
            img = NSImage.alloc().initWithData_(data)
            if img:
                tiff = img.TIFFRepresentation()
                if tiff:
                    pb.setData_forType_(tiff, NSPasteboardTypeTIFF)
        return
    if slot.get("rtf"):
        pb.setData_forType_(
            NSData.dataWithBytes_length_(slot["rtf"], len(slot["rtf"])),
            NSPasteboardTypeRTF,
        )
    if slot.get("html"):
        pb.setData_forType_(
            NSData.dataWithBytes_length_(slot["html"], len(slot["html"])),
            NSPasteboardTypeHTML,
        )
    text = slot.get("text") or ""
    if text:
        pb.setString_forType_(text, NSPasteboardTypeString)

def simulate_paste() -> None:
    """More reliable paste simulation."""
    import subprocess
    import Quartz
    from Quartz import (
        CGEventCreateKeyboardEvent, CGEventPost, CGEventSetFlags,
        kCGEventFlagMaskCommand, kCGHIDEventTap, kCGEventKeyDown, kCGEventKeyUp
    )

    restore_frontmost_app()
    # give app time to settle
    time.sleep(0.3)

    try:
        # use CGEvent for reliability
        source = Quartz.CGEventSourceCreate(0)  # kCGEventSourceStateHIDSystemState
        key_down = CGEventCreateKeyboardEvent(source, 9, True)   # 'v' = 9
        key_up   = CGEventCreateKeyboardEvent(source, 9, False)
        CGEventSetFlags(key_down, kCGEventFlagMaskCommand)
        CGEventSetFlags(key_up,   kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, key_down)
        CGEventPost(kCGHIDEventTap, key_up)
    except Exception:
        # fallback to applescript 
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to keystroke "v" using command down'],
                check=False, capture_output=True, timeout=2,
            )
        except Exception:
            pass

def get_cursor_point():
    import Quartz
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    screen = NSScreen.mainScreen().frame()
    return loc.x, screen.size.height - loc.y

def image_to_png_bytes(image: NSImage) -> bytes | None:
    try:
        tiff = image.TIFFRepresentation()
        if not tiff:
            return None
        from AppKit import NSPNGFileType
        rep = NSBitmapImageRep.imageRepWithData_(tiff)
        if not rep:
            return None
        png = rep.representationUsingType_properties_(NSPNGFileType, None)
        return bytes(png) if png else None
    except Exception:
        return None

def png_bytes_to_image(data: bytes) -> NSImage | None:
    try:
        return NSImage.alloc().initWithData_(
            NSData.dataWithBytes_length_(data, len(data))
        )
    except Exception:
        return None

def thumbnail(image: NSImage, max_side: float = 28.0) -> NSImage:
    size = image.size()
    if size.width <= 0 or size.height <= 0:
        return image
    scale = min(max_side / size.width, max_side / size.height, 1.0)
    new_w, new_h = size.width * scale, size.height * scale
    thumb = NSImage.alloc().initWithSize_((new_w, new_h))
    thumb.lockFocus()
    image.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, new_w, new_h),
        NSMakeRect(0, 0, size.width, size.height), 2, 1.0,
    )
    thumb.unlockFocus()
    return thumb

def minimize_terminal_window() -> None:
    """Minimize the terminal window using NSRunningApplication."""
    try:
        from AppKit import NSRunningApplication
        
        # list of terminal id
        terminal_bundle_ids = [
            "com.apple.Terminal",
            "com.googlecode.iterm2",
            "com.iterm2.iterm2",
        ]
        
        for bundle_id in terminal_bundle_ids:
            apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
            if apps:
                for app in apps:
                    try:
                        # hiding app
                        app.hide()
                        print(f"[DEBUG] Minimized {bundle_id}")
                        return
                    except Exception as e:
                        print(f"[DEBUG] Error hiding {bundle_id}: {e}")
        
        print("[DEBUG] No terminal application found to minimize.")
    except Exception as e:
        print(f"[DEBUG] Failed to minimize terminal: {e}")

# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
class ClipboardStore:
    def __init__(self, settings: dict | None = None) -> None:
        self._lock = threading.Lock()
        self._slots: list[dict] = [self._empty_slot() for _ in range(NUM_SLOTS)]
        self._ignore_next_change = False
        self._last_change_count = NSPasteboard.generalPasteboard().changeCount()
        self._title_cache: dict[str, str] = {}
        self.settings = settings if settings is not None else load_settings()
        self.load()
        self._reaper = threading.Thread(target=self._reaper_loop, daemon=True)
        self._reaper.start()

    @staticmethod
    def _empty_slot() -> dict:
        return {
            "text": "", "rtf": None, "html": None, "pinned": False,
            "kind": "text", "image_hash": None, "color": None,
            "url_title": None, "has_style": False, "sensitive": False,
            "created_at": 0.0,
        }

    def load(self) -> None:
        if not DATA_FILE.exists():
            return
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            slots = data.get("slots", [])
            with self._lock:
                for i in range(min(NUM_SLOTS, len(slots))):
                    entry = slots[i]
                    if isinstance(entry, str):
                        self._slots[i] = {**self._empty_slot(), "text": entry}
                    elif isinstance(entry, dict):
                        if entry.get("sensitive"):
                            self._slots[i] = self._empty_slot()
                            continue
                        rtf = entry.get("rtf")
                        html = entry.get("html")
                        if isinstance(rtf, str):
                            try:
                                rtf = base64.b64decode(rtf)
                            except Exception:
                                rtf = None
                        if isinstance(html, str):
                            try:
                                html = base64.b64decode(html)
                            except Exception:
                                html = None
                        self._slots[i] = {
                            "text": entry.get("text", "") or "",
                            "rtf": rtf, "html": html,
                            "pinned": bool(entry.get("pinned", False)),
                            "kind": entry.get("kind", "text"),
                            "image_hash": entry.get("image_hash"),
                            "color": entry.get("color"),
                            "url_title": entry.get("url_title"),
                            "has_style": bool(entry.get("has_style", False)),
                            "sensitive": False,
                            "created_at": entry.get("created_at", 0.0),
                        }
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        with self._lock:
            serializable = []
            for s in self._slots:
                if s.get("sensitive"):
                    serializable.append(self._empty_slot())
                    continue
                d = {
                    "text": s["text"], "pinned": s["pinned"], "kind": s["kind"],
                    "image_hash": s["image_hash"], "color": s["color"],
                    "url_title": s["url_title"], "has_style": s["has_style"],
                    "sensitive": False, "created_at": s.get("created_at", 0.0),
                }
                if s["rtf"]:
                    d["rtf"] = base64.b64encode(s["rtf"]).decode("ascii")
                if s["html"]:
                    d["html"] = base64.b64encode(s["html"]).decode("ascii")
                serializable.append(d)
            payload = {"slots": serializable}
        try:
            DATA_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {**{k: v for k, v in s.items() if k not in ("rtf", "html")}, "slot": i + 1}
                for i, s in enumerate(self._slots)
            ]

    def _classify(self, text: str):
        if not text:
            return "text", None, None
        color = parse_hex_color(text)
        if color:
            return "color", list(color), None
        if is_url(text):
            return "url", None, self._title_cache.get(text)
        return "text", None, None

    def push_from_pasteboard(self, pb_data: dict) -> None:
        bundle_id = pb_data.get("bundle_id")
        sensitive = is_sensitive_source(bundle_id)

        if pb_data.get("image") is not None:
            self._push_image(pb_data["image"], sensitive=sensitive)
            return

        text = (pb_data.get("text") or "").strip()
        rtf = pb_data.get("rtf")
        html = pb_data.get("html")
        if not text and not rtf and not html:
            return

        if not sensitive and looks_like_secret(text):
            sensitive = True

        kind, color, url_title = self._classify(text)
        has_style = bool(rtf or html)

        with self._lock:
            self._slots = [
                s for s in self._slots
                if not (not s["pinned"] and s["kind"] != "image"
                        and s["text"] == text and bool(s.get("rtf")) == bool(rtf))
            ]
            unpinned = [s for s in self._slots if not s["pinned"]]
            pinned = [s for s in self._slots if s["pinned"]]
            new_entry = {
                "text": text or "[Rich Text]",
                "rtf": None if sensitive else rtf,
                "html": None if sensitive else html,
                "pinned": False, "kind": kind, "image_hash": None,
                "color": color, "url_title": url_title,
                "has_style": has_style and not sensitive,
                "sensitive": sensitive, "created_at": time.time(),
            }
            unpinned.insert(0, new_entry)
            unpinned = unpinned[: NUM_SLOTS - len(pinned)]
            new_slots = [self._empty_slot() for _ in range(NUM_SLOTS)]
            for i, p in enumerate(pinned):
                if i < NUM_SLOTS:
                    new_slots[i] = p
            free = len(pinned)
            for u in unpinned:
                if free >= NUM_SLOTS:
                    break
                new_slots[free] = u
                free += 1
            self._slots = new_slots
        self.save()

        if kind == "url" and text and text not in self._title_cache and not sensitive:
            with self._lock:
                for i, s in enumerate(self._slots):
                    if s["text"] == text and s["kind"] == "url":
                        self._fetch_title_async(text, i)
                        break

    def _push_image(self, image: NSImage, sensitive: bool = False) -> None:
        png = image_to_png_bytes(image)
        if not png:
            return
        h = hashlib.sha1(png).hexdigest()[:16]
        path = CACHE_DIR / f"{h}.png"
        if not path.exists() and not sensitive:
            try:
                path.write_bytes(png)
            except OSError:
                return
        with self._lock:
            self._slots = [
                s for s in self._slots
                if not (s.get("image_hash") == h and not s["pinned"])
            ]
            unpinned = [s for s in self._slots if not s["pinned"]]
            pinned = [s for s in self._slots if s["pinned"]]
            new_entry = {
                "text": f"[Image {image.size().width:.0f}×{image.size().height:.0f}]",
                "rtf": None, "html": None, "pinned": False, "kind": "image",
                "image_hash": None if sensitive else h, "color": None,
                "url_title": None, "has_style": False,
                "sensitive": sensitive, "created_at": time.time(),
            }
            unpinned.insert(0, new_entry)
            unpinned = unpinned[: NUM_SLOTS - len(pinned)]
            new_slots = [self._empty_slot() for _ in range(NUM_SLOTS)]
            for i, p in enumerate(pinned):
                if i < NUM_SLOTS:
                    new_slots[i] = p
            free = len(pinned)
            for u in unpinned:
                if free >= NUM_SLOTS:
                    break
                new_slots[free] = u
                free += 1
            self._slots = new_slots
        self.save()

    def delete_slot(self, slot_number: int) -> None:
        index = slot_number - 1
        if 0 <= index < NUM_SLOTS:
            with self._lock:
                self._slots[index] = self._empty_slot()
            self.save()

    def toggle_pin(self, slot_number: int) -> None:
        index = slot_number - 1
        if not 0 <= index < NUM_SLOTS:
            return
        with self._lock:
            s = self._slots[index]
            if not s["text"] and not s["image_hash"]:
                return
            if s.get("sensitive"):
                return
            s["pinned"] = not s["pinned"]
        self.save()

    def get_slot(self, slot_number: int) -> dict:
        index = slot_number - 1
        with self._lock:
            return dict(self._slots[index]) if 0 <= index < NUM_SLOTS else self._empty_slot()

    def replace_slot_text(self, slot_number: int, new_text: str) -> bool:
        """Replace a slot's text with plain translated text. Returns True on success."""
        index = slot_number - 1
        new_text = (new_text or "").strip()
        if not new_text or not (0 <= index < NUM_SLOTS):
            return False
        with self._lock:
            s = self._slots[index]
            if not s.get("text") and not s.get("image_hash"):
                return False
            kind, color, url_title = self._classify(new_text)
            s["text"] = new_text
            s["rtf"] = None
            s["html"] = None
            s["has_style"] = False
            s["kind"] = kind
            s["color"] = color
            s["url_title"] = url_title
            s["image_hash"] = None
            # keep pin / sensitive / created_at
        self.save()
        return True

    def paste_slot(self, slot_number: int) -> None:
        s = self.get_slot(slot_number)
        if not s.get("text") and not s.get("image_hash"):
            return
        self._ignore_next_change = True
        write_pasteboard(s)
        threading.Thread(target=simulate_paste, daemon=True).start()
        if s.get("sensitive"):
            threading.Timer(0.8, lambda: self.delete_slot(slot_number)).start()

    def clear(self) -> None:
        with self._lock:
            for s in self._slots:
                if not s["pinned"]:
                    s.update(self._empty_slot())
        self.save()

    def clear_all(self) -> None:
        with self._lock:
            self._slots = [self._empty_slot() for _ in range(NUM_SLOTS)]
        self.save()

    def clear_sensitive(self) -> None:
        with self._lock:
            for s in self._slots:
                if s.get("sensitive"):
                    s.update(self._empty_slot())
        self.save()

    def poll_clipboard(self) -> None:
        pb = NSPasteboard.generalPasteboard()
        count = pb.changeCount()
        if count == self._last_change_count:
            return
        self._last_change_count = count
        if self._ignore_next_change:
            self._ignore_next_change = False
            return
        self.push_from_pasteboard(read_pasteboard())

    def _reaper_loop(self) -> None:
        while True:
            time.sleep(5)
            expire = int(self.settings.get("sensitive_expire_seconds", DEFAULT_SENSITIVE_EXPIRE_SECONDS))
            if expire <= 0:
                continue
            now = time.time()
            changed = False
            with self._lock:
                for s in self._slots:
                    if (s.get("sensitive") and s.get("created_at")
                            and now - s["created_at"] > expire):
                        s.update(self._empty_slot())
                        changed = True
            if changed:
                self.save()

    def _fetch_title_async(self, url: str, slot_index: int) -> None:
        def worker():
            try:
                import urllib.request
                req = urllib.request.Request(
                    url, headers={"User-Agent": "PasteDeck/1.0"}
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    html = resp.read(8192).decode("utf-8", errors="ignore")
                m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
                title = m.group(1).strip() if m else None
                if title:
                    title = re.sub(r"\s+", " ", title)[:80]
                    self._title_cache[url] = title
                    with self._lock:
                        if (0 <= slot_index < NUM_SLOTS
                                and self._slots[slot_index]["text"] == url):
                            self._slots[slot_index]["url_title"] = title
                    self.save()
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class QuickLookHoverView(NSVisualEffectView):
    """Vibrancy content view that reports mouse enter/exit so the preview stays open."""

    def initWithFrame_onHover_(self, frame, on_hover):
        self = objc.super(QuickLookHoverView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.on_hover = on_hover
        tracking = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited
            | NSTrackingActiveAlways
            | NSTrackingInVisibleRect,
            self,
            None,
        )
        self.addTrackingArea_(tracking)
        return self

    def mouseEntered_(self, event):
        if self.on_hover:
            self.on_hover(True)

    def mouseExited_(self, event):
        if self.on_hover:
            self.on_hover(False)


class QuickLookPreviewPanel(NSPanel):
    """Temporary floating preview – macOS Quick Look style, shown on hover."""

    def init(self):
        self = objc.super(QuickLookPreviewPanel, self).initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, QL_MIN_WIDTH, 120),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        if self is None:
            return None
        self.setLevel_(26)  # above the picker panel
        self.setOpaque_(False)
        self.setBackgroundColor_(NSColor.clearColor())
        self.setHasShadow_(True)
        self.setHidesOnDeactivate_(False)
        self.setCanHide_(False)
        self.setCollectionBehavior_(128 | 256)
        self._slot = None
        self._picker = None
        self._over_preview = False
        self._text_view = None
        self._scroll_view = None
        self._original_text = None
        self._translated_text = None
        self._translate_btn = None
        self._replace_btn = None
        self._lang_popup = None
        self._detected_label = None
        self._detected_lang = None
        self._target_lang = "en"
        self._translating = False
        return self

    def showForSlot_nearPanel_(self, slot_data, picker_panel):
        """Build content for slot_data and position next to the picker.
        Selector: showForSlot:nearPanel:  (2 args after self).
        """
        self._slot = slot_data.get("slot")
        self._picker = picker_panel
        self._over_preview = False
        self._text_view = None
        self._scroll_view = None
        self._original_text = None
        self._translated_text = None
        self._translate_btn = None
        self._replace_btn = None
        self._lang_popup = None
        self._detected_label = None
        self._detected_lang = None
        self._target_lang = "en"
        self._translating = False
        kind = slot_data.get("kind", "text")
        text = slot_data.get("text") or ""
        sensitive = slot_data.get("sensitive", False)
        image_hash = slot_data.get("image_hash")

        radius = 10.0
        # content container with vibrancy + clean rounded mask (no black rim)
        content = QuickLookHoverView.alloc().initWithFrame_onHover_(
            NSMakeRect(0, 0, QL_MIN_WIDTH, 120),
            self._on_preview_hover,
        )
        content.setMaterial_(NSVisualEffectMaterialHUDWindow)
        content.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        content.setState_(NSVisualEffectStateActive)
        content.setWantsLayer_(True)
        # keeping HUD dark so the light text stays readable in system light mode
        content.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))

        try:
            from AppKit import NSBezierPath, NSEdgeInsetsMake, NSImageResizingModeStretch
            mask = NSImage.alloc().initWithSize_((radius * 2, radius * 2))
            mask.lockFocus()
            NSColor.blackColor().set()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0, 0, radius * 2, radius * 2), radius, radius
            )
            path.fill()
            mask.unlockFocus()
            mask.setCapInsets_(NSEdgeInsetsMake(radius, radius, radius, radius))
            mask.setResizingMode_(NSImageResizingModeStretch)
            content.setMaskImage_(mask)
        except Exception:
            pass

        layer = content.layer()
        if layer is not None:
            layer.setCornerRadius_(radius)
            layer.setMasksToBounds_(True)
            layer.setBorderWidth_(0.6)
            layer.setBorderColor_(
                NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).CGColor()
            )

        width = QL_MIN_WIDTH
        height = 80

        if sensitive:
            label = NSTextField.alloc().initWithFrame_(
                NSMakeRect(QL_PADDING, QL_PADDING, QL_MIN_WIDTH - 2 * QL_PADDING, 40)
            )
            label.setStringValue_("🔒 Sensitive – preview hidden")
            label.setBezeled_(False)
            label.setDrawsBackground_(False)
            label.setEditable_(False)
            label.setSelectable_(False)
            label.setFont_(NSFont.systemFontOfSize_(13))
            label.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.85, 1.0))
            content.addSubview_(label)
            width, height = QL_MIN_WIDTH, 60

        elif kind == "image" and image_hash:
            path = CACHE_DIR / f"{image_hash}.png"
            if path.exists():
                img = png_bytes_to_image(path.read_bytes())
                if img:
                    sz = img.size()
                    iw, ih = float(sz.width), float(sz.height)
                    scale = min(QL_IMAGE_MAX / max(iw, 1), QL_IMAGE_MAX / max(ih, 1), 1.0)
                    disp_w = max(80, int(iw * scale))
                    disp_h = max(80, int(ih * scale))
                    iv = NSImageView.alloc().initWithFrame_(
                        NSMakeRect(QL_PADDING, QL_PADDING, disp_w, disp_h)
                    )
                    iv.setImage_(img)
                    iv.setImageScaling_(3)  # NSImageScaleProportionallyUpOrDown
                    iv.setWantsLayer_(True)
                    iv.layer().setCornerRadius_(6.0)
                    iv.layer().setMasksToBounds_(True)
                    content.addSubview_(iv)
                    width = disp_w + 2 * QL_PADDING
                    height = disp_h + 2 * QL_PADDING
                else:
                    width, height = self._add_text_preview(content, "[Image unavailable]")
            else:
                width, height = self._add_text_preview(content, "[Image not in cache]")

        elif kind == "color" and slot_data.get("color"):
            r, g, b, a = slot_data["color"]
            swatch_size = 120
            swatch = NSView.alloc().initWithFrame_(
                NSMakeRect(QL_PADDING, QL_PADDING + 28, swatch_size, swatch_size)
            )
            swatch.setWantsLayer_(True)
            swatch.layer().setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a).CGColor()
            )
            swatch.layer().setCornerRadius_(8.0)
            swatch.layer().setBorderWidth_(1.0)
            swatch.layer().setBorderColor_(
                NSColor.colorWithCalibratedWhite_alpha_(0.4, 1.0).CGColor()
            )
            content.addSubview_(swatch)
            hex_label = NSTextField.alloc().initWithFrame_(
                NSMakeRect(QL_PADDING, QL_PADDING, swatch_size + 40, 22)
            )
            hex_label.setStringValue_(text or f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}")
            hex_label.setBezeled_(False)
            hex_label.setDrawsBackground_(False)
            hex_label.setEditable_(False)
            hex_label.setSelectable_(False)
            hex_label.setFont_(NSFont.monospacedSystemFontOfSize_weight_(13, 0.4))
            hex_label.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.9, 1.0))
            content.addSubview_(hex_label)
            width = swatch_size + 2 * QL_PADDING
            height = swatch_size + 28 + 2 * QL_PADDING

        else:
            # File-path preview: if clipboard text points to an existing local file,
            # show image thumbnail or text content for supported extensions.
            file_path = try_local_file_path(text)
            if file_path is not None:
                ext = file_path.suffix.lower()
                if ext in IMAGE_PREVIEW_EXTS:
                    try:
                        img = NSImage.alloc().initWithContentsOfFile_(str(file_path))
                        if img and img.size().width > 0:
                            sz = img.size()
                            iw, ih = float(sz.width), float(sz.height)
                            scale = min(QL_IMAGE_MAX / max(iw, 1), QL_IMAGE_MAX / max(ih, 1), 1.0)
                            disp_w = max(80, int(iw * scale))
                            disp_h = max(80, int(ih * scale))
                            # header with filename
                            name_label = NSTextField.alloc().initWithFrame_(
                                NSMakeRect(QL_PADDING, QL_PADDING + disp_h + 6, max(disp_w, 180), 18)
                            )
                            name_label.setStringValue_(file_path.name)
                            name_label.setBezeled_(False)
                            name_label.setDrawsBackground_(False)
                            name_label.setEditable_(False)
                            name_label.setSelectable_(False)
                            name_label.setFont_(NSFont.systemFontOfSize_weight_(11, 0.4))
                            name_label.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.75, 1.0))
                            content.addSubview_(name_label)
                            iv = NSImageView.alloc().initWithFrame_(
                                NSMakeRect(QL_PADDING, QL_PADDING, disp_w, disp_h)
                            )
                            iv.setImage_(img)
                            iv.setImageScaling_(3)
                            iv.setWantsLayer_(True)
                            iv.layer().setCornerRadius_(6.0)
                            iv.layer().setMasksToBounds_(True)
                            content.addSubview_(iv)
                            width = max(disp_w, 180) + 2 * QL_PADDING
                            height = disp_h + 28 + 2 * QL_PADDING
                        else:
                            width, height = self._add_file_info_preview(content, file_path)
                    except Exception:
                        width, height = self._add_file_info_preview(content, file_path)
                elif ext in TEXT_PREVIEW_EXTS:
                    body = read_text_preview(file_path)
                    if body is None:
                        width, height = self._add_file_info_preview(content, file_path)
                    else:
                        # pretty-print JSON files
                        if ext == ".json":
                            try:
                                body = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
                            except Exception:
                                pass
                        header = f"📄 {file_path.name}"
                        display = f"{header}\n{'─' * min(40, len(header) + 4)}\n{body}"
                        width, height = self._add_text_preview(content, display)
                else:
                    # unsupported extension – show icon + metadata
                    width, height = self._add_file_info_preview(content, file_path)
            else:
                # readable expanded popup (plain text / JSON)
                display = text
                if not display and kind != "image":
                    display = "(empty)"
                # pretty-print json whenever possible
                stripped = display.strip()
                if (stripped.startswith("{") and stripped.endswith("}")) or (
                    stripped.startswith("[") and stripped.endswith("]")
                ):
                    try:
                        display = json.dumps(json.loads(stripped), indent=2, ensure_ascii=False)
                    except Exception:
                        pass
                width, height = self._add_text_preview(content, display)

        content.setFrame_(NSMakeRect(0, 0, width, height))
        self.setContentView_(content)
        self.setContentSize_(NSMakeSize(width, height))

        # Position beside the picker, vertically aligned with the hovered slot
        # so the mouse can reach the preview from any slot (including 3+).
        pf = picker_panel.frame()
        screen = NSScreen.mainScreen().visibleFrame()
        ql_x = pf.origin.x + pf.size.width + 10
        if ql_x + width > screen.origin.x + screen.size.width - 8:
            ql_x = pf.origin.x - width - 10

        # Prefer real on-screen slot rect (set by picker before calling us)
        anchor = getattr(self, "_anchor_rect", None)
        if anchor is not None:
            try:
                # Center QL vertically on the slot; bias slightly upward so the
                # top edge stays near the slot for easy mouse entry.
                slot_mid = anchor.origin.y + anchor.size.height / 2.0
                ql_y = slot_mid - height / 2.0
            except Exception:
                anchor = None
        if anchor is None:
            # Fallback: compute from slot number + panel layout constants
            slot_num = slot_data.get("slot") or 1
            try:
                slot_index = max(0, min(NUM_SLOTS - 1, int(slot_num) - 1))
            except Exception:
                slot_index = 0
            content_top = PANEL_HEIGHT - TITLE_TOP_INSET - TITLE_HEIGHT - 4
            row_y = content_top - (slot_index + 1) * SLOT_HEIGHT - slot_index * SLOT_GAP
            slot_mid = pf.origin.y + row_y + SLOT_HEIGHT / 2.0
            ql_y = slot_mid - height / 2.0

        # Keep fully on screen
        if ql_y < screen.origin.y + 8:
            ql_y = screen.origin.y + 8
        if ql_y + height > screen.origin.y + screen.size.height - 8:
            ql_y = screen.origin.y + screen.size.height - height - 8
        self.setFrameOrigin_((ql_x, ql_y))
        self.orderFrontRegardless()

    def _on_preview_hover(self, entered):
        self._over_preview = bool(entered)
        if self._picker is not None:
            self._picker.on_quicklook_hover(entered)

    def is_mouse_over(self) -> bool:
        return bool(self._over_preview)

    def _style_glass_button(self, btn, primary: bool = False, font_size: float = 9.0):
        """Apply liquid-glass look: frosted translucent fill, soft border, pill shape."""
        btn.setBordered_(False)
        btn.setBezelStyle_(0)  # NSBezelStyleNone – full control via layer
        try:
            btn.setControlSize_(1)
        except Exception:
            pass
        enabled = bool(btn.isEnabled())
        weight = 0.45 if primary else 0.3
        btn.setFont_(NSFont.systemFontOfSize_weight_(font_size, weight))
        btn.setWantsLayer_(True)
        layer = btn.layer()
        if layer is None:
            return
        h = btn.frame().size.height
        layer.setCornerRadius_(h / 2.0)
        layer.setMasksToBounds_(True)
        fill_a = (0.20 if primary else 0.09) if enabled else 0.05
        border_a = (0.42 if primary else 0.24) if enabled else 0.12
        text_a = (0.98 if primary else 0.88) if enabled else 0.40
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, fill_a).CGColor()
        )
        layer.setBorderWidth_(0.6 if primary else 0.5)
        layer.setBorderColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, border_a).CGColor()
        )
        try:
            btn.setContentTintColor_(
                NSColor.colorWithCalibratedWhite_alpha_(1.0, text_a)
            )
        except Exception:
            pass
        try:
            layer.setShadowOpacity_(0.0)
        except Exception:
            pass

    def _style_glass_popup(self, popup):
        """Pill / liquid-glass styling for NSPopUpButton."""
        try:
            popup.setBordered_(False)
        except Exception:
            pass
        try:
            popup.setBezelStyle_(0)
        except Exception:
            pass
        try:
            popup.setControlSize_(1)
        except Exception:
            pass
        popup.setFont_(NSFont.systemFontOfSize_weight_(10.0, 0.35))
        popup.setWantsLayer_(True)
        layer = popup.layer()
        if layer is None:
            return
        h = popup.frame().size.height
        layer.setCornerRadius_(h / 2.0)
        layer.setMasksToBounds_(True)
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.10).CGColor()
        )
        layer.setBorderWidth_(0.5)
        layer.setBorderColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.28).CGColor()
        )
        try:
            popup.setContentTintColor_(
                NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.92)
            )
        except Exception:
            pass

    def _style_glass_chip(self, view_or_field):
        """Small glass pill chip (e.g. detected-language label)."""
        view_or_field.setWantsLayer_(True)
        layer = view_or_field.layer()
        if layer is None:
            return
        h = view_or_field.frame().size.height
        layer.setCornerRadius_(h / 2.0)
        layer.setMasksToBounds_(True)
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.08).CGColor()
        )
        layer.setBorderWidth_(0.5)
        layer.setBorderColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.20).CGColor()
        )

    def _set_detected_label(self, code: str | None):
        """Update the detected-language chip text (compact so it fits the pill)."""
        self._detected_lang = code
        label = getattr(self, "_detected_label", None)
        if label is None:
            return
        name = lang_display_name(code)
        if code and code.lower() not in ("auto", "und", "unknown", ""):
            # Short names stay readable in the fixed-width chip
            short = name if len(name) <= 12 else (name[:11] + "…")
            label.setStringValue_(short)
            label.setToolTip_(f"Detected: {name} ({code})")
        else:
            label.setStringValue_("…")
            label.setToolTip_("Detecting language…")

    def _add_file_info_preview(self, content, path: Path):
        """Show file icon + name + size + path for unsupported or non-text files."""
        icon_size = 64
        try:
            icon = NSWorkspace.sharedWorkspace().iconForFile_(str(path))
            if icon:
                icon.setSize_((icon_size, icon_size))
        except Exception:
            icon = None

        y = QL_PADDING
        if icon:
            iv = NSImageView.alloc().initWithFrame_(
                NSMakeRect(QL_PADDING, y + 40, icon_size, icon_size)
            )
            iv.setImage_(icon)
            iv.setImageScaling_(3)
            content.addSubview_(iv)

        name_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(QL_PADDING + icon_size + 10, y + 70, 220, 22)
        )
        name_label.setStringValue_(path.name)
        name_label.setBezeled_(False)
        name_label.setDrawsBackground_(False)
        name_label.setEditable_(False)
        name_label.setSelectable_(True)
        name_label.setFont_(NSFont.systemFontOfSize_weight_(13, 0.5))
        name_label.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.95, 1.0))
        content.addSubview_(name_label)

        try:
            size = path.stat().st_size
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size / (1024 * 1024):.1f} MB"
        except OSError:
            size_str = "?"

        meta = NSTextField.alloc().initWithFrame_(
            NSMakeRect(QL_PADDING + icon_size + 10, y + 48, 220, 18)
        )
        meta.setStringValue_(f"{size_str}  ·  {path.suffix.lower() or 'no ext'}")
        meta.setBezeled_(False)
        meta.setDrawsBackground_(False)
        meta.setEditable_(False)
        meta.setSelectable_(False)
        meta.setFont_(NSFont.systemFontOfSize_(11))
        meta.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.65, 1.0))
        content.addSubview_(meta)

        path_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(QL_PADDING, y, 300, 36)
        )
        path_label.setStringValue_(str(path))
        path_label.setBezeled_(False)
        path_label.setDrawsBackground_(False)
        path_label.setEditable_(False)
        path_label.setSelectable_(True)
        path_label.setFont_(NSFont.monospacedSystemFontOfSize_weight_(10, 0.3))
        path_label.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.55, 1.0))
        content.addSubview_(path_label)

        width = max(320, icon_size + 240 + 2 * QL_PADDING)
        height = icon_size + 50 + 2 * QL_PADDING
        return width, height

    def _add_text_preview(self, content, text: str):
        """Add a scrollable text view + liquid-glass controls; return (width, height)."""
        max_chars = 12000
        if len(text) > max_chars:
            text = text[:max_chars] + "\n… (truncated)"

        self._original_text = text
        self._target_lang = "en"
        self._detected_lang = None

        lines = text.count("\n") + 1
        longest = max((len(ln) for ln in text.splitlines()), default=20)
        est_w = min(QL_MAX_WIDTH, max(QL_MIN_WIDTH, min(longest * 7 + 2 * QL_PADDING, QL_MAX_WIDTH)))
        ctrl_h = 28
        est_h = min(QL_MAX_HEIGHT, max(110, min(lines * 18 + 2 * QL_PADDING + ctrl_h + 8, QL_MAX_HEIGHT)))

        text_h = est_h - 2 * QL_PADDING - ctrl_h - 6
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(QL_PADDING, QL_PADDING + ctrl_h + 6, est_w - 2 * QL_PADDING, text_h)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(False)
        scroll.setBorderType_(0)
        scroll.setDrawsBackground_(False)
        try:
            scroll.setScrollerStyle_(1)  # NSScrollerStyleOverlay
        except Exception:
            pass

        tv = NSTextView.alloc().initWithFrame_(scroll.contentView().bounds())
        tv.setString_(text)
        tv.setEditable_(False)
        tv.setSelectable_(True)
        tv.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, 0.3)
                    if text.lstrip().startswith(("{", "["))
                    else NSFont.systemFontOfSize_(13))
        tv.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.92, 1.0))
        tv.setBackgroundColor_(NSColor.clearColor())
        tv.setDrawsBackground_(False)
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)
        tv.textContainer().setWidthTracksTextView_(True)
        tv.setMaxSize_(NSMakeSize(est_w - 2 * QL_PADDING, 1e7))
        scroll.setDocumentView_(tv)
        content.addSubview_(scroll)
        self._text_view = tv
        self._scroll_view = scroll

        # Bottom control bar: [detected chip] …… [lang pill] [Translate] [Replace]
        # Compact controls, shared baseline so nothing sits higher than its neighbors.
        det_w = 78
        popup_w = 90
        btn_w = 85          # compact Translate / Replace
        gap = 5
        mid_gap = 8
        btn_h = 22          # shared control height
        y = QL_PADDING + 3  # optically center in the control strip
        total_right = popup_w + gap + btn_w + gap + btn_w
        min_for_controls = QL_PADDING + det_w + mid_gap + total_right + QL_PADDING
        if est_w < min_for_controls:
            est_w = min_for_controls
            scroll.setFrame_(
                NSMakeRect(QL_PADDING, QL_PADDING + ctrl_h + 6, est_w - 2 * QL_PADDING, text_h)
            )
            tv.setMaxSize_(NSMakeSize(est_w - 2 * QL_PADDING, 1e7))
        start_x = est_w - QL_PADDING - total_right

        # Detected-language glass chip — outer glass view + vertically centered label
        det_wrap = NSView.alloc().initWithFrame_(
            NSMakeRect(QL_PADDING, y, det_w, btn_h)
        )
        self._style_glass_chip(det_wrap)
        det = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, 0, det_w, btn_h)
        )
        det.setStringValue_("…")
        det.setBezeled_(False)
        det.setDrawsBackground_(False)
        det.setEditable_(False)
        det.setSelectable_(False)
        det.setFont_(NSFont.systemFontOfSize_weight_(9.0, 0.35))
        det.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.88, 1.0))
        det.setAlignment_(1)  # center
        try:
            cell = det.cell()
            if cell is not None:
                cell.setAlignment_(1)
                try:
                    cell.setUsesSingleLineMode_(True)
                except Exception:
                    pass
        except Exception:
            pass
        # Vertically center label inside the pill (NSTextField draws high by default)
        label_h = 14
        label_y = int((btn_h - label_h) / 2) - 1
        det.setFrame_(NSMakeRect(2, label_y, det_w - 4, label_h))
        det.setToolTip_("Detected language")
        det_wrap.addSubview_(det)
        content.addSubview_(det_wrap)
        self._detected_label = det

        # Language dropdown – glass pill (right group)
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(start_x, y, popup_w, btn_h), False
        )
        popup.removeAllItems()
        for code, name in TRANSLATE_LANGS:
            popup.addItemWithTitle_(f"{name}")
        popup.selectItemAtIndex_(0)
        popup.setTarget_(self)
        popup.setAction_("targetLangChanged:")
        popup.setToolTip_("Target language")
        self._style_glass_popup(popup)
        content.addSubview_(popup)
        self._lang_popup = popup

        # Translate – compact primary glass (icon + short label)
        btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(start_x + popup_w + gap, y, btn_w, btn_h)
        )
        btn.setTitle_("Translate")
        btn.setTarget_(self)
        btn.setAction_("translateClicked:")
        btn.setToolTip_("Translate this text")
        btn.setImage_(None)
        btn.setImagePosition_(0)  # NSNoImage – title only
        try:
            btn.setAlignment_(1)  # NSTextAlignmentCenter
        except Exception:
            pass
        self._style_glass_button(btn, primary=True, font_size=9.0)
        content.addSubview_(btn)
        self._translate_btn = btn

        # Replace – matching compact secondary glass
        rep = NSButton.alloc().initWithFrame_(
            NSMakeRect(start_x + popup_w + gap + btn_w + gap, y, btn_w, btn_h)
        )
        rep.setTitle_("Replace")
        rep.setEnabled_(False)
        rep.setTarget_(self)
        rep.setAction_("replaceClicked:")
        rep.setToolTip_("Replace slot contents with the translation")
        rep.setImage_(None)
        rep.setImagePosition_(0)
        try:
            rep.setAlignment_(1)  # NSTextAlignmentCenter
        except Exception:
            pass
        self._style_glass_button(rep, primary=False, font_size=9.0)
        content.addSubview_(rep)
        self._replace_btn = rep

        # Async language detection so the chip fills in quickly
        original = text
        def detect_worker():
            src = detect_language(original)
            def apply():
                if self._original_text is original and getattr(self, "_detected_label", None):
                    self._set_detected_label(src)
            try:
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(apply)
            except Exception:
                apply()
        threading.Thread(target=detect_worker, daemon=True).start()

        return est_w, est_h

    def targetLangChanged_(self, sender):
        """Update selected target language from the dropdown."""
        idx = sender.indexOfSelectedItem()
        if 0 <= idx < len(TRANSLATE_LANGS):
            self._target_lang = TRANSLATE_LANGS[idx][0]
        else:
            self._target_lang = "en"

    def translateClicked_(self, sender):
        """Action: run language detection + translation in background, update panel."""
        if self._translating or not self._original_text or not self._text_view:
            return
        self._translating = True
        self._translated_text = None
        if self._translate_btn:
            self._translate_btn.setEnabled_(False)
            self._translate_btn.setTitle_("…")
            self._translate_btn.setImage_(None)
            self._style_glass_button(self._translate_btn, primary=True, font_size=9.0)
        if getattr(self, "_lang_popup", None):
            self._lang_popup.setEnabled_(False)
        if getattr(self, "_replace_btn", None):
            self._replace_btn.setEnabled_(False)
            self._replace_btn.setTitle_("Replace")
            self._style_glass_button(self._replace_btn, primary=False, font_size=9.0)

        original = self._original_text
        tv = self._text_view
        target = getattr(self, "_target_lang", None) or "en"

        def worker():
            src = detect_language(original) or "auto"
            translated_result = None
            if src != "auto" and src.lower().split("-")[0] == target.lower():
                result_text = original
                status = f"Already {src}"
                scroll_to_translation = False
            else:
                translated, err = translate_text(original, source=src, target=target)
                if translated:
                    header = f"[{src or '?'} → {target}]"
                    result_text = f"{original}\n\n{header}\n\n{translated}"
                    status = "Done"
                    scroll_to_translation = True
                    translated_result = translated
                else:
                    short_err = (err or "Unknown error")[:60]
                    result_text = f"{original}\n\n⚠ {short_err}"
                    status = "Failed"
                    scroll_to_translation = True

            def update():
                try:
                    if self._text_view is tv and self._original_text is original:
                        self._set_detected_label(src if src != "auto" else None)
                        tv.setString_(result_text)
                        if scroll_to_translation:
                            try:
                                tv.scrollRangeToVisible_((len(result_text), 0))
                            except Exception:
                                pass
                            try:
                                sv = getattr(self, "_scroll_view", None)
                                if sv is not None:
                                    sv.flashScrollers()
                            except Exception:
                                pass
                        if self._translate_btn:
                            self._translate_btn.setTitle_("✓" if status == "Done" else "Retry")
                            self._translate_btn.setImage_(None)
                            self._translate_btn.setEnabled_(True)
                            self._style_glass_button(self._translate_btn, primary=True, font_size=9.0)
                        if getattr(self, "_lang_popup", None):
                            self._lang_popup.setEnabled_(True)
                            self._style_glass_popup(self._lang_popup)
                        self._translated_text = translated_result
                        if getattr(self, "_replace_btn", None):
                            self._replace_btn.setEnabled_(bool(translated_result))
                            if translated_result:
                                self._replace_btn.setTitle_("Replace")
                            self._style_glass_button(self._replace_btn, primary=False, font_size=9.0)
                finally:
                    self._translating = False

            try:
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(update)
            except Exception:
                update()

        threading.Thread(target=worker, daemon=True).start()

    def replaceClicked_(self, sender):
        """Replace the clipboard slot with the translated text."""
        translated = getattr(self, "_translated_text", None)
        slot = getattr(self, "_slot", None)
        picker = getattr(self, "_picker", None)
        if not translated or slot is None or picker is None:
            return
        store = getattr(picker, "store", None)
        if store is None:
            return

        ok = store.replace_slot_text(int(slot), translated)
        if ok:
            # Also put translation on the system pasteboard
            try:
                pb = NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(translated, NSPasteboardTypeString)
            except Exception:
                pass
            if self._replace_btn:
                self._replace_btn.setTitle_("✓")
                self._replace_btn.setEnabled_(False)
                self._style_glass_button(self._replace_btn, primary=False, font_size=9.0)
            if self._text_view is not None:
                self._text_view.setString_(translated)
            self._original_text = translated
            self._translated_text = None
            # Refresh the main picker so the slot shows the new text
            try:
                picker._rebuild_content()
            except Exception:
                pass
        else:
            if self._replace_btn:
                self._replace_btn.setTitle_("Failed")

    def hide_preview(self):
        self._over_preview = False
        self.orderOut_(None)
        self._slot = None
        self._picker = None
        self._text_view = None
        self._scroll_view = None
        self._original_text = None
        self._translated_text = None
        self._translate_btn = None
        self._replace_btn = None
        self._lang_popup = None
        self._detected_label = None
        self._detected_lang = None
        self._target_lang = "en"
        self._translating = False


class PickerEventMonitor:
    def __init__(self, panel, on_slot, on_cancel, on_toggle_pin, on_delete) -> None:
        self.panel = panel
        self.on_slot = on_slot
        self.on_cancel = on_cancel
        self.on_toggle_pin = on_toggle_pin
        self.on_delete = on_delete
        self._local_key = self._global_key = self._global_mouse = None

    def start(self) -> None:
        def handle_key(event, consume_local=False):
            if event.keyCode() == ESCAPE_KEYCODE:
                self.on_cancel()
                return None
            chars = event.charactersIgnoringModifiers()
            flags = event.modifierFlags()
            option = bool(flags & (1 << 19))
            cmd = bool(flags & (1 << 20))
            if chars == "0":
                self.panel.store.clear()
                self.on_cancel()
                return None
            if chars in SLOT_KEYS:
                slot = key_to_slot(chars)
                if slot is not None:
                    if option:
                        self.on_toggle_pin(slot)
                    elif cmd:
                        self.on_delete(slot)
                    else:
                        self.on_slot(slot)
                return None
            return event if consume_local else None

        self._local_key = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            KEY_DOWN_MASK, lambda e: handle_key(e, True)
        )
        self._global_key = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            KEY_DOWN_MASK, lambda e: handle_key(e, False)
        )
        def _point_in_frame(pt, frame):
            return (
                frame.origin.x <= pt.x <= frame.origin.x + frame.size.width
                and frame.origin.y <= pt.y <= frame.origin.y + frame.size.height
            )

        def handle_mouse(e):
            pt = NSEvent.mouseLocation()
            if _point_in_frame(pt, self.panel.frame()):
                return None
            ql = getattr(self.panel, "_quicklook", None)
            if ql is not None and ql.isVisible() and _point_in_frame(pt, ql.frame()):
                return None
            self.on_cancel()
            return None

        self._global_mouse = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            MOUSE_DOWN_MASK, handle_mouse
        )

    def stop(self) -> None:
        for m in (self._local_key, self._global_key, self._global_mouse):
            if m is not None:
                NSEvent.removeMonitor_(m)
        self._local_key = self._global_key = self._global_mouse = None


class CloseButtonView(NSView):
    def initWithFrame_onClose_(self, frame, on_close):
        self = objc.super(CloseButtonView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.on_close = on_close
        self.setToolTip_("Close (Esc)")
        self.setWantsLayer_(True)
        layer = self.layer()
        layer.setCornerRadius_(frame.size.height / 2.0)
        layer.setMasksToBounds_(True)
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.08).CGColor()
        )
        layer.setBorderWidth_(0.5)
        layer.setBorderColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.22).CGColor()
        )
        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, 0, frame.size.width, frame.size.height)
        )
        label.setStringValue_("✕")
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.systemFontOfSize_weight_(11, 0.4))
        label.setAlignment_(1)  # center
        label.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.75, 1.0))
        self.addSubview_(label)
        self._label = label
        return self

    def mouseEntered_(self, event):
        self.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).CGColor()
        )
        self.layer().setBorderColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.38).CGColor()
        )
        if getattr(self, "_label", None):
            self._label.setTextColor_(NSColor.whiteColor())

    def mouseExited_(self, event):
        self.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.08).CGColor()
        )
        self.layer().setBorderColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.22).CGColor()
        )
        if getattr(self, "_label", None):
            self._label.setTextColor_(
                NSColor.colorWithCalibratedWhite_alpha_(0.75, 1.0)
            )

    def mouseDown_(self, event):
        if self.on_close:
            self.on_close()

    def resetCursorRects(self):
        from AppKit import NSCursor
        self.addCursorRect_cursor_(self.bounds(), NSCursor.pointingHandCursor())


class SlotRowView(NSView):
    def initWithFrame_slotData_onSelect_onTogglePin_onDelete_onHover_(
        self, frame, slot_data, on_select, on_toggle_pin, on_delete, on_hover
    ):
        self = objc.super(SlotRowView, self).initWithFrame_(frame)
        if self is None:
            return None

        self.slot = slot_data["slot"]
        self.slot_data = slot_data
        self.full_text = slot_data.get("text", "")
        self.pinned = slot_data.get("pinned", False)
        self.kind = slot_data.get("kind", "text")
        self.has_style = slot_data.get("has_style", False)
        self.sensitive = slot_data.get("sensitive", False)
        self.on_select = on_select
        self.on_toggle_pin = on_toggle_pin
        self.on_delete = on_delete
        self.on_hover = on_hover
        self.image_hash = slot_data.get("image_hash")

        if self.sensitive:
            self._normal_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.55, 0.18, 0.18, 0.32
            )
            self._hover_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.65, 0.22, 0.22, 0.45
            )
            self._border_normal = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1.0, 0.45, 0.45, 0.28
            )
            self._border_hover = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1.0, 0.55, 0.55, 0.40
            )
        else:
            # Liquid-glass slot: translucent white fill that sits on HUD vibrancy
            base = 0.14 if self.pinned else (0.12 if self.slot == 1 else 0.08)
            hover = base + 0.10
            self._normal_color = NSColor.colorWithCalibratedWhite_alpha_(1.0, base)
            self._hover_color = NSColor.colorWithCalibratedWhite_alpha_(1.0, hover)
            self._border_normal = NSColor.colorWithCalibratedWhite_alpha_(
                1.0, 0.22 if self.pinned else 0.14
            )
            self._border_hover = NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.32)

        self.setWantsLayer_(True)
        layer = self.layer()
        layer.setBackgroundColor_(self._normal_color.CGColor())
        layer.setCornerRadius_(10.0)
        layer.setMasksToBounds_(True)
        layer.setBorderWidth_(0.5)
        layer.setBorderColor_(self._border_normal.CGColor())

        tip_parts = []
        if self.sensitive:
            tip_parts.append("🔒 SENSITIVE – will auto-delete / never saved to disk")
        if self.pinned:
            tip_parts.append("★ Pinned")
        if self.has_style:
            tip_parts.append("🎨 Styled text")
        tip_parts.append(tooltip_text(self.full_text))
        tip = "\n".join(tip_parts)
        self.setToolTip_(tip)

        left_x = 8

        badge = NSTextField.alloc().initWithFrame_(NSMakeRect(left_x, 8, 18, 20))
        badge.setStringValue_(slot_label(self.slot))
        badge.setBezeled_(False)
        badge.setDrawsBackground_(False)
        badge.setEditable_(False)
        badge.setSelectable_(False)
        badge.setFont_(NSFont.boldSystemFontOfSize_(12))
        badge.setTextColor_(NSColor.whiteColor())
        self.addSubview_(badge)
        left_x += 20

        pin = NSTextField.alloc().initWithFrame_(NSMakeRect(left_x, 8, 16, 20))
        pin.setStringValue_("★" if self.pinned else "☆")
        pin.setBezeled_(False)
        pin.setDrawsBackground_(False)
        pin.setEditable_(False)
        pin.setSelectable_(False)
        pin.setFont_(NSFont.systemFontOfSize_(12))
        if self.sensitive:
            pin.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.25, 1.0))
        else:
            pin.setTextColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.84, 0.0, 1.0)
                if self.pinned
                else NSColor.colorWithCalibratedWhite_alpha_(0.40, 1.0)
            )
        self.addSubview_(pin)
        left_x += 18

        preview_size = 22
        if self.sensitive:
            lock = NSTextField.alloc().initWithFrame_(NSMakeRect(left_x, 7, 18, 20))
            lock.setStringValue_("🔒")
            lock.setBezeled_(False)
            lock.setDrawsBackground_(False)
            lock.setEditable_(False)
            lock.setSelectable_(False)
            lock.setFont_(NSFont.systemFontOfSize_(12))
            self.addSubview_(lock)
            left_x += 20
        elif self.kind == "color" and slot_data.get("color"):
            r, g, b, a = slot_data["color"]
            swatch = NSView.alloc().initWithFrame_(
                NSMakeRect(left_x, 7, preview_size, preview_size)
            )
            swatch.setWantsLayer_(True)
            swatch.layer().setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a).CGColor()
            )
            swatch.layer().setCornerRadius_(4.0)
            swatch.layer().setBorderWidth_(1.0)
            swatch.layer().setBorderColor_(
                NSColor.colorWithCalibratedWhite_alpha_(0.5, 1.0).CGColor()
            )
            self.addSubview_(swatch)
            left_x += preview_size + 6
        elif self.kind == "image" and self.image_hash:
            path = CACHE_DIR / f"{self.image_hash}.png"
            if path.exists():
                img = png_bytes_to_image(path.read_bytes())
                if img:
                    thumb = thumbnail(img, max_side=preview_size)
                    iv = NSImageView.alloc().initWithFrame_(
                        NSMakeRect(left_x, 7, preview_size, preview_size)
                    )
                    iv.setImage_(thumb)
                    iv.setImageScaling_(3)
                    iv.setWantsLayer_(True)
                    iv.layer().setCornerRadius_(4.0)
                    iv.layer().setMasksToBounds_(True)
                    self.addSubview_(iv)
                    left_x += preview_size + 6
        elif self.kind == "url":
            icon = NSTextField.alloc().initWithFrame_(NSMakeRect(left_x, 7, 16, 20))
            icon.setStringValue_("🔗")
            icon.setBezeled_(False)
            icon.setDrawsBackground_(False)
            icon.setEditable_(False)
            icon.setSelectable_(False)
            icon.setFont_(NSFont.systemFontOfSize_(11))
            self.addSubview_(icon)
            left_x += 18
        elif try_local_file_path(self.full_text) is not None:
            # Show Finder-style file icon for local paths
            fp = try_local_file_path(self.full_text)
            try:
                ficon = NSWorkspace.sharedWorkspace().iconForFile_(str(fp))
                if ficon:
                    ficon.setSize_((preview_size, preview_size))
                    iv = NSImageView.alloc().initWithFrame_(
                        NSMakeRect(left_x, 7, preview_size, preview_size)
                    )
                    iv.setImage_(ficon)
                    iv.setImageScaling_(3)
                    iv.setWantsLayer_(True)
                    iv.layer().setCornerRadius_(4.0)
                    iv.layer().setMasksToBounds_(True)
                    self.addSubview_(iv)
                    left_x += preview_size + 6
                else:
                    raise RuntimeError("no icon")
            except Exception:
                icon = NSTextField.alloc().initWithFrame_(NSMakeRect(left_x, 7, 16, 20))
                icon.setStringValue_("📄")
                icon.setBezeled_(False)
                icon.setDrawsBackground_(False)
                icon.setEditable_(False)
                icon.setSelectable_(False)
                icon.setFont_(NSFont.systemFontOfSize_(11))
                self.addSubview_(icon)
                left_x += 18
        elif self.has_style:
            style_icon = NSTextField.alloc().initWithFrame_(NSMakeRect(left_x, 7, 16, 20))
            style_icon.setStringValue_("𝗔")
            style_icon.setBezeled_(False)
            style_icon.setDrawsBackground_(False)
            style_icon.setEditable_(False)
            style_icon.setSelectable_(False)
            style_icon.setFont_(NSFont.systemFontOfSize_(12))
            style_icon.setTextColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.4, 0.75, 1.0, 1.0)
            )
            self.addSubview_(style_icon)
            left_x += 18

        label_text = self._make_label_text(slot_data)
        label_width = frame.size.width - left_x - 36
        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(left_x, 7, label_width, 22)
        )
        label.setStringValue_(label_text)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.systemFontOfSize_(12))
        label.setTextColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.55, 1.0)
            if not self.full_text and self.kind != "image"
            else NSColor.colorWithCalibratedWhite_alpha_(0.92, 1.0)
        )
        label.setLineBreakMode_(4)
        label.setToolTip_(tip)
        self.addSubview_(label)

        del_btn = NSTextField.alloc().initWithFrame_(
            NSMakeRect(frame.size.width - 28, 6, 22, 22)
        )
        del_btn.setStringValue_("🗑️")
        del_btn.setBezeled_(False)
        del_btn.setDrawsBackground_(False)
        del_btn.setEditable_(False)
        del_btn.setSelectable_(False)
        del_btn.setFont_(NSFont.systemFontOfSize_(12))
        del_btn.setToolTip_("Permanently delete (⌘+number)")
        self.addSubview_(del_btn)

        tracking = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            NSTrackingMouseEnteredAndExited
            | NSTrackingActiveAlways
            | NSTrackingInVisibleRect,
            self, None,
        )
        self.addTrackingArea_(tracking)
        return self

    def _make_label_text(self, slot_data: dict) -> str:
        if self.sensitive:
            return "••••••••  (sensitive – click to paste once)"
        if self.kind == "image":
            return slot_data.get("text") or "[Image]"
        if self.kind == "color":
            return self.full_text
        if self.kind == "url":
            title = slot_data.get("url_title")
            host = url_host(self.full_text)
            return f"{title}  ·  {host}" if title else host
        fp = try_local_file_path(self.full_text)
        if fp is not None:
            ext = fp.suffix.lower()
            if ext in IMAGE_PREVIEW_EXTS:
                return f"🖼 {fp.name}"
            if ext in TEXT_PREVIEW_EXTS:
                return f"📄 {fp.name}"
            return f"📁 {fp.name}"
        if not self.full_text:
            return "(empty)"
        prefix = "🎨 " if self.has_style else ""
        return prefix + truncate(self.full_text)

    def mouseEntered_(self, event):
        layer = self.layer()
        layer.setBackgroundColor_(self._hover_color.CGColor())
        if getattr(self, "_border_hover", None) is not None:
            layer.setBorderColor_(self._border_hover.CGColor())
        if self.on_hover:
            self.on_hover(self.slot, self.slot_data, True)

    def mouseExited_(self, event):
        layer = self.layer()
        layer.setBackgroundColor_(self._normal_color.CGColor())
        if getattr(self, "_border_normal", None) is not None:
            layer.setBorderColor_(self._border_normal.CGColor())
        if self.on_hover:
            self.on_hover(self.slot, self.slot_data, False)

    def mouseDown_(self, event):
        loc = self.convertPoint_fromView_(event.locationInWindow(), None)
        if loc.x > self.bounds().size.width - 32:
            if self.on_delete:
                self.on_delete(self.slot)
            return
        if loc.x < 48 and (self.full_text or self.image_hash) and self.on_toggle_pin:
            if not self.sensitive:
                self.on_toggle_pin(self.slot)
            return
        if (self.full_text or self.image_hash) and self.on_select:
            self.on_select(self.slot)

    def resetCursorRects(self):
        from AppKit import NSCursor
        self.addCursorRect_cursor_(self.bounds(), NSCursor.pointingHandCursor())


class ClipboardPickerPanel(NSPanel):
    def initWithStore_frame_(self, store, frame):
        self = objc.super(ClipboardPickerPanel, self).initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        if self is None:
            return None
        self.store = store
        self._monitor = None
        self._closed = False
        self._hovered_slot = None
        self._hovered_data = None
        self._quicklook = None
        self.setLevel_(25)
        self.setOpaque_(False)
        self.setBackgroundColor_(NSColor.clearColor())
        self.setHasShadow_(True)
        self.setHidesOnDeactivate_(False)
        self.setCanHide_(False)
        self.setCollectionBehavior_(128 | 256)
        self._build_content()
        return self

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return False

    def keyDown_(self, event):
        if event.keyCode() == ESCAPE_KEYCODE:
            self.close_panel()
            return
        chars = event.charactersIgnoringModifiers()
        flags = event.modifierFlags()
        option = bool(flags & (1 << 19))
        cmd = bool(flags & (1 << 20))
        if chars == "0":
            self.store.clear()
            self.close_panel()
            return
        if chars in SLOT_KEYS:
            slot = key_to_slot(chars)
            if slot is not None:
                if option:
                    self.toggle_pin(slot)
                elif cmd:
                    self.delete_slot(slot)
                else:
                    self.select_slot(slot)
            return
        objc.super(ClipboardPickerPanel, self).keyDown_(event)

    def select_slot(self, slot):
        s = self.store.get_slot(slot)
        if not s.get("text") and not s.get("image_hash"):
            return
        self.close_panel()
        time.sleep(0.10)
        self.store.paste_slot(slot)

    def toggle_pin(self, slot):
        self.store.toggle_pin(slot)
        self._rebuild_content()

    def delete_slot(self, slot):
        self.store.delete_slot(slot)
        self._rebuild_content()

    def close_panel(self):
        global _active_panel
        if self._closed:
            return
        self._closed = True
        self._cancel_hide_quicklook()
        self._hide_quicklook()
        if self._monitor:
            self._monitor.stop()
            self._monitor = None
        if _active_panel is self:
            _active_panel = None
        self.orderOut_(None)
        NSApplication.sharedApplication().deactivate()

    def start_monitoring(self):
        self._monitor = PickerEventMonitor(
            self, self.select_slot, self.close_panel,
            self.toggle_pin, self.delete_slot,
        )
        self._monitor.start()

    def on_slot_hover(self, slot, slot_data, entered):
        """Show floating Quick Look on hover; keep open when pointer moves onto the preview."""
        if entered:
            self._cancel_hide_quicklook()
            self._hovered_slot = slot
            self._hovered_data = slot_data
            self._show_quicklook(slot_data)
        else:
            if self._hovered_slot == slot:
                self._hovered_slot = None
                self._hovered_data = None
                # delay hide so the user can move into the preview window
                self._schedule_hide_quicklook()

    def on_quicklook_hover(self, entered):
        """Called when the pointer enters/leaves the floating preview itself."""
        if entered:
            self._cancel_hide_quicklook()
        else:
            if self._hovered_slot is None:
                self._schedule_hide_quicklook()

    def _schedule_hide_quicklook(self):
        self._cancel_hide_quicklook()
        from Foundation import NSTimer
        self._ql_hide_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.18, self, "hideQuickLookIfIdle:", None, False
        )

    def _cancel_hide_quicklook(self):
        t = getattr(self, "_ql_hide_timer", None)
        if t is not None:
            try:
                t.invalidate()
            except Exception:
                pass
            self._ql_hide_timer = None

    def hideQuickLookIfIdle_(self, _timer):
        self._ql_hide_timer = None
        if self._hovered_slot is not None:
            return
        if self._quicklook is not None and self._quicklook.is_mouse_over():
            return
        self._hide_quicklook()

    def _slot_screen_rect(self, slot_num):
        """Return the screen rect of the SlotRowView for slot_num, or None."""
        try:
            content = self.contentView()
            if content is None:
                return None
            target = None
            for sub in content.subviews():
                if getattr(sub, "slot", None) == slot_num:
                    target = sub
                    break
            if target is None:
                return None
            # sub.frame is in content-view coords → window → screen
            win_rect = content.convertRect_toView_(target.frame(), None)
            return self.convertRectToScreen_(win_rect)
        except Exception:
            return None

    def _show_quicklook(self, slot_data):
        if self._closed:
            return
        if not slot_data.get("text") and not slot_data.get("image_hash") and slot_data.get("kind") != "color":
            return
        if self._quicklook is None:
            self._quicklook = QuickLookPreviewPanel.alloc().init()
        # Pass the real on-screen rect of the hovered row so QL can align to it
        self._quicklook._anchor_rect = self._slot_screen_rect(slot_data.get("slot"))
        self._quicklook.showForSlot_nearPanel_(slot_data, self)

    def _hide_quicklook(self):
        self._cancel_hide_quicklook()
        if self._quicklook is not None:
            self._quicklook.hide_preview()

    def _rebuild_content(self):
        self._hide_quicklook()
        self._hovered_slot = None
        self._hovered_data = None
        self._build_content()
        self.orderFrontRegardless()
        self.makeKeyAndOrderFront_(None)

    def _build_content(self):
        content = NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT)
        )
        content.setMaterial_(NSVisualEffectMaterialHUDWindow)
        content.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        content.setState_(NSVisualEffectStateActive)
        content.setWantsLayer_(True)
        content.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))

        radius = 12.0
        try:
            from AppKit import NSBezierPath, NSEdgeInsetsMake, NSImageResizingModeStretch
            mask = NSImage.alloc().initWithSize_((radius * 2, radius * 2))
            mask.lockFocus()
            NSColor.blackColor().set()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0, 0, radius * 2, radius * 2), radius, radius
            )
            path.fill()
            mask.unlockFocus()
            mask.setCapInsets_(NSEdgeInsetsMake(radius, radius, radius, radius))
            mask.setResizingMode_(NSImageResizingModeStretch)
            content.setMaskImage_(mask)
        except Exception:
            pass

        layer = content.layer()
        if layer is not None:
            layer.setCornerRadius_(radius)
            layer.setMasksToBounds_(True)
            # Soft liquid-glass rim
            layer.setBorderWidth_(0.6)
            layer.setBorderColor_(
                NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).CGColor()
            )

        title_y = PANEL_HEIGHT - TITLE_TOP_INSET - 22
        title = NSTextField.alloc().initWithFrame_(
            NSMakeRect(14, title_y, PANEL_WIDTH - 54, 22)
        )
        title.setStringValue_("PasteDeck")
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setSelectable_(False)
        title.setAlignment_(0)  # NSTextAlignmentLeft
        title.setFont_(NSFont.boldSystemFontOfSize_(17))
        title.setTextColor_(NSColor.whiteColor())
        content.addSubview_(title)

        subtitle_y = title_y - 16
        subtitle = NSTextField.alloc().initWithFrame_(
            NSMakeRect(14, subtitle_y, PANEL_WIDTH - 54, 14)
        )
        subtitle.setStringValue_("1–9 paste  ·  ⌥ pin  ·  ⌘ del  ·  hover peek  ·  0 clear")
        subtitle.setBezeled_(False)
        subtitle.setDrawsBackground_(False)
        subtitle.setEditable_(False)
        subtitle.setSelectable_(False)
        subtitle.setAlignment_(0)  # left
        subtitle.setFont_(NSFont.systemFontOfSize_(10))
        subtitle.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.65, 1.0))
        content.addSubview_(subtitle)

        close_btn = CloseButtonView.alloc().initWithFrame_onClose_(
            NSMakeRect(PANEL_WIDTH - 36, PANEL_HEIGHT - TITLE_TOP_INSET - 22, 24, 24),
            self.close_panel,
        )
        tracking = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            close_btn.bounds(),
            NSTrackingMouseEnteredAndExited
            | NSTrackingActiveAlways
            | NSTrackingInVisibleRect,
            close_btn, None,
        )
        close_btn.addTrackingArea_(tracking)
        content.addSubview_(close_btn)

        content_top = PANEL_HEIGHT - TITLE_TOP_INSET - TITLE_HEIGHT - 4
        for i, slot_data in enumerate(self.store.snapshot()):
            row_y = content_top - (i + 1) * SLOT_HEIGHT - i * SLOT_GAP
            row = SlotRowView.alloc().initWithFrame_slotData_onSelect_onTogglePin_onDelete_onHover_(
                NSMakeRect(10, row_y, PANEL_WIDTH - 20, SLOT_HEIGHT),
                slot_data,
                self.select_slot,
                self.toggle_pin,
                self.delete_slot,
                self.on_slot_hover,
            )
            content.addSubview_(row)
        self.setContentView_(content)


objc.registerMetaDataForSelector(
    b"CloseButtonView", b"initWithFrame:onClose:",
    {"arguments": {2 + 1: {"type": b"@"}}},
)
objc.registerMetaDataForSelector(
    b"SlotRowView",
    b"initWithFrame:slotData:onSelect:onTogglePin:onDelete:onHover:",
    {"arguments": {
        2 + 1: {"type": b"@"},
        3 + 1: {"type": b"@"},
        4 + 1: {"type": b"@"},
        5 + 1: {"type": b"@"},
        6 + 1: {"type": b"@"},
    }},
)
objc.registerMetaDataForSelector(
    b"QuickLookHoverView", b"initWithFrame:onHover:",
    {"arguments": {2 + 1: {"type": b"@"}}},
)
objc.registerMetaDataForSelector(
    b"SettingsWindowController", b"initWithApp:",
    {"arguments": {2 + 1: {"type": b"@"}}},
)


def close_picker_panel() -> None:
    global _active_panel
    if _active_panel is not None:
        _active_panel.close_panel()


def toggle_picker_panel(store: ClipboardStore) -> None:
    global _active_panel
    if _active_panel is not None:
        _active_panel.close_panel()
        return
    open_picker_panel(store)


def open_picker_panel(store: ClipboardStore) -> None:
    remember_frontmost_app()
    global _active_panel
    if _active_panel is not None:
        _active_panel.close_panel()
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    x, y = get_cursor_point()
    screen = NSScreen.mainScreen().visibleFrame()
    panel_x = max(screen.origin.x + 8,
                  min(x + 12, screen.origin.x + screen.size.width - PANEL_WIDTH - 8))
    panel_y = y - PANEL_HEIGHT - 12
    if panel_y < screen.origin.y + 8:
        panel_y = y + 12
    if panel_y + PANEL_HEIGHT > screen.origin.y + screen.size.height - 8:
        panel_y = screen.origin.y + screen.size.height - PANEL_HEIGHT - 8
    try:
        panel = ClipboardPickerPanel.alloc().initWithStore_frame_(
            store, NSMakeRect(panel_x, panel_y, PANEL_WIDTH, PANEL_HEIGHT)
        )
        if panel is None:
            return
        _active_panel = panel
        panel.start_monitoring()
        panel.orderFrontRegardless()
        panel.makeKeyAndOrderFront_(None)
    except Exception as e:
        print(f"[DEBUG] panel error: {e}")


# ---------------------------------------------------------------------------
# Settings window
# ---------------------------------------------------------------------------
class SettingsWindowController(NSObject):
    """Lightweight controller that keeps the settings window alive and wired."""

    def initWithApp_(self, app):
        self = objc.super(SettingsWindowController, self).init()
        if self is None:
            return None
        self.app = app
        self.window = None
        self._expire_popup = None
        self._notify_btn = None
        self._expire_label = None
        return self

    def show(self):
        if self.window is not None and self.window.isVisible():
            self.window.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            return

        width, height = 420, 320
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
        )
        frame = NSMakeRect(0, 0, width, height)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("PasteDeck Settings")
        self.window.center()
        self.window.setReleasedWhenClosed_(False)

        content = NSView.alloc().initWithFrame_(frame)
        settings = self.app.store.settings

        # --- header ---
        header = NSTextField.alloc().initWithFrame_(NSMakeRect(20, height - 48, width - 40, 24))
        header.setStringValue_("Preferences")
        header.setBezeled_(False)
        header.setDrawsBackground_(False)
        header.setEditable_(False)
        header.setSelectable_(False)
        header.setFont_(NSFont.boldSystemFontOfSize_(16))
        content.addSubview_(header)

        desc = NSTextField.alloc().initWithFrame_(NSMakeRect(20, height - 70, width - 40, 18))
        desc.setStringValue_("Adjust how PasteDeck handles privacy and feedback.")
        desc.setBezeled_(False)
        desc.setDrawsBackground_(False)
        desc.setEditable_(False)
        desc.setSelectable_(False)
        desc.setFont_(NSFont.systemFontOfSize_(11))
        desc.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(desc)

        # --- sensitive expire ---
        expire_title = NSTextField.alloc().initWithFrame_(NSMakeRect(20, height - 110, 200, 18))
        expire_title.setStringValue_("Sensitive auto-expire")
        expire_title.setBezeled_(False)
        expire_title.setDrawsBackground_(False)
        expire_title.setEditable_(False)
        expire_title.setSelectable_(False)
        expire_title.setFont_(NSFont.systemFontOfSize_(13))
        content.addSubview_(expire_title)

        self._expire_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(20, height - 130, width - 40, 16)
        )
        self._expire_label.setBezeled_(False)
        self._expire_label.setDrawsBackground_(False)
        self._expire_label.setEditable_(False)
        self._expire_label.setSelectable_(False)
        self._expire_label.setFont_(NSFont.systemFontOfSize_(11))
        self._expire_label.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(self._expire_label)

        self._expire_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(20, height - 162, 200, 26), False
        )
        expire_options = [
            (15, "15 seconds"),
            (30, "30 seconds"),
            (45, "45 seconds"),
            (60, "1 minute"),
            (120, "2 minutes"),
            (300, "5 minutes"),
            (0, "Never (manual only)"),
        ]
        current = int(settings.get("sensitive_expire_seconds", DEFAULT_SENSITIVE_EXPIRE_SECONDS))
        selected_idx = 2  # default 45s
        for i, (secs, label) in enumerate(expire_options):
            self._expire_popup.addItemWithTitle_(label)
            self._expire_popup.itemAtIndex_(i).setTag_(secs)
            if secs == current:
                selected_idx = i
        self._expire_popup.selectItemAtIndex_(selected_idx)
        self._expire_popup.setTarget_(self)
        self._expire_popup.setAction_("expireChanged:")
        content.addSubview_(self._expire_popup)
        self._update_expire_label(current)

        # --- notifications ---
        notify_title = NSTextField.alloc().initWithFrame_(NSMakeRect(20, height - 210, 200, 18))
        notify_title.setStringValue_("Notifications")
        notify_title.setBezeled_(False)
        notify_title.setDrawsBackground_(False)
        notify_title.setEditable_(False)
        notify_title.setSelectable_(False)
        notify_title.setFont_(NSFont.systemFontOfSize_(13))
        content.addSubview_(notify_title)

        self._notify_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(20, height - 238, width - 40, 24)
        )
        self._notify_btn.setButtonType_(3)  # NSSwitchButton / NSButtonTypeSwitch
        self._notify_btn.setTitle_("Show notifications for clear actions")
        self._notify_btn.setState_(1 if settings.get("show_notifications", True) else 0)
        self._notify_btn.setTarget_(self)
        self._notify_btn.setAction_("notifyChanged:")
        content.addSubview_(self._notify_btn)

        # --- footer actions ---
        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width - 110, 20, 90, 32))
        save_btn.setTitle_("Save")
        save_btn.setBezelStyle_(1)  # rounded
        save_btn.setKeyEquivalent_("\r")
        save_btn.setTarget_(self)
        save_btn.setAction_("saveSettings:")
        content.addSubview_(save_btn)

        cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width - 200, 20, 80, 32))
        cancel_btn.setTitle_("Cancel")
        cancel_btn.setBezelStyle_(1)
        cancel_btn.setTarget_(self)
        cancel_btn.setAction_("cancelSettings:")
        content.addSubview_(cancel_btn)

        about = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 24, 180, 16))
        about.setStringValue_("PasteDeck  ·  local & private")
        about.setBezeled_(False)
        about.setDrawsBackground_(False)
        about.setEditable_(False)
        about.setSelectable_(False)
        about.setFont_(NSFont.systemFontOfSize_(10))
        about.setTextColor_(NSColor.tertiaryLabelColor())
        content.addSubview_(about)

        self.window.setContentView_(content)
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def _update_expire_label(self, seconds: int):
        if seconds <= 0:
            text = "Sensitive items stay until you clear them."
        elif seconds < 60:
            text = f"Password-manager & high-entropy clips vanish after {seconds}s."
        else:
            mins = seconds // 60
            text = f"Password-manager & high-entropy clips vanish after {mins} min."
        self._expire_label.setStringValue_(text)

    def expireChanged_(self, sender):
        tag = sender.selectedItem().tag()
        self._update_expire_label(int(tag))

    def notifyChanged_(self, sender):
        pass  # state read on save

    def saveSettings_(self, sender):
        secs = int(self._expire_popup.selectedItem().tag())
        notify = bool(self._notify_btn.state())
        self.app.store.settings["sensitive_expire_seconds"] = secs
        self.app.store.settings["show_notifications"] = notify
        save_settings(self.app.store.settings)
        self.window.orderOut_(None)

    def cancelSettings_(self, sender):
        self.window.orderOut_(None)


class MultiClipboardApp(rumps.App):
    def __init__(self, store: ClipboardStore) -> None:
        super().__init__("PasteDeck", title="📋", quit_button=None)
        self.store = store
        self._open_picker_requested = False
        self._settings_controller = None

        def _title_noop(_):
            pass

        header = rumps.MenuItem("PasteDeck", callback=_title_noop)
        try:
            from Foundation import NSAttributedString, NSMutableDictionary
            from AppKit import NSFontAttributeName, NSForegroundColorAttributeName
            attrs = NSMutableDictionary.dictionary()
            attrs.setObject_forKey_(
                NSFont.boldSystemFontOfSize_(15), NSFontAttributeName
            )
            attrs.setObject_forKey_(
                NSColor.whiteColor(), NSForegroundColorAttributeName
            )
            attributed = NSAttributedString.alloc().initWithString_attributes_(
                "PasteDeck", attrs
            )
            ns_item = getattr(header, "_menuitem", None) or getattr(header, "nsmenuitem", None)
            if ns_item is not None:
                ns_item.setAttributedTitle_(attributed)
                ns_item.setEnabled_(True)
        except Exception:
            pass

        self.menu = [
            header,
            None,
            rumps.MenuItem("Open Picker                    ⌘⌥⇧V", callback=self.open_picker),
            None,
            rumps.MenuItem("Clear Unpinned", callback=self.clear_slots),
            rumps.MenuItem("Clear Sensitive Now", callback=self.clear_sensitive),
            rumps.MenuItem("Clear Everything", callback=self.clear_all),
            None,
            rumps.MenuItem("Settings…", callback=self.open_settings),
            None,
            rumps.MenuItem("Quit PasteDeck", callback=self.quit_app),
        ]

        poll = float(store.settings.get("poll_interval", 0.4))
        self._poll_timer = rumps.Timer(self.poll_clipboard, poll)
        self._poll_timer.start()
        self._hotkey_timer = rumps.Timer(self.handle_hotkey_request, 0.05)
        self._hotkey_timer.start()

        @quickHotKey(virtualKey=kVK_ANSI_V, modifierMask=mask(cmdKey, optionKey, shiftKey))
        def on_hotkey() -> None:
            self._open_picker_requested = True
        self._hotkey_handler = on_hotkey

    def _notify(self, subtitle: str, message: str) -> None:
        if self.store.settings.get("show_notifications", True):
            rumps.notification("PasteDeck", subtitle, message)

    def open_picker(self, _=None):
        toggle_picker_panel(self.store)

    def open_settings(self, _=None):
        if self._settings_controller is None:
            self._settings_controller = SettingsWindowController.alloc().initWithApp_(self)
        self._settings_controller.show()

    def clear_slots(self, _=None):
        self.store.clear()
        self._notify("Cleared", "Unpinned slots cleared")

    def clear_sensitive(self, _=None):
        self.store.clear_sensitive()
        self._notify("Privacy", "Sensitive items removed")

    def clear_all(self, _=None):
        self.store.clear_all()
        self._notify("Cleared", "Everything removed")

    def quit_app(self, _=None):
        if _active_panel is not None:
            _active_panel.close_panel()
        rumps.quit_application()

    def poll_clipboard(self, _=None):
        self.store.poll_clipboard()

    def handle_hotkey_request(self, _=None):
        if self._open_picker_requested:
            self._open_picker_requested = False
            toggle_picker_panel(self.store)


def main() -> None:
    print("[DEBUG] Starting PasteDeck …")
    settings = load_settings()
    store = ClipboardStore(settings)
    app = MultiClipboardApp(store)

    threading.Timer(0.5, minimize_terminal_window).start()

    app.run()


if __name__ == "__main__":
    main()