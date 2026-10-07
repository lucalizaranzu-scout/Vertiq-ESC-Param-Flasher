#!/usr/bin/env bash
# Add an "ESC Flasher" entry to the application menu that runs run.sh from this
# checkout. Re-run it after moving the checkout.
#   ./install-launcher.sh             menu entry
#   ./install-launcher.sh --desktop   menu entry plus a desktop icon
#   ./install-launcher.sh --uninstall remove both
set -euo pipefail
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

name=esc-flasher.desktop
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

# Desktop Entry spec: quote the Exec argument, backslash-escape \ " ` $ inside it,
# then escape backslashes once more for the string value itself.
exec_arg() {
    local s=$1
    s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//\`/\\\`}; s=${s//\$/\\\$}
    s=${s//\\/\\\\}
    printf '"%s"' "$s"
}

case "${1:-}" in
    --uninstall)
        rm -fv "$apps/$name" "$desktop_dir/$name"
        exit 0 ;;
    ""|--desktop) ;;
    *) echo "usage: $0 [--desktop | --uninstall]" >&2; exit 2 ;;
esac

mkdir -p "$apps"
cat > "$apps/$name" <<EOF
[Desktop Entry]
Type=Application
Name=ESC Flasher
Comment=Flash Vertiq ESC profiles over DroneCAN
Exec=$(exec_arg "$here/run.sh")
Path=${here//\\/\\\\}
Icon=applications-engineering
Terminal=false
Categories=Development;Electronics;
EOF
chmod +x "$apps/$name"
echo "installed $apps/$name"

if [[ ${1:-} == --desktop ]]; then
    mkdir -p "$desktop_dir"
    cp "$apps/$name" "$desktop_dir/$name"
    chmod +x "$desktop_dir/$name"
    # GNOME won't run desktop launchers until they are marked trusted.
    gio set "$desktop_dir/$name" metadata::trusted true 2>/dev/null || true
    echo "installed $desktop_dir/$name"
fi

command -v update-desktop-database >/dev/null && update-desktop-database "$apps" 2>/dev/null || true
