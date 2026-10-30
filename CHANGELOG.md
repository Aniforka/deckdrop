# Changelog · Что нового

Every version has a section here, in English and in Russian. The GitHub release of a version
takes its description from its section, so a version cannot be released without one.

У каждой версии здесь свой раздел, на английском и на русском. Описание релиза на GitHub
берётся из этого раздела, поэтому без него версия не выйдет.

## 0.8.1 — 2026-10-30

### English

**Added**
- **Unreal Engine games are recognized**: an executable in `Game/Binaries/Win64` (or `Win32`,
  `Binaries/Linux`) is found, so games like Dragon Quest XI S can be added to Steam and get
  artwork. The `-Win64-Shipping` suffix is dropped from the name, `CrashReportClient.exe` is
  ignored, and when a launcher sits next to the real game the `*-Win64-Shipping.exe` is
  recommended.
- **"Auto" on the artwork card**: runs the usual VNDB → Steam store → SteamGridDB → exe icon
  order for a game that is already in Steam, without adding it again.

### Русский

**Добавлено**
- **Игры на Unreal Engine теперь определяются**: исполняемый файл в `Game/Binaries/Win64`
  (или `Win32`, `Binaries/Linux`) находится, поэтому игры вроде Dragon Quest XI S можно
  добавить в Steam и получить обложки. Суффикс `-Win64-Shipping` убирается из названия,
  `CrashReportClient.exe` игнорируется, а если рядом с настоящей игрой лежит лаунчер,
  рекомендуется именно `*-Win64-Shipping.exe`.
- **«Авто» в карточке обложек**: запускает обычный порядок VNDB → магазин Steam → SteamGridDB →
  иконка exe для игры, которая уже есть в Steam, без повторного добавления.

## 0.8.0 — 2026-10-29

### English

**Added**
- **Move a game to another disk** from its page: the folder is renamed on the same disk and
  copied file by file across disks, with progress and a free space check. The Steam shortcut is
  pointed at the new executable, not recreated, so the **AppID, the Proton prefix with saves and
  settings, the name, artwork and controller layout stay**. The update backup in
  `.deckdrop-updates/` moves along, so the last update can still be rolled back. The move is
  refused while the game runs or has a new version unpacked aside; a game in Steam needs Steam
  control.
- **Disks and space** in Settings measures the library on the Deck and splits every disk into
  games, Proton prefixes, `_inbox` archives, update backups and the rest (Steam and the system).
  The biggest games and prefixes come first, with their folder, prefix and backup sizes; the same
  game on two disks is marked as a likely copy. Sizes are measured in the background and cached,
  and "Refresh" measures again. Only safe actions are offered: a prefix is never deleted here.
- **Artwork from the Steam store**: a game that is on Steam gets its portrait, hero, logo and
  wide image from the Steam CDN, with no key and only on an exact title match.
- **Artwork from SteamGridDB** (optional): a free API key in Settings adds the 920×430 wide grid,
  transparent logos and icons for games that are not on Steam. The 18+ check box applies here too.
- In `auto`, VNDB, the Steam store and SteamGridDB fill the set in that order, each one only
  filling the slots the previous ones left empty; the exe icon is the last resort. The game page
  has "Steam…" and "GridDB…" pickers next to "VNDB…", and Settings has a "Check the key" button.

**Changed**
- Automatic artwork now tries the Steam store after VNDB when the set is incomplete, so more
  games get real covers instead of capsules drawn from the exe icon.

### Русский

**Добавлено**
- **Перенос игры на другой диск** со страницы игры: на одном диске папка переименовывается,
  между дисками копируется файл за файлом — с прогрессом и проверкой свободного места. Ярлык
  Steam не пересоздаётся, а переключается на новый exe, поэтому **AppID, префикс Proton с сейвами
  и настройками, название, обложки и раскладка контроллера сохраняются**. Бэкап обновления
  из `.deckdrop-updates/` переезжает вместе с папкой, так что откат последнего обновления
  продолжает работать. Пока игра запущена или у неё распакована новая версия, перенос
  не начнётся; для игры из Steam нужно управление Steam.
