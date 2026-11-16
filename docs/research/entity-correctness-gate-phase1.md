# Entity Correctness Gate — Phase 1

Вердикт: READY FOR AUDIT (игровое end-to-end подтверждение ещё не выполнено).

Worktree: D:/Data/plugins/BatMine/Voxaryn-wt-entity-gate
Base: fb3a52ffbd878b3f7f2bcc580adc60f9e211fe12, detached HEAD.
Основной checkout и его три пользовательских untracked-файла не изменялись.

## Цепочка и причины ложного PASS

entity_bench.main -> run -> Server/Popen -> latest.log -> summarize_parstats ->
JSON results -> завершение main. Раньше исключение run записывалось как error,
цикл продолжался, main возвращал None (exit 0). Отсутствие PARSTATS давало {},
никакого evaluator не было. Crash/timeout во время ожидания попадали в тот же
обработчик. Повреждённый/отсутствующий JSON сам старый driver не читал: это
проблема потребителя evidence, а не отдельный путь успешного парсинга driver.
Ошибки записи JSON ранее и сейчас дают nonzero через исключение.

Другие пробелы: mismatch только записывался, неисполненные parallel/verified
passes не проверялись; инъекция могла не сработать; fallback и неполная сцена
не имели явного контракта. wait_log мог принять старую строку успеха до
проверки уже завершившегося процесса.

## Обязательные проверки

verify вызывает явный evaluator: завершённый запуск и clean shutdown,
readback gamerules, отсутствие setup errors, наблюдаемая популяция,
обязательные числовые метрики, отсутствие mismatch и NOT redone,
полностью распознанные строки PARSTATS, parallel>0 и verified==parallel.
VERIFY sampling >1 для этого режима запрещён. count_* без ответа и
запрошенный, но отсутствующий steady результат дают инфраструктурный отказ.

Контракты:
- parallel: положительные threads, parallel/verified passes обязательны,
  неожиданные fallbacks запрещены.
- sequential: только threads=0; PARSTATS не требуются.
- fallback: положительные threads, passes>0, parallel=0 и непустой fallback.
  Это широкий контракт fallback, пока без проверки конкретного reason.
- injection: attack:N или target:N; обязательны marker инъекции и её result
  с tripped true, redone true, unapplied events 0, MobHit events 1.
  Fallback после срабатывания защиты допустим. pushed_attack пока не поддержан.

Коды: PASS=0, FAIL=1 (игровой инвариант), INFRA ERROR=2 (неполное доказательство,
ошибка процесса/среды). argparse errors тоже nonzero. Приоритет INFRA ERROR
не уничтожает другие проваленные assertions: все checks остаются в JSON.
Bench без --expect сохраняет прежнюю семантику измерений, но ошибка run даёт
nonzero. Performance thresholds не добавлены.

## Команды из корня worktree

```powershell
python tools/voxaryn/entity_gate.py --exe X.exe --scenarios herds_8x250 --threads 6 --expect parallel --out .local/entity-gate/verify.json
python tools/voxaryn/entity_gate.py --exe X-unsafe.exe --scenarios herds_8x250 --threads 6 --expect injection --env HORIZON_ENTITY_UNSAFE_INJECT=attack:50 --out .local/entity-gate/injection.json
python tools/voxaryn/entity_gate.py --exe X.exe --scenarios riders_leash --threads 6 --expect fallback --out .local/entity-gate/fallback.json
```

Ожидания инъекции зависят от реальной сцены и unsafe build: команды выше —
способ запуска, не заявленные успешные игровые прогоны. Если guard выключен,
обычный контракт injection обязан отказать. Контроль, намеренно допускающий
mismatch, не представлен как успешная correctness проверка.

## Артефакты и совместимость

