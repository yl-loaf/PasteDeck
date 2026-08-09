# 📋 PasteDeck

> A lightweight, privacy-focused multi-clipboard manager for macOS.

**PasteDeck** expands your macOS clipboard into a 9-slot power tool that lives in the menu bar. Built with Python and native AppKit APIs, it captures text, rich formatting, images, URLs, file paths, and hex colors—with instant hotkey access, hover Quick Look, in-panel translation, and liquid-glass UI.

---

## ✨ Key Features

### Core clipboard
* **9 persistent slots** — history rolls forward; pinned items stay put.
* **Global hotkey picker** — press **⌥⇧⌘V** anywhere to open a floating panel at the cursor.
* **Rich content support**
  * **Images** — screenshots and image clips with thumbnails in the picker.
  * **Hex colors** — automatic color swatches (e.g. `#676767`, `#FF007F`).
  * **URLs** — host extraction and optional page title.
  * **Rich text** — RTF/HTML formatting retained across apps.
  * **Local file paths** — when clipboard text is a path or `file://` URL, slots and Quick Look show rich previews.
* **Slot controls**
  * `1`–`9` paste a slot
  * `⌥ + slot` pin / unpin
  * `⌘ + slot` delete a slot
  * `0` clear all unpinned items
  * `Esc` dismiss

### Instant In-Line Quick Look
* Hover any slot to peek without pressing Space.
* Floating **QuickLookPreviewPanel** shows:
  * Full image previews
  * Expanded text / JSON
  * Large color swatches
  * File-path previews (images, code/text, Finder icon + size)
* Preview is positioned beside the picker, aligned with the hovered slot.

### File-path previews
When clipboard content is a local path or `file://` URL to an existing file:
* **Images** (`.png`, `.jpg`, `.gif`, `.webp`, `.heic`, …) — thumbnail / full preview
* **Text & code** (`.md`, `.py`, `.js`, `.ts`, `.css`, `.html`, `.xml`, `.yaml`, `.csv`, `.log`, and more) — readable content preview with configurable size limit
* **Other files** — Finder icon + file size
* Toggleable in Settings (`enable_file_previews`)

### Translate (inside Quick Look)
* Language dropdown (common targets) + **Translate** + **Replace**.
* **Detected-language chip** — Apple NaturalLanguage framework detects the source language asynchronously on open and refreshes after translate.
* Engines: Google Translate (primary) and MyMemory (fallback), with SSL handling via certifi.
* Result appears in-panel with a smooth cross-fade transition and auto-scroll.
* **Replace** writes the translation back into the clipboard slot *and* the system pasteboard.
* Can be disabled in Settings.

### Privacy & sensitive data
* Optional detection of copies from password managers (1Password, Bitwarden, LastPass, Keychain, etc.) and high-entropy secret patterns.
* **Off by default** (`detect_sensitive: false`) — enable in Settings if desired.
* When enabled, sensitive items are flagged 🔒, kept **only in RAM** (never written to disk), and auto-expire (default **45 seconds**, configurable).
* Regular history stored locally at `~/.multi-clipboard.json`; settings at `~/.pastedeck-settings.json`.

### Settings
Menu bar → **Settings…** opens a native window with:

| Group | Options |
|-------|---------|
| **Privacy** | Detect sensitive data, sensitive auto-expire interval |
| **Previews** | File-path previews, code/text preview max bytes (0 = no truncation within safety cap) |
| **Behaviour** | Enable Translate, show slot numbers, play sound on paste, show notifications |

Settings are persisted to `~/.pastedeck-settings.json` and applied at runtime.

### UI
* **Liquid-glass** design throughout:
  * Translucent glass slot rows with soft border and hover brighten
  * Glass pills for Translate / Replace, language popup, close button, and detected-language chip
  * Soft rim on picker and Quick Look panels
* Menu bar dropdown with refined **PasteDeck** title header.
* Picker panel titled **PasteDeck** with shortcut subtitle.

---

## Installation & Setup

### Prerequisites
* **macOS** 10.15 (Catalina) or newer
* **Python** 3.10+ (3.11+ recommended)

### Install dependencies
```bash
cd /path/to/PasteDeck
chmod +x install_pastedeck.sh
./install_pastedeck.sh
```

If the script does not run, use:
```bash
sh /path/to/install_pastedeck.sh
```
(type `sh `, then drag the file into Terminal.)

You can also install dependencies manually:
```bash
pip3 install -r requirements.txt
```

**Dependencies:** `rumps`, `quickmachotkey`, `pyobjc-core`, `pyobjc-framework-Cocoa`, `pyobjc-framework-Quartz`, `pyobjc-framework-NaturalLanguage`, `certifi`.

### Run (daily use)
Double-click **`run_pastedeck.command`**, or from Terminal:
```bash
./run_pastedeck.command
```
or
```bash
python3 PasteDeck.py
```

The installer script installs dependencies only. For a standalone app bundle, use your preferred packaging tool (e.g. py2app / PyInstaller) with the included `icon.icns` and `PasteDeck.spec` as a starting point.

---

## Privacy & Security

* **Local-first** — clipboard history (except sensitive items) lives only on your machine under `~/.multi-clipboard.json`.
* **Sensitive handling** — when detection is enabled, passwords/tokens stay in memory, auto-delete after the configured timeout, and are never persisted.
* **Network** — used only when you explicitly press **Translate** (Google / MyMemory). No background telemetry or sync.
* **License** — MIT. See [LICENSE](LICENSE).

---

## Keyboard reference

| Key | Action |
|-----|--------|
| ⌥⇧⌘V | Open picker at cursor |
| 1–9 | Paste slot |
| ⌥ + 1–9 | Toggle pin |
| ⌘ + 1–9 | Delete slot |
| 0 | Clear unpinned |
| Esc | Close picker |
| Hover slot | Instant Quick Look |

---

## Notes

* Network is required only for the optional Translate feature.
* Sensitive detection is **off by default**; turn it on in **Settings…** if you want password-manager / high-entropy flagging and auto-expire.
* File-path previews, Translate, slot numbers, paste sound, and notifications are all configurable.
* Built with `rumps`, AppKit, and `quickmachotkey`.
