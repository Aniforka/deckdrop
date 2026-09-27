#!/bin/sh
# DeckDrop installer for Steam Deck (SteamOS).
# Downloads deckdrop.py into ~/deckdrop and registers the systemd user service.
#
#   sh install.sh                                        latest release from GitHub
#   sh install.sh http://192.168.1.10:8000/deckdrop.py   a copy served from your PC
set -e
SRC="${1:-${DECKDROP_SRC:-https://github.com/Aniforka/deckdrop/releases/latest/download/deckdrop.py}}"
DEST="$HOME/deckdrop/deckdrop.py"

# messages follow the language picked on the Deck (Steam's own; SteamOS keeps its locale in English)
if grep -qi '"language"[[:space:]]*"russian"' "$HOME/.steam/registry.vdf" 2>/dev/null; then
    M_GET="Скачиваю deckdrop.py"; M_SVC="Ставлю сервис"
    M_DONE="Готово. Адрес страницы и PIN администратора напечатаны выше."
else
    M_GET="Downloading deckdrop.py"; M_SVC="Installing the service"
    M_DONE="Done. The page address and the admin PIN are printed above."
fi

echo "==> $M_GET: $SRC"
mkdir -p "$HOME/deckdrop"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$DEST.new" "$SRC"
else
    wget -q -O "$DEST.new" "$SRC"
fi
# refuse an HTML error page or a truncated file before replacing a working copy
python3 -c "import sys; compile(open(sys.argv[1], encoding='utf-8').read(), sys.argv[1], 'exec')" "$DEST.new"
mv -f "$DEST.new" "$DEST"

echo "==> $M_SVC"
python3 "$DEST" --install
echo "==> $M_DONE"
