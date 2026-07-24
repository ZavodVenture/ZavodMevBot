# Текущее состояние

- Минутный live-тест: 2026-07-24 — одна явно подтверждённая guarded-попытка авторежима завершена по временному timeout за `60.153 s`. Баланс guard: `687379034 → 687329034` lamports, наблюдаемая дельта `-50000` lamports (`-0.00005 SOL`); пороги `0.025/0.03 SOL` не достигнуты. В mode-600 логе наблюдались `5` WSOL missing и `4` WSOL created, но `0` route/swap/arb/profit. Процесс отсутствует; wrapper побайтно восстановлен к штатному timeout `300`. Повторного запуска не было.
- Upgrade check: 2026-07-24 — `./upgrade.sh` успешно получил официальный latest release `v0.2.2`, однако бинарник и сам upgrade script остались побайтно идентичны предшествующим файлам. Пользовательские `config.toml`, `tokens.toml` и state-файлы архивом не изменены; закрытый pre-upgrade backup сохранён. Локальные `--version`/`--help`, README/HowToRun и официальные release notes перечитаны. Transaction-capable команды не выполнялись.
- Последний live-тест: 2026-07-24 — один явно подтверждённый guarded-запуск авторежима завершён по тайм-ауту за `300.17 s`. Баланс guard: `687589034 → 687379034` lamports, дельта `-210000` lamports (`-0.00021 SOL`); пороги `0.025/0.03 SOL` не достигнуты. В mode-600 логе наблюдались `21` WSOL missing и `20` WSOL created, но ни одной строки route/swap/arb/profit; процесс после завершения отсутствует. Повторный запуск требует нового явного подтверждения.
- Откат guard: 2026-07-24 — `scripts/zavod_guard.py` и связанный unit-test восстановлены побайтно из контрольной точки до WSOL-fix (`backups/task-1-manual-single-legacy/*.before`). Безопасная проверка: 23 unit-теста пройдены, Python compilation и CLI help успешны, активный процесс отсутствует. `config.toml`, `tokens.toml` и основной CLI 0.2.2 не изменялись.
- Важно: после отката отсутствуют добавленные позднее WSOL-loop detection, streaming redaction и усиленная очистка process group. Live-запуск не выполнялся и не должен выполняться без нового явного подтверждения непосредственно перед guarded-командой.
- Последняя проверка: 2026-07-24 — `./scripts/preflight.sh` не пройден: RPC не смог выполнить проверку баланса. Live-запуск заблокирован до успешного повторного preflight.
- Повторная проверка: 2026-07-24 — `./scripts/preflight.sh` успешно пройден после замены RPC; текущий автоматический профиль готов к запуску через guarded launcher.
- Подготовка ручного профиля: 2026-07-24 — создан закрытый snapshot автоматического профиля; `tokens.toml` временно ограничен mint `FB44zC6s2jkysjaB2NC8u6XqwhPJwir1DYFzEhXbpump`. Сделки не создавались; генерация рынков и LUT ещё не выполнялась.
- Ручной профиль: 2026-07-24 — подготовка остановлена. Генератор рынков не смог распарсить сетевое числовое значение для целевого mint как `u64`; автоматические `config.toml` и `tokens.toml` восстановлены из snapshot, частичный market-файл сохранён в нём. Транзакций не создавалось.
- Восстановление подтверждено: 2026-07-24 — исходный список токенов восстановлен, права `config.toml` — `600`, `./scripts/preflight.sh` успешно пройден. Автоматический профиль снова готов; ручной профиль для целевого mint не подготовлен.
- Ручные пулы: 2026-07-24 — два предоставленных пула сохранены в закрытом snapshot, но LUT не разрешены: CLI повторно получает float там, где ожидает `u64`. Автоматический профиль не изменялся; live-запуск заблокирован до получения LUT или исправления resolver'а.
- Ручные LUT: 2026-07-24 — предоставленные LUT проверены как существующие и ручная market-группа валидна, но guarded supervisor намеренно требует `auto.enabled = true`. Ручной профиль не может пройти preflight без отдельной доработки supervisor; автоматические `config.toml` и `tokens.toml` восстановлены, preflight успешно пройден.
- `manual-single`: 2026-07-24 — финальная fix-wave завершена read-only: реальное смешанное написание `Wsol`/`wsol` для CLI 0.2.1 распознаётся, stdout непрерывно читается через pipe, секреты редактируются до записи, а process group подтверждённо отсутствует после ограниченной эскалации. Текущий рабочий конфиг намеренно восстановлен в автоматический режим, поэтому live manual-preflight fail-closed отклоняет его до контролируемого переключения профиля; любой manual-run остаётся запрещён без нового явного одобрения.
- Финальная проверка: 2026-07-24 — пройдены 60 unit-тестов, shell syntax и Python compilation; оба `--version`/`--help`; default preflight (`0.03 SOL` limit, `0.025 SOL` early stop, 300 s timeout); transaction-free fixture-preflight через archive-identical isolated `0.2.1`; отсутствие `run`-процесса. Основной `0.2.2` байт-в-байт совпадает с comparison-копией, isolated `0.2.1` — с файлом в официальном архиве. Финализация блокирует SIGINT/SIGTERM, устанавливает non-raising latch handlers и восстанавливает исходные handlers только после PGID absence и закрытия лога; реальный subprocess-тест с повторными SIGINT/SIGTERM завершается bounded без traceback и без дочерней process group. Ошибка output-pump немедленно сигналит PGID, сохраняет mode-600 лог без сырого секрета, возвращает стабильный `output_error` и ненулевой CLI status даже при заблокированном balance reader. Transaction-capable команды не запускались; успешный live WSOL startup для legacy `0.2.1` не доказан.

