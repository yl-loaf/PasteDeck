#!/usr/bin/env python3
"""Multi-clipboard manager for macOS – rich text, visual previews, pinned clips,
   privacy controls, and Instant In-Line Quick Look (hover a slot to peek).
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
    NSApplication,
    NSBackingStoreBuffered,
    NSBitmapImageRep,
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
    NSRunningApplication,
    NSScreen,
    NSScrollView,
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
    NSWindowStyleMaskBorderless,
    NSWorkspace,
)
from Foundation import NSData
from quickmachotkey import quickHotKey, mask
from quickmachotkey.constants import kVK_ANSI_V, cmdKey, optionKey, shiftKey

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NUM_SLOTS = 9
PREVIEW_LEN = 55
TOOLTIP_LEN = 800
DATA_FILE = Path.home() / ".multi-clipboard.json"
CACHE_DIR = Path.home() / ".multi-clipboard-cache"
CACHE_DIR.mkdir(exist_ok=True)

PANEL_WIDTH = 400
SLOT_HEIGHT = 36
SLOT_GAP = 3
PANEL_PADDING = 10
TITLE_HEIGHT = 24

PANEL_HEIGHT = (
    TITLE_HEIGHT
    + PANEL_PADDING * 2
    + NUM_SLOTS * SLOT_HEIGHT
    + (NUM_SLOTS - 1) * SLOT_GAP
)

SLOT_KEYS = frozenset("1234567890")
KEY_DOWN_MASK = 1 << 10
MOUSE_DOWN_MASK = (1 << 1) | (1 << 3) | (1 << 25)
ESCAPE_KEYCODE = 53
SENSITIVE_EXPIRE_SECONDS = 45

# Quick Look floating preview
QL_MAX_WIDTH = 520
QL_MAX_HEIGHT = 420
QL_MIN_WIDTH = 280
QL_PADDING = 14
QL_IMAGE_MAX = 480

_active_panel = None
_previous_app = None

# ---------------------------------------------------------------------------
# Sensitive-source detection (tightened – no browsers, URLs excluded)
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
    """Strict heuristic – never flags URLs or normal text."""
    if not text:
        return False
    text = text.strip()
    length = len(text)

    # Explicitly reject URLs
    if text.lower().startswith(("http://", "https://", "ftp://", "www.")):
        return False
    if is_url(text):
        return False

    if length < 12 or length > 128:
        return False
    if any(c.isspace() for c in text):
        return False

    lower = text.lower()
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
# Helpers
# ---------------------------------------------------------------------------
def remember_frontmost_app():
    global _previous_app
    try:
        current = NSWorkspace.sharedWorkspace().frontmostApplication()
        our = NSRunningApplication.currentApplication().bundleIdentifier()
        if current and current.bundleIdentifier() != our:
            _previous_app = current
        else:
            # Prefer the most recent non-hidden regular app
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
            # Force activation even if the process is still around
            _previous_app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
            time.sleep(0.05)  # tiny settle
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
    # Give the target app more time after long uptime / sleep
    time.sleep(0.45)

    try:
        # Preferred: pure CGEvent (no System Events dependency)
        source = Quartz.CGEventSourceCreate(0)  # kCGEventSourceStateHIDSystemState
        key_down = CGEventCreateKeyboardEvent(source, 9, True)   # 'v' = 9
        key_up   = CGEventCreateKeyboardEvent(source, 9, False)
        CGEventSetFlags(key_down, kCGEventFlagMaskCommand)
        CGEventSetFlags(key_up,   kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, key_down)
        CGEventPost(kCGHIDEventTap, key_up)
    except Exception:
        # Fallback to AppleScript only if CGEvent fails
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
        
        # List of possible terminal bundle IDs
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
                        # Hide the app, which minimizes its windows
                        app.hide()
                        print(f"[DEBUG] Minimized {bundle_id}")
                        return
                    except Exception as e:
                        print(f"[DEBUG] Error hiding {bundle_id}: {e}")
        
        print("[DEBUG] No terminal application found to minimize.")
    except Exception as e:
        print(f"[DEBUG] Failed to minimize terminal: {e}")

# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class ClipboardStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: list[dict] = [self._empty_slot() for _ in range(NUM_SLOTS)]
        self._ignore_next_change = False
        self._last_change_count = NSPasteboard.generalPasteboard().changeCount()
        self._title_cache: dict[str, str] = {}
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
            if SENSITIVE_EXPIRE_SECONDS <= 0:
                continue
            now = time.time()
            changed = False
            with self._lock:
                for s in self._slots:
                    if (s.get("sensitive") and s.get("created_at")
                            and now - s["created_at"] > SENSITIVE_EXPIRE_SECONDS):
                        s.update(self._empty_slot())
                        changed = True
            if changed:
                self.save()

    def _fetch_title_async(self, url: str, slot_index: int) -> None:
        def worker():
            try:
                import urllib.request
                req = urllib.request.Request(
                    url, headers={"User-Agent": "MultiClipboard/1.0"}
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
        return self

    def showForSlot_nearPanel_(self, slot_data, picker_panel):
        """Build content for slot_data and position next to the picker.
        Selector: showForSlot:nearPanel:  (2 args after self).
        """
        self._slot = slot_data.get("slot")
        self._picker = picker_panel
        self._over_preview = False
        kind = slot_data.get("kind", "text")
        text = slot_data.get("text") or ""
        sensitive = slot_data.get("sensitive", False)
        image_hash = slot_data.get("image_hash")

        radius = 10.0
        # Content container with vibrancy + clean rounded mask (no black rim)
        content = QuickLookHoverView.alloc().initWithFrame_onHover_(
            NSMakeRect(0, 0, QL_MIN_WIDTH, 120),
            self._on_preview_hover,
        )
        content.setMaterial_(NSVisualEffectMaterialHUDWindow)
        content.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        content.setState_(NSVisualEffectStateActive)
        content.setWantsLayer_(True)

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
            layer.setBorderWidth_(0.0)
            layer.setBorderColor_(NSColor.clearColor().CGColor())

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
            # Long text / JSON / URL – readable expanded popup
            display = text
            if not display and kind != "image":
                display = "(empty)"
            # Pretty-print JSON when possible
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

        # Position to the right of the picker (or left if no room)
        pf = picker_panel.frame()
        screen = NSScreen.mainScreen().visibleFrame()
        ql_x = pf.origin.x + pf.size.width + 10
        if ql_x + width > screen.origin.x + screen.size.width - 8:
            ql_x = pf.origin.x - width - 10
        ql_y = pf.origin.y + pf.size.height - height
        # Keep on screen vertically
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

    def _add_text_preview(self, content, text: str):
        """Add a scrollable text view; return (width, height)."""
        # Cap display length for sanity
        max_chars = 12000
        if len(text) > max_chars:
            text = text[:max_chars] + "\n… (truncated)"

        # Estimate size
        lines = text.count("\n") + 1
        # Prefer wider for long lines
        longest = max((len(ln) for ln in text.splitlines()), default=20)
        est_w = min(QL_MAX_WIDTH, max(QL_MIN_WIDTH, min(longest * 7 + 2 * QL_PADDING, QL_MAX_WIDTH)))
        est_h = min(QL_MAX_HEIGHT, max(100, min(lines * 18 + 2 * QL_PADDING, QL_MAX_HEIGHT)))

        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(QL_PADDING, QL_PADDING, est_w - 2 * QL_PADDING, est_h - 2 * QL_PADDING)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(0)  # no border
        scroll.setDrawsBackground_(False)

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
        return est_w, est_h

    def hide_preview(self):
        self._over_preview = False
        self.orderOut_(None)
        self._slot = None
        self._picker = None


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
        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, 0, frame.size.width, frame.size.height)
        )
        label.setStringValue_("✕")
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setFont_(NSFont.systemFontOfSize_(14))
        label.setAlignment_(2)
        label.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.7, 1.0))
        self.addSubview_(label)
        return self

    def mouseEntered_(self, event):
        if self.subviews():
            self.subviews()[0].setTextColor_(NSColor.whiteColor())

    def mouseExited_(self, event):
        if self.subviews():
            self.subviews()[0].setTextColor_(
                NSColor.colorWithCalibratedWhite_alpha_(0.7, 1.0)
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
                0.35, 0.12, 0.12, 1.0
            )
            self._hover_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.45, 0.16, 0.16, 1.0
            )
        else:
            base = 0.20 if self.pinned else (0.18 if self.slot == 1 else 0.14)
            self._normal_color = NSColor.colorWithCalibratedWhite_alpha_(base, 1.0)
            self._hover_color = NSColor.colorWithCalibratedWhite_alpha_(base + 0.10, 1.0)

        self.setWantsLayer_(True)
        self.layer().setBackgroundColor_(self._normal_color.CGColor())
        self.layer().setCornerRadius_(8.0)

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
        if not self.full_text:
            return "(empty)"
        prefix = "🎨 " if self.has_style else ""
        return prefix + truncate(self.full_text)

    def mouseEntered_(self, event):
        self.layer().setBackgroundColor_(self._hover_color.CGColor())
        if self.on_hover:
            self.on_hover(self.slot, self.slot_data, True)

    def mouseExited_(self, event):
        self.layer().setBackgroundColor_(self._normal_color.CGColor())
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
                # Delay hide so the user can move into the preview window
                self._schedule_hide_quicklook()

    def on_quicklook_hover(self, entered):
        """Called when the pointer enters/leaves the floating preview itself."""
        if entered:
            self._cancel_hide_quicklook()
        else:
            # Left the preview – hide unless still over a slot row
            if self._hovered_slot is None:
                self._schedule_hide_quicklook()

    def _schedule_hide_quicklook(self):
        self._cancel_hide_quicklook()
        # NSTimer fires on the main run-loop (AppKit-safe)
        from Foundation import NSTimer
        self._ql_hide_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self, "hideQuickLookIfIdle:", None, False
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
        # Still over a slot or the preview? Keep it.
        if self._hovered_slot is not None:
            return
        if self._quicklook is not None and self._quicklook.is_mouse_over():
            return
        self._hide_quicklook()

    def _show_quicklook(self, slot_data):
        if self._closed:
            return
        # Skip empty slots
        if not slot_data.get("text") and not slot_data.get("image_hash") and slot_data.get("kind") != "color":
            return
        if self._quicklook is None:
            self._quicklook = QuickLookPreviewPanel.alloc().init()
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
        # Native macOS vibrancy / acrylic blur (Spotlight / Raycast style)
        content = NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT)
        )
        content.setMaterial_(NSVisualEffectMaterialHUDWindow)
        content.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        content.setState_(NSVisualEffectStateActive)
        content.setWantsLayer_(True)

        radius = 12.0
        # Prefer maskImage for clean rounded corners (no black rim).
        # Falls back to layer cornerRadius if anything goes wrong.
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
            layer.setBorderWidth_(0.0)
            layer.setBorderColor_(NSColor.clearColor().CGColor())

        title = NSTextField.alloc().initWithFrame_(
            NSMakeRect(14, PANEL_HEIGHT - TITLE_HEIGHT - 6, PANEL_WIDTH - 50, 22)
        )
        title.setStringValue_("1–9 paste • ⌥ pin • ⌘ del • hover peek • 0 clear • Esc")
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setSelectable_(False)
        title.setFont_(NSFont.boldSystemFontOfSize_(11))
        title.setTextColor_(NSColor.whiteColor())
        content.addSubview_(title)

        close_btn = CloseButtonView.alloc().initWithFrame_onClose_(
            NSMakeRect(PANEL_WIDTH - 34, PANEL_HEIGHT - 30, 24, 24),
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

        content_top = PANEL_HEIGHT - TITLE_HEIGHT - PANEL_PADDING
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


class MultiClipboardApp(rumps.App):
    def __init__(self, store: ClipboardStore) -> None:
        super().__init__("Multi-Clipboard", title="📋", quit_button=None)
        self.store = store
        self._open_picker_requested = False

        self.menu = [
            rumps.MenuItem("Open Picker (⌘⌥⇧V)", callback=self.open_picker),
            None,
            rumps.MenuItem("Clear Unpinned", callback=self.clear_slots),
            rumps.MenuItem("Clear Sensitive Now", callback=self.clear_sensitive),
            rumps.MenuItem("Clear Everything", callback=self.clear_all),
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        self._poll_timer = rumps.Timer(self.poll_clipboard, 0.4)
        self._poll_timer.start()
        self._hotkey_timer = rumps.Timer(self.handle_hotkey_request, 0.05)
        self._hotkey_timer.start()

        @quickHotKey(virtualKey=kVK_ANSI_V, modifierMask=mask(cmdKey, optionKey, shiftKey))
        def on_hotkey() -> None:
            self._open_picker_requested = True
        self._hotkey_handler = on_hotkey

    def open_picker(self, _=None):
        toggle_picker_panel(self.store)

    def clear_slots(self, _=None):
        self.store.clear()
        rumps.notification("Multi-Clipboard", "Cleared", "Unpinned slots cleared")

    def clear_sensitive(self, _=None):
        self.store.clear_sensitive()
        rumps.notification("Multi-Clipboard", "Privacy", "Sensitive items removed")

    def clear_all(self, _=None):
        self.store.clear_all()
        rumps.notification("Multi-Clipboard", "Cleared", "Everything removed")

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
    print("[DEBUG] Starting Multi-Clipboard (pre-network version) …")
    store = ClipboardStore()
    app = MultiClipboardApp(store)
    
    # Minimize terminal window at startup
    threading.Timer(0.5, minimize_terminal_window).start()
    
    app.run()


if __name__ == "__main__":
    main()