- **«Диски и место»** в настройках измеряет библиотеку на деке и раскладывает каждый диск
  на игры, префиксы Proton, архивы в `_inbox`, бэкапы обновлений и всё остальное (Steam
  и система). Самые большие игры и префиксы идут первыми, с размерами папки, префикса
  и бэкапа; одна и та же игра на двух дисках помечается как вероятная копия. Размеры считаются
  в фоне и кэшируются, «Обновить» запускает новый подсчёт. Панель предлагает только безопасные
  действия: префикс отсюда не удаляется.
- **Обложки из магазина Steam**: игре, которая есть в Steam, вертикальная обложка, hero, логотип
  и широкая картинка берутся с CDN Steam — без ключа и только при точном совпадении названия.
- **Обложки из SteamGridDB** (по желанию): бесплатный ключ в настройках добавляет широкую сетку
  920×430, прозрачные логотипы и иконки для игр вне Steam. Галочка «пропускать 18+» действует
  и здесь.
- В режиме «авто» VNDB, магазин Steam и SteamGridDB заполняют набор по порядку, каждый —
  только пустые слоты; иконка exe — последний вариант. На странице игры рядом с «VNDB…»
  появились «Steam…» и «GridDB…», а в настройках — кнопка «Проверить ключ».

**Изменено**
- Автопоиск обложек теперь пробует магазин Steam после VNDB, если набор неполный, поэтому
  больше игр получают настоящие обложки вместо капсул, нарисованных из иконки exe.

## 0.7.0 — 2026-10-29

### English

**Added**
- **A whole game folder from your PC**: "Add" has a folder picker, and a folder can be dropped
  onto the page. The browser sends it file by file; DeckDrop keeps the tree, moves the folder next
  to your games and, when the settings ask for it, adds it to Steam.
- **Torrents**: a magnet link or a link to a `.torrent` file is handed to the qBittorrent already
  running on the Deck. DeckDrop follows the progress, takes the finished game away from the client
  (the files stay) and finishes with it like any other download.
- Settings → Torrents: the qBittorrent Web UI address (the usual ports are tried while it is
  empty), the user and password (kept behind the PIN), "check the connection", and "add a game that
  arrived as a folder or a torrent to Steam right away".
- Both new ways end like a download: a single archive in the folder is unpacked, a nested
  `Games/Foo/Foo.exe` folder is flattened, and an installer (`setup.exe`) is not added to Steam.
- An upload shows its speed, and a finished folder shows the "importing" stage while it becomes
  a game.

### Русский

**Добавлено**
- **Целая папка игры с ПК**: в «Добавить» появился выбор папки, а папку можно перетащить на
  страницу. Браузер шлёт её файл за файлом; DeckDrop сохраняет структуру, переносит папку
  к остальным играм и, если так просят настройки, добавляет её в Steam.
- **Торренты**: magnet-ссылка или ссылка на файл `.torrent` уходит в qBittorrent, который уже
  работает на деке. DeckDrop следит за загрузкой, забирает готовую игру у клиента (сами файлы
  остаются) и доводит её до конца как любое другое скачивание.
- Настройки → «Торренты»: адрес Web UI qBittorrent (пока пуст — ищем на обычных портах),
  пользователь и пароль (под PIN), «проверить связь» и «сразу добавлять в Steam игру, пришедшую
  папкой или торрентом».
- Оба новых способа заканчиваются как скачивание: единственный архив в папке распаковывается,
  вложенная папка `Games/Foo/Foo.exe` разворачивается, а установщик (`setup.exe`) в Steam не
  добавляется.
- У загрузки видно скорость, а готовая папка показывает стадию «разбираю», пока становится игрой.

## 0.6.0 — 2026-10-07

### English

**Added**
- **Update a game** from its page: a link to the new version or its file from your phone or PC.
  It is unpacked aside, and the page shows what the update will do before anything changes: new
  and replaced files, which exe Steam will start, which of your files stay. Then "update".
- The game keeps its folder and its Steam shortcut, so the Proton prefix with saves and settings,
  the name, Proton, artwork and controller layout stay. A renamed exe gets the same shortcut.