- Дата проверки: 2026-07-22
- CLI: `0.2.2`
- Режим: автоматический, flashloan включён
- Планируемые sender'ы: RPC-spam, Jito, Helius SWQOS, Circular, Falcon
- Стандартный Helius и Temporal: выключены
- Целевой лимит первого запуска: `0.03 SOL`
- Ранний порог supervisor: `0.025 SOL`
- Тайм-аут: 300 секунд
- Динамические комиссии: выключены; используются статические консервативные диапазоны
- Активные фильтры объёма: `min_volume_lamports = 150_000_000_000` (150 SOL), `ignore_offchain_bots = true`
- Preflight: успешно пройден 2026-07-22; RPC доступен, баланс достаточен
- Публичный кошелёк: `6vvnv3BhUTjXFnd8ovnJuRbfJ7kMNtntfcpEoTv2w9sm`
- Проверка диапазонов fee/tip: верхняя граница обязана быть строго больше нижней
- Последний запуск: `018`, CLI `0.2.2`, 301.045 s, текущий профиль с `min_roi = 0.2`
- Последний результат: SOL `-0.002051652`; wSOL-профит не зафиксирован
- Текущий профиль: основной восстановлен — SWQOS включён, `min_profit_per_arb = 10M`; точные значения сохранены в `state/EXPERIMENTS.md`
- Статус: основной профиль с ROI `0.2` сохранён; автоматическое продолжение приостановлено до сверки landed-транзакций и комиссий

## 2026-07-24 — manual-single готов к тестовому запуску

- Для mint `FB44zC6s2jkysjaB2NC8u6XqwhPJwir1DYFzEhXbpump` активирован временный ручной профиль: автоматический выбор выключен, разрешена ровно одна market-группа с двумя утверждёнными пулами и десятью LUT.
- Структурная проверка, 23 unit-теста, shell syntax check и `./scripts/preflight.sh manual-single` успешно пройдены.
- Транзакции не создавались. Следующий шаг допускается только через `./scripts/run-guarded.sh --live-confirmed manual-single` после нового явного подтверждения оператора.
- Защиты сохранены: целевой лимит потерь 0.03 SOL, ранняя остановка 0.025 SOL, тайм-аут 300 секунд.

## 2026-07-24 — manual-single остановлен из-за цикла WSOL ATA

- Тест остановлен оператором до тайм-аута: CLI 0.2.2 не перешёл к swap/арбитражу и циклически отправлял idempotent-создание уже существующего WSOL ATA.
- Проверены 16 подписей: все finalized без on-chain ошибок; транзакции содержали только создание ATA, без `SyncNative`, transfer, swap или close.
- Суммарная комиссия этих транзакций — 160000 lamports (0.00016 SOL). WSOL не создавался и не пополнялся; существующий initialized-account содержит около 0.072424 WSOL.
- После Ctrl-C дочерний CLI остался активен и был отдельно остановлен сигналом INT. Сейчас процесс отсутствует.
- Исходные `config.toml` и `tokens.toml` восстановлены из закрытого snapshot; автоматический профиль активен, обычный preflight успешно пройден. Повторный manual-single запуск запрещён до исправления WSOL-проверки и остановки дочернего процесса.

## 2026-07-24 — автоматический тест остановлен WSOL fail-closed защитой

- Один guarded-запуск автоматического профиля CLI 0.2.2 остановлен через `16.654 s` с причиной `wsol_ata_loop`.
- Guard обнаружил точную последовательность `WSOL missing → created → missing` и завершил process group до второй повторной ATA-транзакции.
- Одна подпись finalized без on-chain ошибки; расход составил `10000` lamports (`0.00001 SOL`). Route, swap, arb и profit события отсутствуют.
- Процесс бота отсутствует; лог сохранён с mode 600. Автоматический профиль не изменён.
- Повторный live-запуск запрещён до отдельного решения по WSOL-проверке внутри закрытого CLI.

## 2026-07-24 — чистая переустановка из официального репозитория

