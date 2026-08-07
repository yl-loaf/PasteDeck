# 📋 PasteDeck

> A lightweight, privacy-focused multi-clipboard manager for macOS.

**PasteDeck** expands your macOS clipboard into a 9-slot power tool that lives in the menu bar. Built with Python and native AppKit APIs, it captures text, rich formatting, images, URLs, and hex colors—with instant hotkey access, hover Quick Look, and in-panel translation.

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
* Preview is positioned beside the picker, aligned with the hovered slot.

### Translate (inside Quick Look)
* Language dropdown (common targets) + **Translate** + **Replace**.
* **Detected-language chip** — Apple NaturalLanguage framework detects the source language asynchronously on open and refreshes after translate.
* Engines: Google Translate (primary) and MyMemory (fallback), with SSL handling.
* Result appears in-panel with auto-scroll.
* **Replace** writes the translation back into the clipboard slot *and* the system pasteboard.

### Privacy & sensitive data
* Detects copies from password managers (1Password, Bitwarden, LastPass, Keychain, etc.) and high-entropy secret patterns.
* Sensitive items are flagged 🔒, kept **only in RAM** (never written to disk), and auto-expire (default **45 seconds**, configurable).
* Regular history stored locally at `~/.multi-clipboard.json`; settings at `~/.pastedeck-settings.json`.

### Settings
* Menu bar → **Settings…** opens a native window.
* Configurable **sensitive auto-expire** interval.
* Optional **notifications**.
* Settings persisted across launches.

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
* **Python** 3.10+

### Install
```bash
cd ~/path/to/PasteDeck
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
pip install -r requirements.txt
```

### Run
```bash
./run_pastedeck.command
```
or
```bash
python3 PasteDeck.py
```

---

## Privacy & Security

* **Local-first** — clipboard history (except sensitive items) lives only on your machine under `~/.multi-clipboard.json`.
* **Sensitive handling** — passwords/tokens stay in memory, auto-delete after the configured timeout, and are never persisted.
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
* Sensitive auto-expire and notifications are controlled from **Settings…**.
* Built with `rumps`, AppKit, and `quickmachotkey`.
