# P1.3: итоговый статус

> 2026-11-09. Windows 11, i5-12400F, rustc/cargo 1.99.1. База: `ec4780cd` (HEAD), все изменения не закоммичены.
> Предыдущие отчёты: [p13-vanilla-parity.md](p13-vanilla-parity.md), [p13-parallel-correctness.md](p13-parallel-correctness.md), [p13-regressions.md](p13-regressions.md).

## 1. Статус

P1.3 закрыт.

| Пункт | Статус |
|---|---|
| P1.3 B: удары и тревоги внутри тика атакующего | ✓ |
| P1.3 C: размножение | ✓ |
| P1.3 D: правки блоков видны следующим мобам | ✓ |
| P1.3 E: `Motion` при загрузке (компонента > 10 → 0) | ✓ |
| P1.3 F: порядок толчков по секциям | известное ограничение (§3) |
| P1.3.1: страховка «запись в моба другого острова» | ✓ |
| P1.3.2: синхронный урон через `PassVictims` | ✓ |
| P1.3.2 hotfix: ожидающие толчки до урона | ✓ |
| Итоговая проверка 1: `PendingPushes` + межостровной rollback | ✓ (§2.1) |
| Итоговая проверка 2: быстрый моб в пределах vanilla | ✓ (§2.2) |

В итоговой проверке игровая логика не менялась. Всё добавленное в этой фазе работает только в тестовой сборке `parallel-entities-unsafe-test` и включается через `#[cfg]`:

- инъекция `pushed_attack` в `game/creatures.rs` и `game/creatures_parallel.rs`;
- `PendingPushes::waiting_for` в `mobs.rs`.

Плюс переписан `tools/voxaryn/repro_fast_mover.py`. Обычная сборка и сборка `parallel-entities` компилируют тот же код, что и после hotfix.

## 2. Итоговые проверки

### 2.1 Ожидающий толчок и межостровной rollback

Инъекция `HORIZON_ENTITY_UNSAFE_INJECT=pushed_attack:N` действует на N-м проходе, дошедшем до разбиения. На этом проходе:

- выбирается толкающий C и жертва B из острова X;
- выбирается атакующий A из другого острова Y;
- порядок тика такой, что C тикает раньше A, а B не тикает между ними;
- C в свой тик толкает B на `[0.25, 0.1, -0.125]`;
- A в свой тик бьёт B на 1.0.

Значит, когда A бьёт B, толчок ещё ждёт в `PendingPushes`. Острова не видят друг друга, поэтому удар остаётся событием `AttackMob`, сработает страховка, и тик пересчитывается последовательно. Для такого прохода последовательный эталон на копиях считается всегда, при любом `HORIZON_ENTITY_VERIFY`.

Что сверяется в журнале сервера (строки `TEST pushed_attack`):

- был ли у жертвы ожидающий толчок в начале тика атакующего;
- скорость жертвы сразу после тика атакующего, пересчитанная независимо: `LivingEntity.knockback(0.4)` от скорости плюс все ожидающие толчки, каждый по одному разу. Рядом считаются варианты «толчок дважды» и «толчок не применён»;
- остался ли толчок недоставленным, остались ли события `Push`, сколько `MobHit`;
- HP, скорость (биты), digest RNG и digest всего моба: redo против эталона;
- весь проход: мобы, события, мёртвые, `died`, привязки, level random (`difference()` = None).

`species_land_8x250`, 6 потоков, `VERIFY=0`:

| | `largest` | `shuffle:7` |
|---|---|---|
| Инъекция | A=405, B=17, C=18 (порядок: B 0, C 1, A 2) | A=410, B=22, C=23 (B 0, C 1, A 2) |
| Rollback | 1 (`redone sequentially`, затем проход выключен) | 1 |
| Толчок ждал в момент удара | redo да, эталон да | да / да |
| После удара = один толчок | да (дважды: нет; ни разу: нет) | да (нет; нет) |
| Толчок недоставлен / лишние `Push` | нет / 0 | нет / 0 |
| `MobHit` | 1 и 1 | 1 и 1 |
| HP, скорость, RNG, моб | побитово равны эталону | побитово равны |
| Весь проход | равен | равен |
| Расхождений / непересчитанных | 0 / 0 | 0 / 0 |

Без hotfix вариант «после удара = один толчок» был бы ложным: толчок попадал бы в скорость после отбрасывания, а не до него.

### 2.2 Быстрый моб в пределах vanilla

