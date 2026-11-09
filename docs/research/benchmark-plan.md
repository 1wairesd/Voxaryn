# benchmark-plan.md — Воспроизводимая методика будущих измерений

> Фаза 1, задача 4 + **обновление фазы 1.5**. Опирается на существующие инструменты
> Voxaryn. Результаты не выдумываются.
>
> **Обновление фазы 1.5:** окружение настроено (Python 3.14 `py -3`, JDK 25, ресурсы
> `.local/horizon-client/26.3`), и **Windows-адаптация выполнена и применена** —
> `.local/tick_bench.py` даёт реальные MSPT/фазовые замеры без Unix-only
> `server_bench.py`. Первые измерения — в `profiling-results.md`. Разделы ниже
> уточнены: см. §6 (что теперь доступно, что ещё нет).

## 1. Что уже есть в репозитории (проверено)

| Инструмент | Файл | Что меряет | Платформа |
|---|---|---|---|
| **Windows tick-бенч (фаза 1.5)** | `.local/tick_bench.py` (не в Git) | MSPT (avg/P50/P95/P99), TPS, разбивка фаз из `HORIZON_SPIKES`; без игроков | Windows ✅ |
| Серверный бенч (боты vs vanilla/paper/fabric) | `tools/server_bench.py` | MSPT (avg/P50/P95/P99), TPS, RSS/CPU дерева процессов, чанки/с, pull-backs, время старта | Unix (`ps`, `ssh`) |
| Графический бенч клиента | `tools/perf_bench.py`, `docs/benchmark.md` | FPS, 1%/0.1% low, p95/p99 кадра, RSS, GPU-память | кросс (нужен vanilla) |
| Встроенный бенч игры | `horizon-client --benchmark standard` | старт, загрузка чанков, кадры, память | кросс |
| Формат отчёта | `crates/horizon-bench`, `docs/benchmark-report.md` | единый JSON (Summary со stdev/ci95, гистограммы кадров) | кросс |
| Потиковый профайлер фаз | `horizon_client::spans` + `HORIZON_SPIKES=MS` | разбивка тика по спанам (`tick_blocks`, `tick_mobs`, …) | кросс |
| JFR-профиль (сравнение с Java) | `docs/profiling.md`, `tools/JfrSummary.java` | аллокации/CPU по потокам (для vanilla) | нужен JDK |
| `/tick query` | движок (как vanilla `ServerTickRateManager`) | MSPT avg + P50/P95/P99 | кросс |

**Эталон методики** уже написан и воспроизводим: `benchmarks/server-2026-10-17/README.md`
(боты walk/explore, equal-work, 1–100 ботов, 60 с warm-up + 180 с измерение, `nice -n 19`,
боты на отдельной машине). Будущие замеры многопоточности должны **встраиваться в этот
же протокол**, чтобы цифры сравнивались с историческими.

## 2. Метрики и как их снимать

| Цель задачи | Метрика | Источник |
|---|---|---|
| MSPT и TPS | mean/P50/P95/P99 MSPT, TPS | `/tick query` каждые 5 с (как в `server_bench.py::tick_query`) |
| AI и pathfinding | доля `tick_mobs`/`tick_creatures` в тике | `HORIZON_SPIKES` спаны + perf/JFR |
| Entity physics | доля `tick_items/tick_carts/tick_falling/tick_tnt` + движение | спаны + perf |
| Block и block-entity ticking | доля `tick_blocks`, `tick_block_entities`, `tick_scheduled` | спаны `tick_world` |
| Загрузка CPU и масштабирование по ядрам | CPU % дерева процессов, load average, perf по потокам | `server_bench.py` (CPU), perf record |
| Память | RSS mean/peak | `server_bench.py` (sampled 1 Гц) |

