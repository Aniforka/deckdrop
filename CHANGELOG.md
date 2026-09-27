# Changelog · Что нового

Every version has a section here, in English and in Russian. The GitHub release of a version
takes its description from its section, so a version cannot be released without one.

У каждой версии здесь свой раздел, на английском и на русском. Описание релиза на GitHub
берётся из этого раздела, поэтому без него версия не выйдет.

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