Старая проверка (`Motion:[62,0,0]`) устарела. `Entity.load` в 26.3 обнуляет компоненту больше 10, и Horizon делает так же с P1.3 E. Прогон `legacy62` это подтверждает:

- 400 из 400 проходов параллельные, отказов `motion` нет;
- 8 из 10 «снарядов» остались на месте призыва.

То есть старая проверка ничего не проверяла.

Новая сцена (`repro_fast_mover.py`):

- «снаряд» — корова с `Motion:[10,0,0]` (10 — предел, который `Entity.load` ещё сохраняет);
- стоит на каменном полу в 9,5 блока перед первым столбцом стада B1, на одной линии с рядом стада;
- в первый тик проходит 10 блоков (движение идёт раньше трения) и падает на корову стада;
- 10 блоков за тик намного больше доказуемого шага (`islands::STEP_MAX` = 3).

`VERIFY=1` на каждом проходе, 10 «снарядов», 400 тиков. «Остались» — сколько «снарядов» осталось на месте призыва (0 значит, что все улетели).

| Прогон | Переменные | Параллельно | Тревоги | Пересчёт | Расхождений | Остались |
|---|---|---|---|---|---|---|
| `protected` | — | 190/400, отказы `motion` на тиках со «снарядом» | 0 | 0 | **0** | 0 |
| `reach3` | `UNSAFE_REACH=3` | 190/400, отказы `motion` | 0 | 0 | **0** | 1 |
| `nobound` | `UNSAFE_NO_MOTION_BOUND=1` | 7/460 | **1** («moved further than the plan's step of 4.00») | 1 | 0 | 1 |
| `nobound_reach3` | + `UNSAFE_REACH=3` | 7/460 | **1** | 1 | 0 | 0 |
| `nobound_reach3_noguard` | + `UNSAFE_NO_GUARD=1` | 6/480 | **1** | **0** (NOT redone) | **1** | 0 |
| `legacy62` | `--speed 62` | 400/400 | 0 | 0 | 0 | 8 |

В последнем прогоне расхождение такое: корова 367 стада в точке (1060.5, 1010.5), на которую упал «снаряд». В параллельном проходе её не толкнули, в последовательном толкнули.

Что это показывает:

- с защитой допустимый быстрый моб безопасен, доказательство отказывает на таких тиках;
- без доказательства шага проверка после прохода находит нарушение, и guard его исправляет;
- без guard расхождение реально и verify его ловит.

Негативный контроль работает в обе стороны.

Наблюдение: при настоящем радиусе (`follow_range` ≥ 16 плюс запас) скорость, допустимая в vanilla (≤ 10 по оси за тик), сама не переносит моба из одного острова в чужой за один тик. Поэтому прогон с реальной ошибкой дополнительно сужает радиус (`UNSAFE_REACH=3`). Защита по шагу при этом остаётся консервативной: она срабатывает раньше, чем возможна ошибка.

### 2.3 Регрессии (только необходимые)

Код вне `#[cfg(feature = "parallel-entities-unsafe-test")]` в этой фазе не менялся, поэтому parity-прогоны hotfix остаются в силе.

- `cargo clippy -p horizon-client --lib --bins [--examples] -- -D warnings` без фич, с `parallel-entities` и с `parallel-entities-unsafe-test`: чисто.
- `cargo test -p horizon-client --bins --features parallel-entities-unsafe-test`: 104 + 18 passed.
- `cargo test -p horizon-client --lib --features parallel-entities-unsafe-test`: 1090 passed, 3 failed. Это известные падения Windows, они есть и на нетронутом HEAD: `crash::tests::stacks_start_at_the_panicking_frame`, `packs::cache::tests::directory_inputs_config_and_owned_output_recovery`, `save::tests::session_lock_is_exclusive`.

Результаты hotfix на том же коде игры:

- `test_mob_ai_parity.py`: 154/162 сцены совпадают полностью, регрессий нет;
- `test_server_entities_parity.py`: 30 из 30;
- `species_land_8x250`, `VERIFY=1`: 179 + 179 проходов сверено, 0 расхождений.

## 3. Известные ограничения

1. **Порядок толчков по секциям (P1.3 F).** Vanilla перебирает цели толчка по секциям 16³ в порядке входа, Horizon — в порядке мобов. Толпа на границе секций расходится в последнем бите скорости (`cows_crammed_sections`).
2. **Взгляд зомби на убитую в прыжке цель.** На следующий тик после убийства цели, подброшенной отбрасыванием, зомби Horizon ещё смотрит на неё (лишние вызовы RNG), а vanilla нет. Видно в `cows_push_before_zombie_kill`, тик 25. Не зависит от P1.3.2 и hotfix: то же при `HORIZON_PROBE_POST_TICK_HITS=1` и `HORIZON_PROBE_PUSH_AFTER_HIT=1`.
3. **Сплетни жителя** о зомби-обидчике не перенесены (`zombie_hits_villager`, тик 22). **Зелье невидимости** странствующего торговца ночью не перенесено.
4. **`jmath` sin/cos на Windows** (UCRT libm) расходится с HotSpot в последнем бите (`creeper_alone` t92, `breeze_player` t70).
5. **`lambs_seven_pens` / `lambs_same_tick`** расходятся одинаково до hotfix и после. Причина не разбиралась.
6. **`zombie_kills_trader`** расходится на тике 152, как и до hotfix.
7. **Отклонённый урон (cooldown)** в oracle-сценах не встречается: зомби бьёт ровно тогда, когда истекает неуязвимость. Покрыт только юнит-тестом `a_refused_hit_still_applies_the_pushes`.
8. **Параллельный проход после любого срабатывания страховки выключается до перезапуска** (`the pass is now off`). Это по замыслу, но поэтому за один прогон проверяется только первая ошибка.
9. **Непроверенные виды** (волк, летучая мышь, лиса, аксолотль) держат проход последовательным (`wolves_skeletons_8x`: `species`).
10. **Случайный выбор моб-тройки.** Инъекция `pushed_attack` берёт мобов по плану острова. Сцена между прогонами недетерминирована (время прогрева), поэтому выбор разный, но оба прогона проходят.
11. Не запускались на этой машине: `tools/parity.py` (`fcntl`), `lan_e2e.py` (symlink), `save_roundtrip`/`world_reopen` (`vscreen`).

## 4. Состояние дерева

`git status --short` (2026-11-09):

```
 M Cargo.lock
 M crates/horizon-client/Cargo.toml
 M crates/horizon-client/examples/probe/mob_ai_probe.rs
 M crates/horizon-client/src/entity_nbt.rs
 M crates/horizon-client/src/game.rs
 M crates/horizon-client/src/game/commands/world_cmds.rs
 M crates/horizon-client/src/game/creatures.rs
 M crates/horizon-client/src/game/lan_mobs.rs
 M crates/horizon-client/src/game/levels.rs
 M crates/horizon-client/src/game/zombie_villagers.rs
 M crates/horizon-client/src/mobs.rs
 M crates/horizon-client/src/mobs/ai.rs
 M crates/horizon-client/src/mobs/animal_brain.rs
 M crates/horizon-client/src/mobs/farm.rs
 M crates/horizon-client/src/mobs/goals.rs
 M crates/horizon-client/src/mobs/path.rs
 M crates/horizon-client/src/mobs/seen_grid.rs
 M crates/horizon-client/src/mobs/world.rs
 M crates/horizon-client/src/save.rs
 M crates/horizon-client/src/scoreboard.rs
 M crates/horizon-core/src/jmath.rs
 M mods/horizon/src/test/java/dev/horizonlab/parity/VanillaMobAiOracle.java
 M research-lock.json
 M tools/test_mob_ai_parity.py
 M tools/test_server_entities_parity.py
?? .idea/
?? Voxaryn.iml
?? crates/horizon-client/src/game/alloc_stats.rs
?? crates/horizon-client/src/game/creatures_parallel.rs
?? crates/horizon-client/src/game/speculate.rs
?? crates/horizon-client/src/mobs/islands.rs
?? crates/horizon-client/src/mobs/spec_probe.rs
?? crates/horizon-client/src/mobs/victims.rs
?? docs/research/
?? mods/horizon/src/bench/bench.iml
?? mods/horizon/src/neoforge-bench/neoforge-bench.iml
?? mods/horizon/src/neoforge-oracle/neoforge-oracle.iml
?? mods/horizon/src/test/test.iml
?? tests/tests.iml
?? tools/voxaryn/
```

`git diff --stat HEAD`: 25 files changed, 2418 insertions(+), 197 deletions(-). Untracked файлы в это число не входят.

SHA-256 (первые 16 символов) на момент отчёта:

| Хеш | Файл |
|---|---|
| `216e907f04545e2d` | `crates/horizon-client/src/mobs/victims.rs` |
| `d0cd2f01f8aacc67` | `crates/horizon-client/src/mobs.rs` |
| `21185d2077ff79ea` | `crates/horizon-client/src/game/creatures.rs` |
| `b6a60f14a533ecc2` | `crates/horizon-client/src/game/creatures_parallel.rs` |
| `89820e3da644afe6` | `crates/horizon-client/src/mobs/islands.rs` |
| `9271ce3253d9af71` | `crates/horizon-client/src/entity_nbt.rs` |
| `b58c2f0ad09f2520` | `crates/horizon-client/examples/probe/mob_ai_probe.rs` |
| `1e2a84d1d088447a` | `tools/test_mob_ai_parity.py` |
| `c896885f39092d53` | `tools/voxaryn/repro_fast_mover.py` |
| `a9693a7e44ac4faf` | `tools/voxaryn/entity_bench.py` |
| `1f955913f4df3169` | `.local/p1-unsafe-target/release/horizon-client.exe` (тестовая сборка итоговых прогонов) |

Результаты прогонов (`.local/voxaryn-bench/`, git-ignored):

- `p13final_pushed_attack_v0.json`, `p13final_pushed_attack_v0_shuffle.json`, `p13final_pushed_attack_shuffle.log`;
- `p13final_fast_{protected,legacy62,nobound,nobound_reach3,nobound_reach3_noguard,reach3}.json`;
- `p132h_*.json` (hotfix).

## 5. Команды воспроизведения

PowerShell, корень репозитория. Для серверных прогонов нужен `target/release/horizon-bots.exe` (`cargo build --release -p horizon-bots`).

```powershell
# Тестовая сборка сервера
$env:CARGO_TARGET_DIR = "$PWD\.local\p1-unsafe-target"
cargo build --release -p horizon-client --bin horizon-client --features parallel-entities-unsafe-test
Remove-Item Env:CARGO_TARGET_DIR
$exe = '.local/p1-unsafe-target/release/horizon-client.exe'

# 2.1: ожидающий толчок + межостровной rollback, VERIFY=0
py -3 tools/voxaryn/entity_bench.py verify --exe $exe --scenarios species_land_8x250 --threads 6 `
  --orders largest --verify-every 0 --ticks 60 --env HORIZON_ENTITY_UNSAFE_INJECT=pushed_attack:30 `
  --out .local/voxaryn-bench/p13final_pushed_attack_v0.json
Select-String .local/voxaryn-bench/run_species_land_8x250/logs/latest.log -Pattern 'TEST|parallel entity pass:'
# (то же с --orders shuffle:7)

# 2.2: быстрый моб; защита включена, затем негативные контроли
py -3 tools/voxaryn/repro_fast_mover.py --exe $exe --out .local/voxaryn-bench/p13final_fast_protected.json
py -3 tools/voxaryn/repro_fast_mover.py --exe $exe --speed 62 --out .local/voxaryn-bench/p13final_fast_legacy62.json
py -3 tools/voxaryn/repro_fast_mover.py --exe $exe --env HORIZON_ENTITY_UNSAFE_NO_MOTION_BOUND=1 `
  --out .local/voxaryn-bench/p13final_fast_nobound.json
py -3 tools/voxaryn/repro_fast_mover.py --exe $exe --env HORIZON_ENTITY_UNSAFE_NO_MOTION_BOUND=1 `
  --env HORIZON_ENTITY_UNSAFE_REACH=3 --out .local/voxaryn-bench/p13final_fast_nobound_reach3.json
py -3 tools/voxaryn/repro_fast_mover.py --exe $exe --env HORIZON_ENTITY_UNSAFE_NO_MOTION_BOUND=1 `
  --env HORIZON_ENTITY_UNSAFE_REACH=3 --env HORIZON_ENTITY_UNSAFE_NO_GUARD=1 `
  --out .local/voxaryn-bench/p13final_fast_nobound_reach3_noguard.json
py -3 tools/voxaryn/repro_fast_mover.py --exe $exe --env HORIZON_ENTITY_UNSAFE_REACH=3 `
  --out .local/voxaryn-bench/p13final_fast_reach3.json

# Регрессии
cargo clippy -p horizon-client --lib --bins --examples -- -D warnings
cargo clippy -p horizon-client --lib --bins --features parallel-entities -- -D warnings
cargo clippy -p horizon-client --lib --bins --examples --features parallel-entities-unsafe-test -- -D warnings
cargo test -p horizon-client --lib --features parallel-entities-unsafe-test
cargo test -p horizon-client --bins --features parallel-entities-unsafe-test

# Parity (hotfix): JAVA_HOME = JDK 25, HORIZON_SERVER_JAR = jar сервера 26.3, без CARGO_TARGET_DIR
py -3 tools/test_mob_ai_parity.py
py -3 tools/test_server_entities_parity.py
```
