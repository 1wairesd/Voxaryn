# baseline.md — Состояние сборки, проверки, ограничения среды

> Фаза 1, задача 2 + **обновление фазы 1.5**. Всё ниже — реально выполненные команды
> на этой машине (2026-11-06). Где проверка не запускалась — сказано явно; результаты
> невыполненных тестов не выдумываются.

## 0. Обновление фазы 1.5 (окружение теперь настроено)

Ключевые ограничения фазы 1 **сняты**:

| Что было (фаза 1) | Стало (фаза 1.5) |
|---|---|
| Python не найден | ✅ `py -3` → **Python 3.14.3** (pip 25.3). `python`/`python3` остаются Store-заглушками — использовать `py -3`. |
| JDK 21 (нужен 25) | ✅ установлен **Temurin JDK 25.0.5+8** через `winget EclipseAdoptium.Temurin.25.JDK` в `C:\Program Files\Eclipse Adoptium\jdk-25.0.5.8-hotspot`. Задавать `JAVA_HOME` на него. |
| Нет игровых ресурсов | ✅ **подготовлены** `.local/horizon-client/26.3` (23076 файлов). |

**Как получены ресурсы (легально, из собственной копии игрока):**
- На машине установлен официальный Minecraft 26.3 (`%APPDATA%\.minecraft\versions\26.3`);
  SHA1 его `26.3.jar` **совпадает** с пиннингом `client_sha1`.
- `server.jar` скачан с **официального URL Mojang** из `versions/26.3/26.3.json`
  (`piston-data.mojang.com/.../33680f5f….../server.jar`), SHA1 совпал с `server_sha1`.
  Это документированный путь (`lab.py` `SERVER_HINT`), не сторонняя сборка.
- `.local/` в `.gitignore` (`/.local/`) — ресурсы Mojang **не попадут в Git**
  (`git check-ignore .local` подтверждает).

**Правка `research-lock.json` (единственный изменённый файл в репо):** лаунчер этой
машины держит переизданный `26.3.json` с manifest sha1
`80608ff6753705b6a82e2cb86a5937d4d233abd4` и assetIndex sha1
`331e5c80937c9cd79647ab34bdc1cbb5ef09f456`, которых не было в allowlist.
**Хэши самих jar (client/server) идентичны пиннингу** — различается только assetIndex
(переводы), ровно как предупреждает заметка в lock-файле. Добавлены в
`accepted_manifest_sha1` / `accepted_asset_index_sha1`. Это lock/allowlist-файл, **не
производственный код**; верификация самих jar не ослаблена (они по-прежнему проверяются
по точным `client_sha1`/`server_sha1`).

## 1. Среда

| Инструмент | Версия | Статус | Источник |
|---|---|---|---|
| OS | Windows (win32), shell PowerShell | — | окружение |
| rustc | `1.99.1 (58cf8f9c3 2026-10-13)` | ✅ ≥ `rust-version = "1.85"` из `Cargo.toml` | `rustc --version` |
| cargo | `1.99.1 (5f94df478 2026-08-27)` | ✅ | `cargo --version` |
| host toolchain | `stable-x86_64-pc-windows-msvc` (default), также установлен `nightly-2026-08-21` | ✅ | `rustup show` |
| target | только `x86_64-pc-windows-msvc` | ⚠️ нет `wasm32-unknown-unknown` (нужен для `horizon-mods`) | `rustup show` |
| rustfmt | `1.10.0-stable` | ✅ | `rustfmt --version` |
| clippy | `0.1.99` | ✅ | `cargo clippy --version` |
| Python | **`py -3` → 3.14.3** (pip 25.3); `python`/`python3` — Store-заглушки | ✅ (фаза 1.5) | `py -3 --version` |
| Java (JDK) | **Temurin 25.0.5+8** (и 21.0.10) | ✅ major 25 (фаза 1.5) | `java --version` |
| Node.js | `v24.14.0` | ✅ (нужен только для `website/`) | `node --version` |
| Git | `2.45.1.windows.1` | ✅ | `git --version` |
| CPU | Intel i5-12400F, 6 ядер / 12 потоков | — | `Win32_Processor` |

### Классификация проблем среды

- **Инструменты (блокирует весь Python self-check):** Python 3.10+ отсутствует. Это
  отключает `tools/pr_ready.py`, `tools/parity.py`, `tools/server_bench.py`,
  `tools/perf_bench.py`, `tools/lan_e2e.py` и все `tools/test_*_parity.py`. Это не
  дефект кода Voxaryn.
