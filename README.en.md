<div align="center">

# DeckDrop

**A Steam helper and gallery for your Steam Deck, over your home network**

Open a page on your phone or PC, paste a link or drop a file, and the Deck downloads it,
unpacks it and adds the game to Steam with artwork.

[![Release](https://img.shields.io/github/v/release/Aniforka/deckdrop)](https://github.com/Aniforka/deckdrop/releases/latest)
[![CI](https://github.com/Aniforka/deckdrop/actions/workflows/ci.yml/badge.svg)](https://github.com/Aniforka/deckdrop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776ab)
![SteamOS](https://img.shields.io/badge/SteamOS-3.x-1a9fff)

**English** · [Русский](README.ru.md)

<img src="docs/media/en/demo.gif" width="320" alt="A link pasted on a phone; the Deck downloads and unpacks the game">

</div>

## What it is

DeckDrop is a small web page that lives on the Deck itself. Open it from any device on the
same Wi-Fi, and from the couch you can:

- **download a game from a link**: direct links, Mega (decrypted right on the Deck),
  Yandex Disk, Google Drive, or just send a file from your phone or PC;
- **unpack archives**: zip, 7z, rar, multi-volume, password-protected (asked once, remembered);
- **add it to Steam** under a proper name, with your default Proton and **all five kinds of
  artwork**: from VNDB for visual novels, or drawn from the exe icon;
- **drop a patch** straight into the game folder, or unpack a patch archive over the game after
  seeing exactly what it will replace;
- **back up saves** to a zip and restore them;
- **browse Steam screenshots and recordings** in a password-protected gallery and save them
  to your phone;
- do all of this **in English or Russian**: the language follows your browser.

One Python file, standard library only. Nothing to install on stock SteamOS, no root, no
developer mode, and SteamOS updates do not break it.

## Screenshots

<table>
<tr>
<td align="center" valign="top" width="50%"><img src="docs/media/en/games.png" width="280" alt="Games tab"><br><sub>A download and the game list</sub></td>
<td align="center" valign="top" width="50%"><img src="docs/media/en/game.png" width="280" alt="Game page"><br><sub>Game page: Proton and artwork</sub></td>
</tr>
<tr>
<td align="center" valign="top" width="50%"><img src="docs/media/en/media.png" width="280" alt="Gallery"><br><sub>Screenshot gallery</sub></td>
<td align="center" valign="top" width="50%"><img src="docs/media/en/settings.png" width="280" alt="Settings"><br><sub>Settings and language</sub></td>
</tr>
</table>

<p align="center"><img src="docs/media/en/desktop.png" width="760" alt="DeckDrop in a desktop browser"></p>

## Quick start

1. On the Deck, switch to **Desktop Mode** and open **Konsole**.
2. Run two commands:

   ```bash
   wget https://github.com/Aniforka/deckdrop/releases/latest/download/install.sh
   sh install.sh
   ```

3. On your phone, open the address the installer prints, e.g. `http://steamdeck.local:8088`,
   and note the **admin PIN**.

That's it. DeckDrop starts by itself every time the Deck boots and keeps working in Gaming Mode.

## Contents

- [Installation](#installation)
  - [Requirements](#requirements)
  - [From GitHub](#from-github)
  - [From a PC on your network](#from-a-pc-on-your-network)
  - [First run: address and PIN](#first-run-address-and-pin)
  - [Updating](#updating)
  - [Logs, restart, uninstall](#logs-restart-uninstall)
  - [Environment variables](#environment-variables)
- [Interface language](#interface-language)
- [Steam control](#steam-control)
- [Passwords and PIN](#passwords-and-pin)
- [Games](#games)
  - [Downloading and unpacking](#downloading-and-unpacking)
  - [Adding a game you already have](#adding-a-game-you-already-have)
  - [Game page](#game-page)
  - [Game files and patches](#game-files-and-patches)
- [Artwork](#artwork)
- [Saves](#saves)
- [Archives](#archives)
- [Media](#media)
- [Supported links](#supported-links)
- [Mega](#mega)
- [Network and proxy](#network-and-proxy)
- [Settings](#settings)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Installation

### Requirements

- A Steam Deck on **SteamOS 3.x**. A regular Linux PC with Steam works too, given `python3`
  3.8+ and `systemd --user`.
- The Deck and your phone (or PC) on the **same Wi-Fi network**.
- Internet on the Deck to install from GitHub. Without it, install from a PC, see below.
- Optional: `ffmpeg` (precise artwork cropping, Steam recordings with sound) and `7z` or
  `bsdtar` (7z and rar archives). Without them, zip files are unpacked by Python itself and
  artwork is used as is.

No root password, `sudo`, read-only toggling or Decky Loader is needed: DeckDrop lives in your
home folder and runs as a user service.

### From GitHub

1. Switch the Deck to Desktop Mode: Steam button → Power → Switch to Desktop.
2. Open **Konsole** (application menu → System → Konsole).
3. Download and run the installer:

   ```bash
   wget https://github.com/Aniforka/deckdrop/releases/latest/download/install.sh
   ```

   ```bash
   sh install.sh
   ```

   It puts `deckdrop.py` into `~/deckdrop`, checks the download is complete, registers the
   `deckdrop` user service with autostart and starts it. At the end it prints the **page
   address** and the **admin PIN**.

4. You can go back to Gaming Mode: the service runs there too, and after reboots.

<details>
<summary>The same by hand, without the installer</summary>

```bash
mkdir -p ~/deckdrop
wget -O ~/deckdrop/deckdrop.py https://github.com/Aniforka/deckdrop/releases/latest/download/deckdrop.py
python3 ~/deckdrop/deckdrop.py --install
```

The `-O` flag is a capital letter **O**, not a zero.
</details>

### From a PC on your network

Useful when the Deck cannot reach GitHub, or to install your own copy.

1. On the PC, download `deckdrop.py` and `install.sh` from the
   [latest release](https://github.com/Aniforka/deckdrop/releases/latest), open a terminal in
   that folder and serve it. Keep the window open:

   ```bash
   python -m http.server 8000
   ```

2. Find the PC's address on your network: `ipconfig` on Windows (the IPv4 address line),
   `ip addr` or `ifconfig` on Linux and macOS. The examples below use `192.168.1.10`.
3. On the Deck, in Konsole:

   ```bash
   wget 192.168.1.10:8000/install.sh
   sh install.sh http://192.168.1.10:8000/deckdrop.py
   ```

4. Once the installer prints the address and the PIN, stop the server on the PC with Ctrl+C.

If the PC's firewall asks whether `python` may accept connections from private networks,
allow it.

### First run: address and PIN

The page opens from any device on the same network:

- **`http://steamdeck.local:8088`**, where `steamdeck` is the Deck's name (shown by the
  installer and in the Deck's Settings → System → Hostname). `.local` addresses work on iOS,
  macOS, Linux and Windows 10+; some Android browsers do not resolve them.
- **`http://<Deck IP>:8088`** works everywhere. The IP is printed by the installer and shown
  on the page's Settings tab.

Bookmark it or add it to your phone's home screen, and it feels almost like an app.

The **admin PIN** guards risky actions: deleting, updating, the proxy. On first install
DeckDrop picks a random 4-digit PIN; change it in Settings. If you lose it, look it up on the
Deck:

```bash
grep admin_pin ~/.config/deckdrop/state.json
```

Port 8088 is not random: 8080 belongs to Steam's own debugging port, which DeckDrop uses to
control Steam (see [Steam control](#steam-control)).

### Updating

The **Update DeckDrop** button at the bottom of the page fetches `deckdrop.py` from the
[latest release](https://github.com/Aniforka/deckdrop/releases/latest), checks it, replaces
itself and restarts. You can also give your own link, such as a copy served from a PC
(`http://192.168.1.10:8000/deckdrop.py`); it is remembered. If the new version uses another
port, the page moves to the new address by itself.

**Settings, passwords, the game list and everything else are kept across updates.** This is
tested automatically before every release: the test installs previous versions, sets them up
through the page, updates them with the button and checks nothing was lost.

Running `sh install.sh` again also updates to the latest version.

<details>
<summary>Running 0.3.23 or older?</summary>

Those versions updated from the `main` branch, which no longer has the file. Update such a
copy once by hand: paste
`https://github.com/Aniforka/deckdrop/releases/latest/download/deckdrop.py`
into the update dialog, or run `sh install.sh`. After that it updates from releases by itself.
</details>

### Logs, restart, uninstall

| What | Command on the Deck |
|---|---|
| Live log | `journalctl --user -u deckdrop -f` |
| Restart | `systemctl --user restart deckdrop` |
| Stop until the next boot | `systemctl --user stop deckdrop` |
| Remove the service | `python3 ~/deckdrop/deckdrop.py --uninstall` |
| Run in a terminal, without the service | `python3 ~/deckdrop/deckdrop.py` |

`--uninstall` removes the service only. Downloaded games, `~/deckdrop`, the settings
(`~/.config/deckdrop`) and the cache (`~/.cache/deckdrop`) stay; delete them by hand if you
like. To remove the Steam control marker, turn that feature off in Settings before uninstalling.

### Environment variables

Set them in `~/.config/systemd/user/deckdrop.service`, in the `[Service]` section, as lines
like `Environment=DECKDROP_PORT=8090`. Then run `systemctl --user daemon-reload` and
`systemctl --user restart deckdrop`.

| Variable | Default | Effect |
|---|---|---|
| `DECKDROP_PORT` | `8088` | page port |
| `DECKDROP_GAMES` | `~/Games` | games folder on the internal disk |
| `DECKDROP_DISKS` | — | extra disks: `label=/path;label2=/path2` |
| `DECKDROP_STEAM` | detected | Steam root, if it is not in the usual place |
| `DECKDROP_STATE` | `~/.config/deckdrop/state.json` | settings and state file |
| `DECKDROP_CEF` | `1` | `0` turns Steam control off |
| `DECKDROP_CEF_PORT` | `8080` | Steam's debugging port |
| `DECKDROP_PIN` | random | initial PIN on first install |
| `DECKDROP_UPDATE_URL` | latest release | default update link |
| `DECKDROP_EXTRACT` | `1` | `0` leaves archives packed |
| `DECKDROP_KEEP` | `1` | `0` deletes an archive once it is unpacked |

## Interface language

DeckDrop speaks **English and Russian** and picks the language for each device, in this order:

1. **Settings → Language** on that phone or PC. The choice is kept on that device only: you
   can have English while a friend opening the page on their phone gets Russian.
2. **The browser language** of the device the page is open on.
3. **The Steam language on the Deck**, i.e. the one picked in the Deck's settings. SteamOS
   always keeps its system locale in English, so DeckDrop reads Steam's own language.
4. English.

Error messages and task texts come in the same language. The installer's messages follow the
Steam language on the Deck. Logs in `journalctl` are always in English.

## Steam control

Steam keeps its settings in memory and rewrites its files on exit, so edits to `config.vdf` and
`shortcuts.vdf` made while it runs are lost. DeckDrop does what Decky Loader does: it turns on
Steam's local CEF debugging port (the `.cef-enable-remote-debugging` marker file in the Steam
folder) and asks the running client to add a shortcut with the right name, set Proton, apply
artwork or remove the shortcut.

- The port listens on the Deck only (localhost); it is not reachable from outside.
- It becomes active after **one Deck reboot**. The status is shown at the bottom of the page
  and in Settings.
- Until then, games are added with `steamos-add-to-steam`, and the name and Proton are queued
  and applied automatically once control is available.
- Turning it off in Settings removes the marker; Steam closes the port on its next start.

## Passwords and PIN

- **Admin PIN**: deleting games and media, resetting the gallery password, the proxy, updates.
  Random on install, 4 to 8 digits, changed in Settings.
- **Gallery password**: set on the first visit to the Media tab and asked every time it opens.
  Stored as a PBKDF2 hash; a session lasts 12 hours. Reset with the PIN at the bottom of the page.

## Games

### Downloading and unpacking

- Paste a link into the field, press Ctrl+V anywhere on the page, or drag a file or link onto
  it. Next to the title you pick the disk (internal or microSD) with its free space; the
  choice is remembered.
- Archives (zip, 7z, rar, tar.\*) are unpacked into `<disk>/Games/<archive name>`; a `(1)`
  tail from a repeated download on a PC is dropped from the folder name.
- An encrypted archive stops with "password needed" and a password field right in the task.
  "Remember" adds the password to a list that is tried automatically from then on.
- Every download can be cancelled, "stop all" clears the whole queue, and partial files are
  deleted. "Clear" removes finished tasks from the list.
- Each game in the list is a card: name, disk, whether it is in Steam, whether it has artwork.
  Tap it to open the [game page](#game-page); "← back to the list" or the phone's Back button
  returns.

### Adding a game you already have

**"+ add a game"** adds **one game** that already lives somewhere on the Deck, for example
one you set up by hand earlier. Just give the path to its launch file:

    /home/deck/Games/MyGame/Game.exe

DeckDrop copies and moves nothing: the folder stays where it is and shows up in the list marked
"own". From then on it behaves like any other game.

- If the game is already a Steam shortcut, DeckDrop picks up its name, Proton and artwork.
  Steam and the game files are not touched.
- The dialog lists games that are **already in Steam** but live outside the DeckDrop folders;
  each can be added with one tap.
- The game's folder works too; if the exe sits in `bin` or `game`, the folder above is used.
- Games can be added from the home folder and from mounted drives.
- "Remove from DeckDrop" only removes the card. "Delete from Deck 🔒" deletes the files, with
  the PIN.

### Game page

- **Launch**: every executable of the game. A Linux build (`.sh`, `.x86_64`) is recommended
  when there is one: it runs natively, without Proton. The added file has a check mark and a
  Proton picker.
- **Add to Steam**: the name comes from the exe without its extension, capitalized
  (`yosuga.exe` → "Yosuga"); for faceless `game.exe` or `start.exe`, from the folder name;
  versions like `v1.4`, tags in brackets and `(1)` are dropped. When there are several
  candidates, a dialog offers them, a field for your own name and a Proton picker. The name
  can be changed later with the pencil next to the title.
- **Artwork**: all five Steam slots with previews, see [Artwork](#artwork).
- **Saves**: what goes into the backup, "download backup" and "import zip".
- **Actions**: hide from the list, delete from the Deck with the PIN (a check box removes the
  Steam shortcut too).
- **Details**: folder, size, name in Steam, AppID, Proton, artwork source.

### Game files and patches

The "Game files" card uploads a patch or any other file straight into the game folder. The
path is **relative to the exe's folder**:

| Path | Where it goes |
|---|---|
| empty | next to the exe |
| `data/patch` | a subfolder next to the exe, created if missing |
| `../` | one level above the exe, e.g. when it sits in `bin/` |

Nothing can go outside the game folder. If the file exists, DeckDrop asks first and keeps the
very first version next to it as `name.bak`, so the original survives any number of patch
updates.

**Unpack an archive over the game.** "Unpack an archive…" first unpacks it aside on the Deck
and shows what is inside, where it goes, and how many files it adds and replaces. If everything
is wrapped in one folder (`Patch v2/…`), a check box strips it. Replaced files are kept as
`.bak` by default. Nothing changes until you confirm.

## Artwork

Steam's slots: the 600×900 portrait capsule (seen in Big Picture), the 920×430 landscape one,
the 1920×620 hero banner, the logo and the icon. A full set is applied right after a game is
added.

- **VNDB** (experimental): the game's name is looked up as a visual novel. The cover goes to
  the portrait slot, a screenshot to landscape and hero, the logo is drawn as text, the icon
  comes from the exe. Automatic lookup is a setting and only fires on a confident title match.
  18+ images can be skipped with a check box in Settings.
- **From the exe icon**: DeckDrop reads the PE resources itself and puts the icon on a
  background of its own color.
- **Your own images**: any slot can take a PNG or JPEG from your phone or PC, or a specific
  image from VNDB.
- With `ffmpeg`, images are cropped precisely to each slot; without it, VNDB images are used
  as they are.
- With Steam control on, new artwork shows up immediately; otherwise after Steam restarts.

## Saves

"Download backup" zips the save folders inside the game (`save`, `saves`, `savedata` and
similar), the user folders of the Proton prefix (`AppData`, `Documents`, `Saved Games`, minus
system clutter) and, for Linux builds, the Ren'Py and Unity folders in your home. Before an
import DeckDrop backs up the current saves to `~/.cache/deckdrop`, and files from the archive
cannot escape the target folders.

## Archives

Everything in `_inbox` on every disk: size, date, unpacked or not. Unpack from here or delete
anything. "Delete unpacked" removes, in one tap, archives that already have a game folder.
"Clear all" empties `_inbox` completely, after showing how much space it frees; game folders
and running downloads are left alone.

## Media

Steam screenshots with game names, Steam recordings (joined into mp4, with sound when `ffmpeg`
is there), videos and pictures from `~/Videos` and `~/Pictures`. Tap a tile to view it:
"download" saves it to your phone, "delete from Deck 🔒" frees the space. Videos play and seek
fine on iPhones too.

## Supported links

| Source | What happens |
|---|---|
| A direct file link | downloaded as is |
| Yandex Disk (`disk.yandex.*`, `yadi.sk`) | the direct link comes from Disk's public API |
| Google Drive | via `drive.usercontent.google.com` |
| Mega (`mega.nz`, `mega.co.nz`) | files and folders, new and old links; decrypted on the Deck |
| Telegram | there is no file link: download on your phone and upload the file |
| A page with a button (itch.io, file hosts with a timer) | an HTML page arrives instead of a file, and DeckDrop says so |

## Mega

Mega encrypts a file in the uploader's browser, and the key is part of the link after the `#`.
So **copy the whole link**: without the part after `#` there is nothing to decrypt with, and
DeckDrop says so right away.

| Link | What happens |
|---|---|
| `mega.nz/file/<id>#<key>` | downloads right away, the name comes from the encrypted metadata |
| `mega.nz/folder/<id>#<key>` | opens a file list; the ticked files download as one game |
| `mega.nz/folder/<id>#<key>/file/<id>` | downloads just that file |
| Old `mega.nz/#!<id>!<key>` and `#F!<id>!<key>` | understood too |
| `mega.nz/#P!…` (password-protected) | not supported yet: open it in a browser and take the regular link |

Files are decrypted on the fly (AES via the system libcrypto) and the Mega checksum is verified
at the end, so a damaged file is never passed off as complete. An interrupted download resumes
where it stopped. Files download one at a time, since Mega dislikes several connections from
one address.

**A Mega folder is one game.** Ticked files download as one task with shared progress
("Game · file 12 of 300") and keep the whole folder structure. If the folder holds only
archives, the main one is unpacked: a plain archive, `.part1.rar` or `.7z.001`.

If Mega reports a transfer quota, that is its limit on free downloads: wait a few hours or turn
on a proxy.

## Network and proxy

Some providers cut connections to VNDB; the game details then say `connection reset`. For that,
Settings has a proxy used **by DeckDrop only**; the rest of the Deck goes online as usual.

- `socks5://host:port` and `http://host:port` are supported, with a login if needed:
  `socks5://user:password@host:port`.
- VLESS and similar protocols are not supported directly and are not needed: their clients
  (Xray, for example) expose a local SOCKS5 port. Use the address of a PC running such a client
  (`socks5://192.168.1.10:10808`, allowing connections from the local network in the client),
  or run the client on the Deck and use `socks5://127.0.0.1:10808`.
- "Download games through the proxy too" applies the proxy to downloads. It is off by default:
  big files are usually faster directly.
- "Test the connection to VNDB" shows whether VNDB is reachable directly and through the proxy,
  with response times.
- The proxy address may contain a password, so it is **behind the PIN**: the page shows only a
  masked version.

## Settings

Open them with the tab or the ⚙ in the header; every switch saves immediately. They cover the
interface language, the default Proton for new games, preferring Linux builds, Steam control,
automatic VNDB lookup and skipping 18+ images, the proxy and a connection test, the Mega
checksum check, archive passwords, the update link, changing the PIN, and disk and address
details.

## Security

DeckDrop is meant for a **home network**. The gallery and risky actions are protected by a
password and the PIN, but uploading files and links is open: anyone on your Wi-Fi can put a
file on the Deck. There is no encryption (HTTPS), so passwords cross the network in plain text.
Do not expose port 8088 to the internet, and do not run DeckDrop on public networks.

## Troubleshooting

<details>
<summary><b>The page does not open at <code>steamdeck.local</code></b></summary>

Some Android browsers do not resolve `.local` addresses. Use the IP instead: the installer
prints it and it is shown in Settings → System, e.g. `http://192.168.1.42:8088`. Make sure the
phone and the Deck are on the same Wi-Fi network (not a guest one).
</details>

<details>
<summary><b>The name or Proton was not applied, it says "queued"</b></summary>

Steam control turns on after one Deck reboot. Until then, changes wait in a queue and apply by
themselves. If Steam control is off in Settings, set the name and Proton in Steam by hand.
</details>

<details>
<summary><b>"The link serves an HTML page, not a file"</b></summary>

The link leads to a page with a Download button (itch.io, file hosts with a timer), not to the
file itself. Download it on your phone or PC and drop the file onto the DeckDrop page.
</details>

<details>
<summary><b>Mega: "the transfer quota is used up"</b></summary>

That is Mega's limit on free downloads from one address. Wait a few hours, or turn on a proxy
in Settings with "Download games through the proxy too".
</details>

<details>
<summary><b>No artwork from VNDB, "connection reset"</b></summary>

Your provider blocks VNDB. Set up a proxy (see [Network and proxy](#network-and-proxy)) and
press "Test the connection to VNDB". Artwork from the exe icon works without the network.
</details>

<details>
<summary><b>I forgot the PIN</b></summary>

On the Deck, in Konsole: `grep admin_pin ~/.config/deckdrop/state.json`.
</details>

<details>
<summary><b>I want the interface in another language</b></summary>

Pick it in Settings → Language; the choice applies to that device only. Your language is not
there yet? Adding one is easy, see [Development](#development).
</details>

## Development

The source is the [`src/deckdrop`](src/deckdrop) package, one module per area; the page is
[`web/index.html`](src/deckdrop/web/index.html), `app.css` and `app.js`. Users still get a
single file: [`tools/build.py`](tools/build.py) packs the package into `dist/deckdrop.py`, and
that is what releases ship. Neither the app nor the build has external dependencies.

<details>
<summary>Layout</summary>

```
src/deckdrop/
  app.py             entry point: the server and background loops
  config.py          environment variables, paths, constants, log()
  state.py           state.json, PIN, gallery password
  i18n/              languages: __init__.py and one JSON per language (en.json, ru.json)
  storage.py         disks, microSD, game roots
  jobs.py            download and unpack tasks
  paths.py           file names, archive volumes, sizes
  net.py             proxy (SOCKS5/HTTP), HTTP, retries
  downloads.py       downloads by link, queue, cancelling
  aes.py, mega.py    Mega: AES (libcrypto or pure Python), API, folders
  archives.py        unpacking, passwords, the Archives tab
  detect.py          the executables in a game folder and a human name for the game
  steam/             Steam VDF files, Proton, controlling the client over CEF, shortcuts
  art/               PNG/ICO/PE icons, ffmpeg, artwork from the icon and VNDB
  games.py           the Games tab, importing, adding to Steam
  saves.py, media.py, patches.py
  update.py          self-update and restart
  service.py         installing the systemd service
  bundle.py          where this copy runs from: the built file or src/
  web/               server.py (HTTP API), page.py, index.html, app.css, app.js
tools/
  build.py           builds the single file
  dev.py             runs straight from src/, no build
  screenshots.py     README screenshots and GIFs from demo data
tests/
```
</details>

**Run on a PC without building:** `python tools/dev.py`. On Windows or without Steam some
features say "unavailable", and the page opens at `http://localhost:8088`. Point
`DECKDROP_STATE` and `DECKDROP_GAMES` elsewhere to keep your own folders untouched.

**Checks, the same as CI:**

```bash
python tools/build.py                     # -> dist/deckdrop.py
python -m pyflakes src tools tests
python -m unittest discover -s tests      # modules, languages, AES, VDF, updates, user data
python tests/smoke.py dist/deckdrop.py    # the server from the built file
python tests/ui_check.py                  # the page in Chromium in every language (needs playwright)
```

The key test is [`tests/test_user_data.py`](tests/test_user_data.py): it installs previous
releases, sets them up through the page, updates them with the button and checks that no
setting, password, game or file of the user was lost. CI runs it as its own required step.

**Releasing.** Bump `__version__` in [`src/deckdrop/__init__.py`](src/deckdrop/__init__.py) and
merge into `main`. GitHub Actions builds the file, runs every check, tags `vX.Y.Z` and publishes
a release with `deckdrop.py` and `install.sh`. Commits that keep the version do not release.

**A new language.** Copy [`src/deckdrop/i18n/en.json`](src/deckdrop/i18n/en.json) to
`<code>.json` (e.g. `de.json`) and translate the values, leaving the keys and `{placeholders}`
alone. That's all: the language shows up in Settings and is picked by the browser. The tests
check its keys and placeholders match every other language. If the language has its own plural
rules, add them to `plural_form()` in [`i18n/__init__.py`](src/deckdrop/i18n/__init__.py) and
to `plural()` in `app.js`. Refresh the README screenshots with `python tools/screenshots.py`.
