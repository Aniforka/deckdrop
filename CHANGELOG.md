# Changelog · Что нового

Every version has a section here, in English and in Russian. The GitHub release of a version
takes its description from its section, so a version cannot be released without one.

У каждой версии здесь свой раздел, на английском и на русском. Описание релиза на GitHub
берётся из этого раздела, поэтому без него версия не выйдет.

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
