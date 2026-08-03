# 📋 PasteDeck

> A lightweight, privacy-focused multi-clipboard manager for macOS.

**PasteDeck** expands your macOS clipboard into a 9-slot power tool that runs directly from your menu bar. Built with Python and native AppKit APIs, it captures text, rich formatting, images, URLs, and hex colors—giving you instant hotkey access to past copies.

---

## ✨ Key Features

* **Global Hotkey Picker:** Press `Cmd + Option + Shift + V` anywhere on macOS to bring up a floating paste panel right at your cursor position.
* **Rich Content Support:**
  * **Images:** Preview captured screenshots and image clips directly in the picker.
  * **Hex Colors:** Automatic color swatch previews for hex values (e.g., `#676767`, `#FF007F`).
  * **URLs:** Automatically fetches and displays web page titles.
  * **Rich Text:** Retains formatting (RTF/HTML) across applications.
* **Smart Privacy & Auto-Delete:**
  * Automatically detects copies coming from password managers (1Password, Bitwarden, LastPass, Keychain, etc.) and high-entropy secret patterns.
  * Sensitive items are encrypted in memory, **never written to disk**, and automatically deleted after 45 seconds.
* **Slot Pinning:** Lock your frequently used snippets or boilerplate text to fixed slots so they never roll off your history stack.
* **⌨️ Fast Keyboard Control:**
  * `1`–`9` to paste a slot.
  * `Option + Slot` to toggle pin/unpin.
  * `Cmd + Slot` to delete a specific slot.
  * `0` to clear all unpinned items.
  * `Esc` to close.

---

## Installation & Setup

### Prerequisites
* **macOS:** 10.15 (Catalina) or newer.
* **Python:** 3.9+

### Dependencies
Navigate into the path of the folder:
```bash
cd ~/path/to/PasteDeck
```
And run this ```.sh``` code:

```bash
chmod +x install_pastedeck.sh
./install_pastedeck.sh
```
### 🛡️ Privacy & Security PasteDeck was built with privacy in mind:

**Local Storage:**
All local clipboard history (except pinned/regular items) is kept strictly on your local machine under ~/.multi-clipboard.json.

**Sensitive Data Handling:** 
Sensitive items (passwords, tokens, credentials) are flagged with 🔒, kept only in **RAM**, and cleared automatically after 45 seconds.

**Zero Telemetry:** 
No network connection or tracking telemetry is included.

**📄 License:**
Distributed under the **MIT** License. See **LICENSE** for more information.