- **Инструменты (платформа):** `tools/server_bench.py` и `tools/perf_bench.py`
  используют Unix-утилиту `ps` (`ps -A -o pid,ppid,rss,time`) для замера RSS/CPU и
  предполагают `ssh`-доступ к хосту ботов. На Windows они в текущем виде не работают
  без адаптации (замена на `Get-Process`/`Get-Counter`). Для фазы 1 это ограничение,
  не ошибка.
- **Инструменты (платформа, фаза 1.5):** `tools/parity.py` (оркестратор) использует
  **`fcntl`** (Unix-only) в `machine_slot()` → падает на Windows с
  `ModuleNotFoundError: No module named 'fcntl'`. **Обход:** запускать отдельные
  `tools/test_<name>_parity.py` напрямую (у них свой `main`), минуя оркестратор. Так и
  был пройден tick-parity (§2.7).
- **Игровые ресурсы — фаза 1: отсутствовали, фаза 1.5: подготовлены** (см. §0).
  `.local/horizon-client/26.3`, декомпилят и vanilla-джарники git-ignored
  (`AGENTS.md`, `tools/check_public_tree.py`). Теперь parity-харнессы и запуск
  сервера возможны.
- **Зависимости (платформенный патч):** корневой `Cargo.toml` содержит
  `[patch.crates-io] wgpu-hal = { path = "third_party/wgpu-hal" }` — обход краша
  драйвера Intel iGPU на Windows/Vulkan (описано в комментарии патча). Патч на месте,
  сборка его подхватывает.

## 2. Проверки Rust, которые реально выполнены

Запускались только те проверки, которые **не требуют** игровых ресурсов и Python.
Это подмножество того, что делает `pr_ready.py` (fmt, clippy -D warnings, тесты
крейтов). Полный self-check не запускался — нет Python.

### 2.1 Форматирование — ✅ PASS

```
cargo fmt --all --check
# EXIT=0 (вывода нет — расхождений нет)
```

### 2.2 Тесты лёгких крейтов — ✅ PASS

```
cargo test -p horizon-threads -p horizon-bench
```
- `horizon-bench`: 7 passed; 0 failed; 0 ignored (lib + `hardware::tests::detects_this_machine`).
- `horizon-threads`: 2 passed; 0 failed; 0 ignored.
- doc-tests: 0 для обоих.
- EXIT=0.

### 2.3 Тесты ядра симуляции — ✅ PASS

```
cargo test -p horizon-core --lib
# result: ok. 103 passed; 0 failed; 2 ignored; 0 measured; 0 filtered out; ~8.4 s
```
Покрывает Java `Random`, `jmath`, позиции, paletted storage, density-функции,
`ticks` (очереди запланированных тиков). 2 ignored — не разбирались (вероятно, тяжёлые
или платформозависимые; к ним нужно вернуться при необходимости).

### 2.4 Тесты серверной логики мира — ✅ PASS

```
cargo build -p horizon-world         # Finished в ~27 s
cargo test  -p horizon-world --lib
# result: ok. 159 passed; 0 failed; 1 ignored; ~2.2 s
```
`horizon-world` (NBT, регионы, чанки, worldgen, `level/` — блочное поведение, тики,
redstone, физика сущностей) **собирается и проходит lib-тесты на Windows**. Это
ключевой факт: серверный слой симуляции компилируется на целевой платформе без
игровых ресурсов. 1 ignored не разбирался.

### 2.5 Clippy с `-D warnings` — ✅ PASS (на проверенных крейтах)

```
cargo clippy -p horizon-core -p horizon-threads -p horizon-bench --all-targets -- -D warnings
# Finished dev; EXIT=0 (ни одного предупреждения)
```
Это тот же режим строгости, что в CI (`clippy -D warnings`), но ограниченный тремя
крейтами. Clippy по всему workspace и по `horizon-client`/`horizon-world --all-targets`
не прогонялся из-за времени сборки (см. ниже).

### 2.6 Сборка `horizon-client` (lib) — ✅ PASS