- Save folders in the game are never overwritten, your settings files (`*.ini`, `*.cfg`, ...)
  are kept unless you say otherwise, and old files stay unless you ask to clear them.
- The last update can be rolled back: what it replaced or cleared is kept aside, not deleted.
  An update waits while the game is running.

### Русский

**Добавлено**
- **Обновление игры** со страницы игры: ссылка на новую версию или её файл с телефона или ПК.
  Она распаковывается в сторонку, и страница показывает, что сделает обновление, до того как
  что-то изменится: новые и заменяемые файлы, какой exe будет запускать Steam, какие твои файлы
  останутся. Потом «обновить».
- Игра остаётся в своей папке и со своим ярлыком Steam, поэтому префикс Proton с сейвами
  и настройками, название, Proton, обложки и раскладка сохраняются. Переименованный exe получает
  тот же ярлык.
- Папки сейвов в игре никогда не перезаписываются, файлы настроек (`*.ini`, `*.cfg`…) остаются,
  если не сказать иначе, а старые файлы остаются, если не попросить их убрать.
- Последнее обновление можно откатить: то, что оно заменило или убрало, лежит в сторонке, а не
  удалено. Пока игра запущена, обновление ждёт.

## 0.5.1 — 2026-10-06

### English

**Added**
- **Select several items in Media.** Hold a photo or a video (right-click on a PC, or the new
  "select" button) and the gallery switches to selecting: tap more tiles or pick "all". The bar at
  the bottom shows how many are selected and their size, and offers "download" (one zip archive,
  or separate files) and "delete 🔒" with one PIN for all of them.

### Русский

**Добавлено**
- **Выбор нескольких файлов в «Медиа».** Зажми фото или видео (на ПК правый клик или новая
  кнопка «выбрать»), и галерея перейдёт в режим выбора: тапай другие плитки или жми «все».
  Панель внизу показывает, сколько выбрано и сколько это весит, и предлагает «скачать» (одним
  zip-архивом или отдельными файлами) и «удалить 🔒» с одним PIN на все.

## 0.5.0 — 2026-09-28

### English

**Added**
- **Controller layouts shared between games.** Steam offers a layout only in the game it was
  made for. Now the game page lists the game's layouts in Steam, and "Save to DeckDrop" makes
  one available to every game on the Deck: controller settings → Templates → "DeckDrop: name".
- **Apply a saved layout from the phone**: "Layout from DeckDrop" → "Apply" on the game page
  (with Steam control). DeckDrop checks that Steam took it; the game's previous layout stays in
  Steam. The page also shows the game's current layout.
- **Layout for new games** in Settings: applied when DeckDrop adds a game to Steam; "—" by default.
- **Settings → Controller layouts**: rename, delete, download as `.vdf`, or upload a `.vdf`,
  for example one from another Deck.
- Saving, renaming and deleting change only DeckDrop's own layout files: never a game's layouts,
  Valve's templates or DeckDrop's settings. If a Steam update clears the templates, DeckDrop
  puts its layouts back when it starts.

**Fixed**
- Settings → Performance → self-check no longer counts as a download of `deckdrop.py` on GitHub:
  it checks that the releases page answers instead of knocking on the file. The tests do not touch
  GitHub at all, so the download counter shows real downloads again.

### Русский

**Добавлено**
- **Раскладки контроллера, общие для всех игр.** Steam предлагает раскладку только в той игре,
  для которой её сделали. Теперь на странице игры видны её раскладки в Steam, а «Сохранить
  в DeckDrop» делает раскладку доступной в любой игре на деке: настройки контроллера →
  Шаблоны → «DeckDrop: название».
- **Применить сохранённую раскладку с телефона**: на странице игры «Раскладка из DeckDrop» →
  «Применить» (при управлении Steam). DeckDrop проверяет, что Steam её поставил; прежняя раскладка
  игры остаётся в Steam. Там же видно, какая раскладка у игры сейчас.
- **Раскладка для новых игр** в настройках: ставится, когда DeckDrop добавляет игру в Steam;
  по умолчанию «—».
- **Настройки → Раскладки контроллера**: переименовать, удалить, скачать `.vdf` или загрузить
  `.vdf`, например с другого дека.