**Разбивка тика уже встроена:** каждый шаг `tick_world` обёрнут в
`horizon_client::spans::time("<name>", ...)`; `tick_server_timed` при `HORIZON_SPIKES=MS`
логирует медленные тики со спанами и числом мобов. Это основной бесплатный инструмент
для задач 4-метрик (AI, physics, block ticking) **без** внешних профайлеров.

## 3. Воспроизводимые сценарии (привязка к `server_bench.py`)

Фиксированные условия (как в эталонном README): `level-seed=12345`, view/sim distance 10,
creative forced, `online-mode=false`, свежий мир на каждый прогон, боты по одному в секунду,
warm-up 60 с, измерение 180 с, оба сервера под `nice -n 19`, боты на отдельной машине.

### S1 — Entity/AI нагрузка (mob-heavy)
Боты `walk` вокруг спавна с принудительным спавном мобов (геймрул), чтобы нагрузить
`tick_creatures`/AI/pathfinding. Варьировать 1/10/25/50/100 ботов.
Снимать: MSPT, доля `tick_mobs` в спанах, CPU %.
*Гипотеза H-AI: при >25 мобов `tick_mobs` — доминирующая фаза тика.* (Проверяется прогоном,
пока не подтверждена.)

### S2 — Block/Redstone ticking
Мир с плотными redstone-часами и случайными тиками (растущие культуры/листва) в
загруженных чанках. Нагрузка — на `tick_blocks`/`tick_scheduled`.
Снимать: доля `tick_blocks`, `tick_block_entities`.
**Урок фазы 1.5:** статическое поле `fill redstone_wire`/`redstone_block` НЕ создаёт
устойчивых тиков (зажигается один раз). Нужен реальный осциллятор — observer-clock,
поршневой/компараторный таймер или repeater-петля, — иначе блок-фазы останутся
< 0.5 мс и не атрибутируются (как в `profiling-results.md` сценарий 5).

### S3 — Entity physics (предметы/вагонетки/TNT)
Массовый дроп предметов и вагонетки (нагрузка `tick_items`/`tick_carts`/`tick_falling`).
Снимать: доля соответствующих спанов.

### S4 — Equal-work explore (чанк-генерация)
`--explore-speed 0.8` (как в эталоне): боты летят с одинаковой скоростью — равная работа,
сравнимые тики. Нагрузка — worldgen-воркеры + стриминг. Проверяет масштабирование
фоновых потоков (не самого тика).

### S5 — Масштабирование по ядрам
Любой из S1–S3 при искусственном ограничении ядер (`start /affinity` на Windows,
`taskset` на Linux) — снять MSPT при 1/2/4/8/… ядрах. Для однопоточного тика (текущее
состояние) кривая должна быть **плоской**: это baseline, относительно которого позже
измеряется ускорение от параллелизма.

## 4. Протокол сравнения «до/после» параллелизма

1. Зафиксировать коммит baseline (однопоточный тик) и собрать release.
2. Прогнать S1–S5, сохранить JSON-отчёты в `benchmarks/<date>-baseline/`.
3. Реализовать прототип (см. `parallelization-plan.md`), собрать release тем же профилем.
4. Прогнать те же S1–S5 на той же машине, сохранить в `benchmarks/<date>-proto/`.
5. Сравнить mean/P95 MSPT с доверительным интервалом (формат `horizon-bench` даёт
   stdev и ci95; «различия внутри интервалов — шум», см. `docs/benchmark.md`).
6. **Обязательно:** проверить, что `--explore-speed`/равная работа соблюдена (иначе
   сравнение нечестное — правило `EQUAL_WORK = 10%` в `server_bench.py`).

Правило сравнения кадров/тиков из `docs/benchmark.md`: каждая конфигурация — N повторов
(по умолчанию 3) со свежего мира, цифра — среднее ± ci95 (Student t). Lows — самые шумные.

## 5. Детерминизм как часть бенчмарка

