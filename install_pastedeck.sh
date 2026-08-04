set -e

echo "🚀 PasteDeck Dependency Installer"
echo "=================================="
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.9+ first."
    echo "   Recommended: https://www.python.org/downloads/ or 'brew install python'"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "✅ Python $PYTHON_VERSION found"
echo "   Path: $(which python3)"
echo ""

# Prefer python3 -m pip (more reliable than bare pip3)
if ! python3 -m pip --version &> /dev/null; then
    echo "❌ pip is not available for this Python."
    echo "   Try: python3 -m ensurepip --upgrade"
    exit 1
fi

echo "📦 Installing PasteDeck dependencies..."
echo ""

# Single install command (matches the manual fix)
# pyobjc-core provides the 'objc' module
python3 -m pip install --user \
    pyobjc-core \
    pyobjc-framework-Cocoa \
    pyobjc-framework-Quartz \
    rumps \
    quickmachotkey

echo ""
echo "🔍 Verifying critical imports..."
if python3 -c "import objc; import AppKit; import Quartz; import rumps; from quickmachotkey import quickHotKey; print('✅ All imports OK')" 2>/dev/null; then
    echo "✅ All modules import successfully"
else
    echo "⚠️  Some modules failed to import. Try this manually:"
    echo "   python3 -m pip install --user --force-reinstall pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz rumps quickmachotkey"
fi

echo ""
echo "✅ Installation finished!"
echo ""
echo "📋 Next steps:"
echo "   1. Make sure PasteDeck.py is in the same folder (or give full path)"
echo "   2. Run:  python3 PasteDeck.py"
echo ""
echo "   If you get a permission error about Accessibility / Input Monitoring,"
echo "   go to System Settings → Privacy & Security and allow Terminal / Python."
echo ""
