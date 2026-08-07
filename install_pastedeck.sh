#!/bin/bash
set -e

echo "🚀 PasteDeck Dependency Installer"
echo "=================================="
echo ""

# --- Ensure latest Python (via Homebrew if available) ---
if command -v brew &> /dev/null; then
    echo "🍺 Homebrew detected — ensuring latest Python..."
    brew update --quiet 2>/dev/null || true
    if brew list python &>/dev/null || brew list python@3 &>/dev/null; then
        brew upgrade python 2>/dev/null || brew upgrade python@3 2>/dev/null || true
    else
        brew install python 2>/dev/null || true
    fi
    # Prefer the Homebrew python3 on PATH
    if [ -d "$(brew --prefix)/bin" ]; then
        export PATH="$(brew --prefix)/bin:$PATH"
    fi
    echo "✅ Homebrew Python ready"
    echo ""
else
    echo "ℹ️  Homebrew not found. Using system python3."
    echo "   (For the absolute latest Python: install Homebrew then re-run this script)"
    echo ""
fi

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.9+ first."
    echo "   Recommended: https://www.python.org/downloads/ or 'brew install python'"
    exit 1
fi

PYTHON_BIN="$(command -v python3)"
PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1 | awk '{print $2}')
echo "✅ Python $PYTHON_VERSION found"
echo "   Path: $PYTHON_BIN"
echo "   Arch: $("$PYTHON_BIN" -c 'import platform; print(platform.machine())' 2>/dev/null || echo unknown)"
echo ""

# Prefer python3 -m pip (more reliable than bare pip3)
if ! "$PYTHON_BIN" -m pip --version &> /dev/null; then
    echo "⚠️  pip not available — trying ensurepip..."
    "$PYTHON_BIN" -m ensurepip --upgrade 2>/dev/null || true
    if ! "$PYTHON_BIN" -m pip --version &> /dev/null; then
        echo "❌ pip is not available for this Python."
        echo "   Try: $PYTHON_BIN -m ensurepip --upgrade"
        exit 1
    fi
fi

# Force-upgrade core packaging tools first (pip, setuptools, wheel)
echo "⬆️  Upgrading pip / setuptools / wheel to latest..."
"$PYTHON_BIN" -m pip install --user --upgrade --force-reinstall --no-cache-dir \
    pip setuptools wheel 2>&1 | tail -5
echo ""

# Packages required by PasteDeck (order matters: pyobjc-core first)
# - pyobjc-framework-NaturalLanguage → language detection in Quick Look
# - certifi → SSL certs for Translate (Google / MyMemory)
PACKAGES=(
    "pyobjc-core"
    "pyobjc-framework-Cocoa"
    "pyobjc-framework-Quartz"
    "pyobjc-framework-NaturalLanguage"
    "rumps"
    "quickmachotkey"
    "certifi"
)

# Install one package at a time with retries for reliability
install_pkg() {
    local pkg="$1"
    local attempt max_attempts=3
    for attempt in $(seq 1 $max_attempts); do
        echo "   → Installing $pkg (attempt $attempt/$max_attempts)..."
        if "$PYTHON_BIN" -m pip install --user --upgrade --force-reinstall --no-cache-dir "$pkg"; then
            echo "     ✅ $pkg OK"
            return 0
        fi
        echo "     ⚠️  Failed. Retrying in 2s..."
        sleep 2
    done
    # Last-resort: try without --user (some environments block user site)
    echo "     ⚠️  Retrying $pkg without --user..."
    if "$PYTHON_BIN" -m pip install --upgrade --force-reinstall --no-cache-dir "$pkg"; then
        echo "     ✅ $pkg OK (system/site-packages)"
        return 0
    fi
    return 1
}

echo "📦 Installing PasteDeck dependencies (latest, forced, no cache)..."
echo ""

FAILED=()
for pkg in "${PACKAGES[@]}"; do
    if ! install_pkg "$pkg"; then
        FAILED+=("$pkg")
        echo "     ❌ $pkg could not be installed after retries"
    fi
    echo ""
done

# --- Verify each import individually ---
echo "🔍 Verifying critical imports..."
VERIFY_OK=1

check_import() {
    local label="$1"
    local code="$2"
    if "$PYTHON_BIN" -c "$code" 2>/dev/null; then
        echo "   ✅ $label"
        return 0
    else
        echo "   ❌ $label  (import failed)"
        VERIFY_OK=0
        return 1
    fi
}

check_import "objc (pyobjc-core)"           "import objc"
check_import "AppKit (Cocoa)"               "from AppKit import NSPasteboard"
check_import "Foundation"                   "from Foundation import NSObject"
check_import "Quartz"                       "import Quartz"
check_import "NaturalLanguage"              "from NaturalLanguage import NLLanguageRecognizer"
check_import "rumps"                        "import rumps"
check_import "quickmachotkey"               "from quickmachotkey import quickHotKey"
check_import "certifi"                      "import certifi"

echo ""

if [ ${#FAILED[@]} -eq 0 ] && [ $VERIFY_OK -eq 1 ]; then
    echo "✅ All modules installed and import successfully"
else
    echo "⚠️  Some packages had problems."
    if [ ${#FAILED[@]} -gt 0 ]; then
        echo "   Failed installs: ${FAILED[*]}"
    fi
    echo ""
    echo "   Manual fix (run in Terminal):"
    echo "   $PYTHON_BIN -m pip install --user --upgrade --force-reinstall --no-cache-dir \\"
    echo "       pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz \\"
    echo "       pyobjc-framework-NaturalLanguage rumps quickmachotkey certifi"
    echo ""
    echo "   If you still see import errors, try a fresh user site:"
    echo "   $PYTHON_BIN -m pip uninstall -y pyobjc-core pyobjc-framework-Cocoa \\"
    echo "       pyobjc-framework-Quartz pyobjc-framework-NaturalLanguage \\"
    echo "       rumps quickmachotkey certifi"
    echo "   then re-run this script."
fi

echo ""
echo "✅ Installation finished!"
echo ""
echo "📋 Next steps:"
echo "   1. Make sure PasteDeck.py is in the same folder (or give full path)"
echo "   2. Run:  $PYTHON_BIN PasteDeck.py"
echo ""
echo "   If you get a permission error about Accessibility / Input Monitoring,"
echo "   go to System Settings → Privacy & Security and allow Terminal / Python."
echo ""
