# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — [SemVer](https://semver.org/lang/ru/).
Format: Keep a Changelog; sections are RU with short EN where useful.

Релизы — только portable (`InviterStudioPortable.zip` + `SHA256.txt`), без подписи кода.
Releases are portable-only (ZIP + SHA-256), unsigned.

## [Unreleased]

## [1.2.25] — 2026-10-27

### Добавлено
- Сводки через собственного Telegram-бота: токен хранится через DPAPI, chat_id вводится или определяется кнопкой; канал: выкл / «Избранное» / бот / оба. Только счётчики. / Summaries via the user's own Telegram bot (DPAPI token, counters only).
- Очередь кампаний: автопродолжение — следующая кампания запускается автоматически. / Queue auto-continue.
- Динамический бюджет авторов сообщений при парсинге (делится между аккаунтами). / Dynamic chat-authors budget.
- Светлая и тёмная тема. / Light and dark theme.
- Справка и интерфейс новых экранов на английском (EN help + i18n). / EN help and i18n for the new screens.

### Изменено
- Портативный ZIP стал меньше. / Smaller portable ZIP.

## [1.2.24] — 2026-10-27

### Добавлено
- Follow-up: проверка истории чата перед отправкой второго сообщения и «догон» диалогов после перезапуска/простоя. / Follow-up history verify and dialog catch-up.
- Парсинг авторов сообщений с несколькими аккаунтами. / Multi-account chat authors parsing.
- Дашборд конверсии: отправлено → ответы → вступления по кампаниям. / Conversion dashboard.
- Очередь кампаний: несколько задач запускаются последовательно, выбор из очереди. / Campaign queue.
- A/B-тексты сообщений и импорт/экспорт кампаний в JSON. / A/B message texts and campaign JSON import/export.
- Мастер первого запуска, интерфейс RU/EN (i18n), сводка кампании в «Избранное» (Saved Messages). / First-run wizard, EN/RU i18n, Saved Messages summary.
- `CHANGELOG.md` и GitHub Actions workflow `release.yml`: сборка portable по тегу `v*` или вручную (`workflow_dispatch`), публикация ZIP и `SHA256.txt` в GitHub Release. / Release automation, no signing secrets.

### Изменено
- Заметки релиза на GitHub берутся из секции версии в этом файле. / GitHub release notes are taken from the matching section here.
- Обновлены справка и README под новые возможности.

## [1.2.23] — 2026-10-27

### Добавлено
- Пресеты темпа (Безопасный / Обычный / Быстрый) и суточный лимит кампании; предупреждение перед агрессивными настройками. / Pacing presets and campaign daily cap.
- Расписание: дни недели, часы работы, умный интервал по локальному времени или по Москве. / Weekday and hours schedule, local or Moscow time.
- Парсинг с несколькими аккаунтами: источники перераспределяются при FloodWait; diff «Новые с прошлого запуска»; аудитория по `id:` без никнейма. / Multi-account parsing, run diffs, ID-based audience.
- Опция «только прогретые аккаунты» (warmup-gate). / Warmed-up accounts only option.

### Изменено
- Ожидающие вторые сообщения (follow-up) переживают перезапуск программы (доставка — пока программа открыта). / Durable follow-ups across restarts.
- Неизвестный результат отправки сверяется с историей чата. / Uncertain sends are verified against chat history.
- Первые и вторые сообщения делят один дневной лимит аккаунта. / Shared daily account limit.
- README и справка обновлены под новые чеклисты.

### Исправлено
- Замечания Ruff и форматирование, блокировавшие quality gate сборки.

## [1.2.22] — 2026-10-27

### Добавлено
- Конвейер парсинга: продолжение после обрыва, расширенные фильтры (скам/фейки/админы), экспорт XLSX/CSV/JSON/TXT. / Parsing pipeline with resume, filters, exports.
- Журнал приглашённых (ledger) и исключение уже приглашённых / уже в целевом чате. / Invited ledger.
- Мост «закрытым — написать со ссылкой» (private → DM). / Private-to-DM bridge.
- Шаблоны сообщений: `{имя}`, `{a|b|c}`, предпросмотр, «Тест в Избранное», оценка срока; ручная отметка «Ушло / Не ушло». / Templates, preview, manual resolve of uncertain sends.

### Изменено
- Усиление рассылки: ошибки отдельного получателя не валят кампанию; FloodWait ставит на паузу только затронутый аккаунт; классификация RPC-ошибок; задержка второго сообщения после ответа; подтверждение остановки при отмене follow-up. / Messaging hardening.
- Более мягкий темп ЛС (60–120 с, минимум 45 с).
- Возраст аккаунта влияет на дневной лимит; бэкап при старте и повторный вход существующего номера не блокируют запуск.

### Исправлено
- Ruff-ошибки, мешавшие сборке portable 1.2.22.

## [1.2.21] — 2026-10-26

### Изменено
- Portable — единственный формат поставки: убраны кнопка обновлений и EXE-установщик; обновление через `update.bat` (папка `profile` не затрагивается). / Portable as the only distribution format.
- `start.bat` сам снимает блокировку «скачано из интернета» (`Unblock-File`). / Auto-unblock on launch.
- Пользовательские данные (`profile`, `data`, `sessions`, `*.db`, `.env`) не попадают ни в git, ни в ZIP; сборка проверяет это. / User data never ships.

[Unreleased]: https://github.com/SalazarUltra/inviter-studio/compare/v1.2.25...HEAD
[1.2.25]: https://github.com/SalazarUltra/inviter-studio/releases/tag/v1.2.25
[1.2.24]: https://github.com/SalazarUltra/inviter-studio/releases/tag/v1.2.24
[1.2.23]: https://github.com/SalazarUltra/inviter-studio/releases/tag/v1.2.23
[1.2.22]: https://github.com/SalazarUltra/inviter-studio/releases/tag/v1.2.22
[1.2.21]: https://github.com/SalazarUltra/inviter-studio/releases/tag/v1.2.21