- Сохранение, переименование и удаление меняют только собственные файлы раскладок DeckDrop:
  ни раскладки игр, ни шаблоны Valve, ни настройки DeckDrop не трогаются. Если обновление Steam
  очистит шаблоны, DeckDrop вернёт свои раскладки при запуске.

**Исправлено**
- Самопроверка (Настройки → Производительность) больше не засчитывается на GitHub как скачивание
  `deckdrop.py`: она проверяет, что отвечает страница релизов, а не сам файл. Тесты к GitHub
  вообще не обращаются, так что счётчик скачиваний снова показывает настоящие скачивания.

## 0.4.2 — 2026-09-28

### English

**Changed**
- An open page costs the Deck far less, and no longer grows with the size of your games. Each
  update used to look through every game's folders again; now a game is looked at only when its
  files change. Measured on a test library: 5 games 16 → 0.8 ms per update, 30 games 122 → 5 ms,
  100 games 311 → 16 ms, i.e. from about a third of a CPU core down to under 2% with 100 games.
- Free space and memory cards are checked once per update instead of once per game and task.
- A page in a hidden tab (a locked phone, another app or browser tab) stops asking the Deck for
  updates and catches up the moment it is shown again. A visible page still updates every second.

### Русский

**Изменено**
- Открытая страница нагружает дек гораздо меньше, и нагрузка больше не растёт с размером игр.
  Раньше каждое обновление заново перебирало папки всех игр, теперь игра просматривается, только
  когда её файлы изменились. На тестовой библиотеке одно обновление: 5 игр — 16 → 0,8 мс,
  30 игр — 122 → 5 мс, 100 игр — 311 → 16 мс, то есть при 100 играх с трети ядра до менее чем 2%.
- Свободное место и карты памяти проверяются один раз за обновление, а не для каждой игры
  и каждого задания.
- Страница в свёрнутой вкладке (телефон заблокирован, открыто другое приложение или вкладка)
  перестаёт опрашивать дек и сразу обновляется, когда её снова открывают. Видимая страница
  по-прежнему обновляется раз в секунду.

## 0.4.1 — 2026-09-27

### English

**Added**
- **Settings → Performance**, measured on the Deck itself:
  - **Self-check**: autostart, background tasks, the settings file, free space and writing on
    every disk, leftovers of downloads, Steam and its shortcuts, Steam control, queued Steam
    changes, games whose files are gone, unpacking tools, Mega decryption, ffmpeg, the internet,
    the update link, VNDB and errors in the log. Each item says what is wrong and what to do.
  - **Measure the load**: how much CPU DeckDrop and the whole Deck use right now, the Deck's
    power draw from the battery, the memory DeckDrop takes, what one page update costs, and the
    speed of Mega decryption, zip unpacking and writing to each disk.
  - **Copy report**: the result as text, for a message or a GitHub issue; the small **⤓**
    button downloads it as a Markdown file, laid out by the template `src/deckdrop/web/report.md`.

**Fixed**
- Rows of buttons in Settings (proxy, PIN) had a large empty gap under them.

### Русский

**Добавлено**
- **«Настройки» → «Производительность»**, всё измеряется на самом деке:
  - **Самопроверка**: автозапуск, фоновые задачи, файл настроек, место и запись на каждом диске,
    остатки загрузок, Steam и его ярлыки, управление Steam, отложенные изменения Steam, игры,
    чьи файлы пропали, распаковщики, расшифровка Mega, ffmpeg, интернет, адрес обновления, VNDB
    и ошибки в журнале. У каждого пункта написано, что не так и что делать.
  - **Замерить нагрузку**: сколько процессора сейчас берут DeckDrop и весь дек, потребление дека
    по датчику батареи, сколько памяти занимает DeckDrop, во что обходится одно обновление
    страницы, скорость расшифровки Mega, распаковки zip и записи на каждый диск.
  - **Скопировать отчёт**: результат текстом, для сообщения или issue на GitHub; маленькая
    кнопка **⤓** скачивает его файлом Markdown по шаблону `src/deckdrop/web/report.md`.