Поскольку цель — сохранить корректность, к перф-прогонам добавляется проверка
детерминизма (не выдумывать «зелёный»):
- Прогнать фиксированный сценарий (seed 12345, скриптованные действия через
  `horizon-ctl`/`net_harness`) дважды в однопоточном режиме → сравнить итоговый NBT
  региона/сущностей (`save_roundtrip`-подобная сверка). Это эталон.
- Тот же сценарий в многопоточном прототипе → сравнить с эталоном. Расхождение = баг
  порядка/детерминизма.
- Где есть ресурсы и Python — прогнать затронутые parity-харнессы (`tools/parity.py
  --changed-files`), т. к. vanilla — спецификация порядка RNG.

## 6. Статус измерений (обновление фазы 1.5)

### Что теперь доступно и выполнено ✅
- Python 3.14 (`py -3`), JDK 25, ресурсы `.local/horizon-client/26.3` — готовы
  (см. `baseline.md` §0).
- **Windows-адаптация `server_bench.py` сделана:** `.local/tick_bench.py` (не в Git).
  Она заменяет Unix-части: вместо `ps`/`ssh` драйвит выделенный сервер через stdin,
  читает MSPT из `/tick query`, строит сценарии консольными командами и парсит
  `HORIZON_SPIKES`-спаны для разбивки фаз. **Первые реальные MSPT/фазовые измерения —
  в `profiling-results.md`** (все пять сценариев, 20 TPS, `tick_mobs` ≈ 60% под
  нагрузкой).

### Что ещё не сделано / ограничения ⚠️
- **CPU-vs-wall на Windows недоступен:** `spans::thread_cpu()` возвращает `None`
  (`crates/horizon-client/src/spans.rs`). Нельзя отделить «считал» от «ждал лок» —
  критично для оценки стоимости синхронизации будущего параллелизма. Решения:
  профилировать на Linux/macOS, либо внешним профайлером (ETW/VTune) на Windows.
- **`spans::describe()` показывает только спаны ≥ 0.5 мс** — лёгкие фазы не
  атрибутируются. Для разбивки `tick_mobs` на AI/pathfinding нужна временная
  инструментализация под-шагов (добавочные `spans::time` внутри `tick_creature_entities`).
- **Мультиплеерный бенч с ботами (`server_bench.py`) и сравнение с vanilla** всё ещё
  требуют Unix (боты `horizon-bots`, `ps`, `ssh`) либо дальнейшей адаптации. Пока
  измерения — без игроков, сущности расставлены командами.
- **`tools/parity.py`-оркестратор** не идёт на Windows (`fcntl`); отдельные
  `test_*_parity.py` запускаются напрямую (tick-parity так и пройден).
- **vanilla-эталон (`perf_bench`)** не прогонялся: нужен полный драйвер и тот же
  протокол, что в `benchmarks/server-2026-10-17/`.

### Минимально необходимое для полного S1–S5 с ботами и vanilla
1. Собрать `cargo build --release -p horizon-client -p horizon-server -p horizon-bots`.
2. Либо прогнать `server_bench.py` на Linux/macOS-раннере, либо доадаптировать его
   process-замеры под Windows (`Get-Process`/`Get-Counter`) и запуск ботов локально.
3. Для разбивки AI/pathfinding — временная инструментализация под-шагов `tick_mobs`.
4. Для CPU-времени — профилировать на Unix или внешним профайлером.

## 7. Источники

- `tools/server_bench.py`, `tools/perf_bench.py`, `tools/JfrSummary.java` (прочитаны).
- `crates/horizon-bench/src/lib.rs`, `docs/benchmark.md`, `docs/benchmark-report.md`,
  `docs/profiling.md` (прочитаны).
- `benchmarks/server-2026-10-17/README.md`, `docs/server-levels.md` — исторические
  измерения на других машинах (не воспроизведены здесь).
- `horizon_client::spans` / `HORIZON_SPIKES` — из кода `game.rs`/`game/levels.rs`.
