"""DeckDrop - LAN inbox, Steam helper and media gallery for Steam Deck.

Open http://<deck>.local:8088 from a phone or PC, paste a link or drop a file.
The Deck downloads it (to the internal disk or a microSD, Mega links included,
decrypted on the fly), unpacks archives
(asking for a password when needed), adds the game to Steam under a clean name,
picks the default Proton, and sets every kind of Steam cover art: from VNDB for
visual novels, or built from the exe icon. Extra tabs: archives, and a
password-protected gallery of Steam screenshots and clips. Save games can be
backed up to a zip and imported back.

Steam is driven live through its CEF remote-debugging port (the same mechanism
Decky Loader uses). DeckDrop enables it with a marker file; it becomes active
after one Steam restart (a Deck reboot). Until then, name/Proton changes are
queued and applied automatically later.

Stdlib only, runs on stock SteamOS (nothing to install, survives OS updates).

    python3 deckdrop.py             run in foreground
    python3 deckdrop.py --install   install as a systemd user service (autostart, works in Gaming Mode)
    python3 deckdrop.py --uninstall

Env overrides: DECKDROP_PORT (8088), DECKDROP_GAMES (~/Games), DECKDROP_STATE (state file),
               DECKDROP_STEAM (Steam root), DECKDROP_UPDATE_URL, DECKDROP_PIN (initial admin PIN, random if unset),
               DECKDROP_DISKS (extra roots "label=path;..."), DECKDROP_CEF=0 (no Steam control),
               DECKDROP_CEF_PORT (8080), DECKDROP_EXTRACT=0 (don't unpack), DECKDROP_KEEP=0
"""
__version__ = "0.5.0"
