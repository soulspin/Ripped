#!/bin/sh
# Installs Ripped for the current user (or system-wide with PREFIX=/usr/local, as root).
# Usage: ./install.sh            install to ~/.local
#        ./install.sh --uninstall
set -e
PREFIX="${PREFIX:-$HOME/.local}"
SRC="$(dirname "$(readlink -f "$0")")"
LIB="$PREFIX/share/ripped"
APPS="$PREFIX/share/applications"
ICONS="$PREFIX/share/icons/hicolor/scalable/apps"

if [ "$1" = "--uninstall" ]; then
    rm -rf "$LIB" "$PREFIX/bin/ripped" "$APPS/se.soulspin.Ripped.desktop" \
           "$ICONS/se.soulspin.Ripped.svg"
    echo "Ripped uninstalled from $PREFIX"
    exit 0
fi

mkdir -p "$LIB" "$PREFIX/bin" "$APPS" "$ICONS"
rm -rf "$LIB/ripped"
cp -r "$SRC/ripped" "$LIB/"
find "$LIB/ripped" -name __pycache__ -prune -exec rm -rf {} +
cat > "$PREFIX/bin/ripped" <<LAUNCHER
#!/bin/sh
PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m ripped "\$@"
LAUNCHER
chmod +x "$PREFIX/bin/ripped"
sed "s|^Exec=.*|Exec=$PREFIX/bin/ripped %f|" "$SRC/se.soulspin.Ripped.desktop" \
    > "$APPS/se.soulspin.Ripped.desktop"
cp "$SRC/data/icons/hicolor/scalable/apps/se.soulspin.Ripped.svg" "$ICONS/"
command -v gtk-update-icon-cache >/dev/null && \
    gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null && \
    update-desktop-database -q "$APPS" 2>/dev/null || true
echo "Ripped installed to $PREFIX (run: ripped)"