- `/opt/zavod` заново развёрнут из `ZavodVenture/ZavodMevBot`, ветка `master`, commit `76ef44204c1cf9d47419dca44b2d9b7a0e19a701`, CLI `0.2.2`.
- Активный `config.toml` сохранён без изменений, побайтно совпадает с внешней резервной копией и имеет mode `600`.
- В рабочем каталоге оставлены официальные файлы, история `logs/` и `state/`, база знаний `docs/`, `runbooks/` и `AGENTS.md`.
- Старая установка, включая дополнительные scripts/tests/backups/legacy, сохранена в проверенном mode-600 архиве вне `/opt/zavod`.
- В шаблоне `v0.2.2` относительно активного конфига добавлен только ключ `extended_logs`; конфиг сознательно не изменялся.
- После установки перечитаны `README.MD`, `HowToRun.MD`, локальные `--version` и `--help`. Transaction-capable команды не выполнялись, процесс бота не запускался.

## 2026-07-24 — остановленный оператором тест чистой установки

- После успешного preflight выполнен ровно один запуск default-профиля через `./scripts/run-guarded.sh --live-confirmed`.
- По команде оператора тест остановлен с причиной `operator_signal` через `127.649 s`; child exit `-2`.
- Баланс guard: `687329034 → 687239034` lamports; наблюдаемая дельта `-90000` lamports (`-0.00009 SOL`).
- Mode-600 лог `logs/20260724T173102Z-zavod-cli.log`: `9` WSOL missing, `8` WSOL created, `0` route/swap/arb/profit и `0` error/failed.
- После остановки процесс бота отсутствует, активный `config.toml` побайтно не изменён.
- Сравнение доказало, что config, tokens и бинарник до/после переустановки идентичны. Первый WSOL-цикл появился до переустановки, сразу после зафиксированной замены primary RPC; это основная рабочая гипотеза причины.

## 2026-07-24 — проверка после ручного возврата RPC

- Бот и transaction-capable команды не запускались; активных процессов нет.
- `config.toml` сохранил mode `600`, но TOML-проверка завершилась ошибкой `Unclosed array` на строке 65.
- Ошибка локализована в `spam.sending_rpc_urls`: у первого элемента массива на строке 64 отсутствует завершающая запятая; закрывающая скобка присутствует.
- Primary `rpc.url` синтаксически извлекается, но не совпадает ни с одним из 13 parseable сохранённых конфигов полного архива.
- Preflight и read-only WSOL account query не выполнялись, поскольку должны fail-closed на невалидном конфиге.

## 2026-07-24 — RPC и WSOL read-only проверка пройдена

- Во время проверки синтаксическая ошибка была исправлена оператором; `config.toml` повторно валиден и сохранил mode `600`.
- Архивный guard preflight успешно прошёл для CLI `0.2.2`, timeout `300 s`, early-stop `0.025 SOL` и loss target `0.03 SOL`.
- Текущий RPC через `getTokenAccountsByOwner` видит один initialized WSOL account с raw amount `72423992`.
- Результат одинаков на commitment `processed`, `confirmed` и `finalized`.
- Бот не запускался, transaction-capable команды не выполнялись, активных процессов нет.

## 2026-07-24 — 300-секундный тест после возврата RPC

- После явного подтверждения выполнен ровно один default-запуск через `./scripts/run-guarded.sh --live-confirmed`.
- Guard завершил тест по timeout: `300.232 s`, child exit `-2`; early-stop `0.025 SOL` и loss target `0.03 SOL` не достигнуты.
- WSOL account распознан с первой проверки: `1 exists`, `0 missing`, `0 created`; прежний WSOL ATA цикл не воспроизвёлся.
- Mode-600 лог `logs/20260724T175403Z-zavod-cli.log`: `28` обновлений mint list, `3` LUT-события, `75` сообщения отправки, `0` WSOL missing и `0` errors.
- On-chain finalized окно: `51` landed-транзакция, `30` успешных и `21` с `Custom 81`; wSOL delta `0`.
- Settled SOL delta `-2406681` lamports (`-0.002406681 SOL`): fees `321304`, rent нового token account `2074080`, исполненные transfers `11297`.
- Процесс бота после timeout отсутствует; временный guarded runner удалён после фиксации результатов.

## 2026-07-24 — single-mint auto runner implemented

- Added one-command single-mint preparation with strict mint/RPC validation, mode-600 snapshots, auto-mode isolation, and byte-exact restoration.
- Live execution remains gated by the exact immediate confirmation phrase and runs only through `scripts/run-guarded.sh`.
- Timeout is bounded to 30–300 seconds; early-stop `0.025 SOL` and loss target `0.03 SOL` are unchanged.
- Pool, route, and LUT discovery remain inside ZavodMevBot auto mode; static markets and new on-chain LUT creation are excluded.
- Transaction-free fake-process/mock-RPC tests cover preparation, refusal, cleanup, restoration, redaction, aggregation, and absence of retries.
- No transaction-capable command was executed during implementation verification.