```
cargo build -p horizon-client --lib
# Finished `dev` profile [unoptimized + debuginfo] target(s) in 1m 51s
```
`horizon-client` (клиент + выделенный сервер `--dedicated` + интегрированный сервер,
с wgpu/winit/gilrs/cpal и платформенным патчем `wgpu-hal`) **собирается на Windows**.
Это самый тяжёлый по линковке крейт (по `docs/server.md` — 384 слинкованных крейта).
Тесты клиента (`cargo test -p horizon-client`) при этом **не запускались** —
library/bin/integration-тесты клиента долго линкуются и частично зависят от ресурсов;
к ним стоит вернуться отдельно. Clippy `-D warnings` по клиенту также не прогонялся.

## 2bis. Расширенные проверки фазы 1.5

Разделено на **успешно / пропущено / не удалось** (требование задачи).

### ✅ Успешно

| Проверка | Команда | Результат |
|---|---|---|
| Clippy `-D warnings` (core+world+threads) | `cargo clippy -p horizon-world -p horizon-core -p horizon-threads --all-targets -- -D warnings` | Finished, 0 предупреждений (~18.8 s) |
| Сборка выделенного сервера (release) | `cargo build --release -p horizon-client --bin horizon-client` | Finished release 7 m 52 s; `target/release/horizon-client.exe` |
| **Запуск выделенного сервера** | `horizon-client --resources .local/horizon-client/26.3 --dedicated --nogui --universe DIR --world world --port N` | ✅ стартует до `Done`, тикает 20 TPS; `/tick query` работает. `--resources` ставить ДО `--dedicated`. |
| **Parity игрового тика** | `py -3 tools/test_tick_parity.py` (напрямую, минуя `parity.py`) | ✅ **PASS**: `client/server: 97,776 scheduled-tick lines match`, `Scheduled-tick comparison passed`. Сверка с vanilla-байткодом через Java-oracle на JDK 25. |
| Подготовка ресурсов | `lab.py prepare`, `horizon-prepare --prepare` | ✅ `.local/horizon-client/26.3` (23076 файлов) |

### ⏭️ Пропущено (платформа/время, не ошибки)

| Проверка | Причина |
|---|---|
| `tools/parity.py` (оркестратор) | Unix-only `fcntl`. Обход — `test_*_parity.py` по одному (release-probe `horizon-world` LTO ≈ 4 мин/харнесс). |
| `tools/server_bench.py`, `tools/perf_bench.py` | Unix (`ps`,`ssh`). Заменены `.local/tick_bench.py`. |
| `cargo test -p horizon-client` | долгая линковка; к клиентским тестам вернуться отдельно. |
| `cargo test --workspace` | нужен target `wasm32-unknown-unknown` (`horizon-mods`). |
| массовый `mob_ai` parity | медленный release-probe-build; точечно на этапе P0. |

### ❌ Не удалось

- По вине кода — ничего. Единственный сбой — `tools/parity.py` (`fcntl`): несовместимость
  runner'а с Windows, обойдена прямым запуском харнесса.

## 3. Что НЕ проверялось и почему (фаза 1.5)

| Проверка | Почему не запущена |
|---|---|
| `py -3 tools/pr_ready.py` целиком | использует `parity.py` (Unix `fcntl`) и долгие харнессы; стадии fmt/clippy/тесты пройдены вручную |
| массовые parity-харнессы | `parity.py` Unix-only; поштучно упирается в медленную release-сборку probe |
| `tools/server_bench.py`, `perf_bench.py` | Unix (`ps`, `ssh`) |
| `tools/lan_e2e.py`, `agent_control_e2e.py` | требуют клиента с окном/скриптования |
| `cargo test --workspace` | нужен `wasm32-unknown-unknown` |

## 4. Итог baseline (фаза 1.5)

- Rust toolchain, Python 3.14 (`py -3`), JDK 25 и игровые ресурсы **готовы**.
- Формат, clippy (core/world/threads/bench), тесты `core`(103)/`world`(159)/`threads`(2)/
  `bench`(7) — **зелёные**. Release-бинарь собирается.
- **Выделенный сервер реально запускается и тикается**; получен runtime-baseline
  (`profiling-results.md`).
- **Tick-parity пройден** против vanilla (97 776 строк совпали) — серверный планировщик
  тиков соответствует спецификации.
- Главные оставшиеся ограничения — **Windows-несовместимость runner'ов**
  (`parity.py`/`server_bench.py`/`perf_bench.py`: `fcntl`/`ps`/`ssh`) и **отсутствие
  CPU-vs-wall на Windows** (`spans::thread_cpu()` = None).
- Производственный код не менялся ради обхода проблем окружения. Изменён только
  `research-lock.json` (allowlist хэшей переизданного манифеста; jar'ы идентичны пиннингу).