Старые поля JSON сохранены; добавлены mode, source_sha, complete, status,
per-row evaluation/checks, process_exit и artifact_dir. Каждая игровая попытка
получает UUID-каталог (старые миры не удаляются), stdout и latest.log сохраняются.
Частичные метрики сохраняются вместе с exception. JSON во время цикла помечен
complete=false; прерывание не выдаёт законченный PASS. read_result отвергает
отсутствующий/повреждённый/пустой/незавершённый документ. Это локальный reader,
не аттестация происхождения стороннего JSON и не publication service.

source_sha — SHA исходного checkout, НЕ доказательство, что переданный exe
собран из него. Перед внешней обязательной игровой CI проверкой runner должен
собрать exe из github.sha, записать build provenance и сохранить manifest.
Зелёный результат с произвольным локальным exe не является release attestation.

## Windows

Новый gate/evaluator не импортирует POSIX parity runner. Локальные unit tests
выполнены на Windows Python 3.14.3. parity.py остаётся ограничен fcntl, resource,
killpg и Java symlink wrappers. Исправление одного fcntl недостаточно; перенос
всей цепочки отложен. Минимальный путь сейчас — прямой Entity gate на Windows,
Vanilla parity на Linux runner с подготовленными ресурсами. Horizon не менялся.

## CI

В ci.yml добавлен самостоятельный Entity gate tooling job для Ubuntu и Windows,
Python 3.12, без assets/secrets. Он запускает настоящие регрессионные unit tests,
а не фиктивный игровой PASS. Checkout относится к SHA workflow. Нужность checks
в ruleset отдельно настраивается владельцем; настройки репозитория не менялись.

Игровой CI не подключён: доступного подготовленного runner не подтверждено.
План: отдельный изолированный runner, checkout exact SHA, сборка parallel и
unsafe-test exe, подготовленные личные ресурсы, запуск двух команд выше,
upload JSON/stdout/logs с always(), nonzero блокирует job. Не использовать
continue-on-error для итогового verdict. Performance оставить вне gate.

## Проверки

До исправления test_launch_failure_exit_regression запущен против source из
`git show HEAD:tools/voxaryn/entity_bench.py`: FAILED, AssertionError 0 == 0.
После: Entity tests 13 tests OK (10 новых и 3 steady), parity_runner 20 OK
(1 skipped), pr_ready 17 OK. Black недоступен для завершённой проверки в этой среде (процесс зависает); синтаксис проверен через ast.parse;
унаследованный большой benchmark-файл целиком не переформатирован.
Git diff --check выполнен. Rust не менялся — Cargo/Rust tests не запускались.

Примеры evaluator fixtures: полные verified/parallel counters -> PASS;
mismatches=1 -> FAIL; missing metric/crash/timeout -> INFRA ERROR;
parallel=0 с контрактом parallel -> FAIL; с ожидаемым fallback -> PASS.
Проверка из CLI с отсутствующими prerequisites даёт INFRA ERROR и exit 2.
Это проверка честного отказа, не игровой тест.

Настоящие VERIFY и fault injection не выполнялись: в новом worktree нет
resources, client/bots binaries. Старые бинарники основного checkout не
использованы как доказательство текущего source. Полный Python suite не
запускался; проверены релевантные наборы. P3.1 Diagnostics не запускался.

## Остаточные ограничения для аудитора

- VERIFY доказывает только покрытые reported passes, не все тики истории;
  PARSTATS агрегированы, partial tail менее 20 passes может не попасть в counters.
- Injection проверяется по существующему текстовому контракту. Требуется
  реальный protected run для проверки совместимости формата и ожиданий.
- Смена формата PARSTATS приводит к fail-closed, может требовать обновления parser.
- Сохранённые каталоги требуют ручной/внешней квоты; автоматический GC не добавлен.
- Полного Windows process-tree supervision/внешнего deadline всего driver нет;
  существующие ожидания ограничены timeout, launcher/wait cleanup унаследованы.
- Подтверждённая exit-code регрессия исправлена; полноценная готовность игровых
  сценариев к публикации пока не заявлена.

Commit/push/merge/rebase не выполнялись. Изменения оставлены для независимого аудита.
