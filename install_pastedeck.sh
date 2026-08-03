#!/bin/bash

# PasteDeck Dependency Installation Script
# This script installs all required Python modules for PasteDeck

set -e  # Exit on error

echo "🚀 PasteDeck Dependency Installer"
echo "=================================="
echo ""

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.9+ first."
    exit 1
fi

PYTHON_VERSION=$(python3 --version | awk '{print $2}')
echo "✅ Python $PYTHON_VERSION found"
echo ""

# Check if pip is installed
if ! command -v pip3 &> /dev/null; then
    echo "❌ pip3 is not installed. Please install pip3 first."
    exit 1
fi

echo "📦 Installing PasteDeck dependencies..."
echo ""

# Install dependencies from requirements
DEPENDENCIES=(
    "rumps>=0.4.0"
    "pynput>=1.7.6"
    "pyobjc-framework-Cocoa>=10.0"
    "pyobjc-framework-Quartz>=10.0"
)

for dep in "${DEPENDENCIES[@]}"; do
    echo "   Installing: $dep"
    pip3 install "$dep"
done

echo ""
echo "✅ All dependencies installed successfully!"
echo ""
echo "📋 Next steps:"
echo "   1. Run PasteDeck: python3 PasteDeck.py"
echo "   2. Or use: ./run_pastedeck.command"
echo ""
