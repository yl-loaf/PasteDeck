#ignore
#run as sh <updateApp.sh path>
cd /Users/cheeyl/Documents/GitHub/PasteDeck/src   
# Clear old cache files
rm -rf build dist

# Rebuild with your custom icon
pyinstaller --windowed --onefile --icon=icon.icns PasteDeck.py