**Исправлено**
- Под рядами кнопок в настройках (прокси, PIN) была большая пустая полоса.

## 0.4.0 — 2026-09-27

### English

**Added**
- The interface is now in **English as well as Russian**: the whole page and every message
  from the Deck, including errors, download tasks, the installer and `--install`.
- The language is taken from **the Steam client on the Deck**, i.e. the language picked in the
  Deck's settings. SteamOS keeps its system locale in English, so DeckDrop reads Steam's own.
- **Settings → Language**: Auto (same as Steam), Русский or English. The choice applies to that
  phone or PC only, so different people can use different languages.
- A new language is one JSON file in `src/deckdrop/i18n/`; see the README.
- New READMEs: `README.en.md` and `README.ru.md` with screenshots, a demo GIF, a quick start
  and troubleshooting.

**Changed**
- Dates in the gallery and archives follow the page language.
- Gallery items of Steam recordings are named after the file; the tile badge shows the kind.

**Fixed**
- On Python 3.8 importing a game, Mega links, patches, custom artwork and save import failed
  with "unsupported operand" (SteamOS ships a newer Python, so Decks were not affected).

### Русский

**Добавлено**
- Интерфейс теперь **на английском и на русском**: вся страница и все сообщения дека, включая
  ошибки, задания загрузки, установщик и `--install`.
- Язык берётся **из клиента Steam на деке** — того, что выбран в настройках дека. Системная
  локаль SteamOS всегда английская, поэтому DeckDrop смотрит на язык самого Steam.
- **«Настройки» → «Язык»**: Авто (как в Steam), Русский или English. Выбор действует только на
  этом телефоне или ПК, так что у разных людей могут быть разные языки.
- Новый язык — это один JSON-файл в `src/deckdrop/i18n/`, см. README.
- Новые README: `README.ru.md` и `README.en.md` со скриншотами, GIF, быстрым стартом
  и разделом «Частые проблемы».

**Изменено**
- Даты в галерее и архивах выводятся на языке страницы.
- Записи Steam в галерее называются по имени файла; тип показывает значок на плитке.

**Исправлено**
- На Python 3.8 не работали импорт игры, ссылки Mega, патчи, свои обложки и импорт сейвов
  (ошибка «unsupported operand»). На деке стоит более новый Python, так что его это не касалось.

## 0.3.24 — 2026-09-27

### English

**Changed**
- DeckDrop is released through GitHub Releases: the "Update" button, `install.sh` and the
  README download the latest release. A stored update link that points at the old `main`
  branch moves to releases by itself; your own link is kept.
- The code is split into modules and built into the same single `deckdrop.py`; nothing changes
  for installing or updating.

**Added**
- Automatic checks before every release, including a test that updates earlier versions and
  makes sure no setting, password, game or file is lost.

### Русский

**Изменено**
- DeckDrop выходит через GitHub Releases: кнопка «Обновить», `install.sh` и README берут
  последний релиз. Сохранённый адрес обновления из старой ветки `main` сам переключается на
  релизы; свой адрес сохраняется.
- Код разбит на модули и собирается в тот же единый `deckdrop.py`; для установки и обновления
  ничего не меняется.

**Добавлено**
- Автоматические проверки перед каждым релизом, в том числе тест, который обновляет прошлые
  версии и следит, чтобы не потерялись настройки, пароли, игры и файлы.

## 0.3.23 — 2026-09-23

### English

**Added**
- The first public version: a LAN inbox and Steam helper for the Steam Deck. Downloads by link
  (Mega, Yandex Disk, Google Drive) and uploads, unpacking with passwords, adding to Steam with
  Proton and artwork (VNDB or the exe icon), game files and patches, save backups, archives and
  a password-protected media gallery.

### Русский

**Добавлено**
- Первая публичная версия: входящие по локальной сети и помощник Steam для Steam Deck.
  Скачивание по ссылкам (Mega, Яндекс.Диск, Google Drive) и загрузка файлов, распаковка
  с паролями, добавление в Steam с Proton и обложками (VNDB или иконка exe), файлы игры и патчи,
  бэкапы сейвов, архивы и галерея под паролем.
