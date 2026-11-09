# tick-architecture.md — Архитектура текущего игрового тика Voxaryn/Horizon

> Фаза 1, задача 1. Всё описанное ниже прочитано в коде; пути и имена функций
> указаны. Где утверждение — вывод, а не прямое чтение, это помечено.
> Игровая логика на этом этапе не менялась.

## 0. Терминология

Репозиторий переименован в **Voxaryn**, но крейты и бинарники сохраняют имена
**Horizon** (см. `README.md`, `PROJECT_ORIGINS.md`). Проект — это Minecraft Java
Edition 26.3, переписанный на Rust; vanilla — спецификация.

## 1. Два пути одиночной игры

`AGENTS.md` → "The two single-player paths". Подтверждено кодом:

1. **Server thread (по умолчанию):** второй `Game` (`serving`) крутится на потоке
   "Server thread"; окно присоединяется к нему как клиент через память (`remote`) —
   как LAN-гость или игрок выделенного сервера. Это то, что запускают игроки.
2. **In-frame (`--in-frame`, `--no-server-thread`, `HORIZON_SERVER_THREAD=0`):** окно
   само тикает мир, один уровень следует за игроком. Большинство харнессов тестируют
   этот путь.

LAN-гости и выделенный сервер всегда идут через серверный код.

## 2. Цепочка входа в серверный тик

```
game/integrated.rs::run_integrated   (поток "Server thread", 20 TPS)
        │  loop { run_tasks(); tick_server_timed(paused); apply_publish(); wait_for_next_server_tick() }
        ▼
game/levels.rs::tick_server          (один серверный тик по всем измерениям)
        │  для каждого Dim в [Overworld, Nether, End] с игроком:
        │    enter_level(dim)  →  update_dedicated_streaming()  →  update_world()
        │    k==0 ? self.tick() : (tick_weather_effects(); tick_world(running))
        ▼
game.rs::tick  →  game.rs::tick_world
```

### 2.1 `run_integrated` — цикл серверного потока
`crates/horizon-client/src/game/integrated.rs`, `fn run_integrated` (≈ строка 283).
Поток создаётся через `std::thread::Builder::new().name("Server thread")` (≈ 241–243)
и вызывает `horizon_threads::favour_current()` (≈ 246). Тело цикла (≈ 363–386):
`while !flags.stop { run_tasks(); if paused { tick_server_timed(true) } else {
follow_distances(); tick_server_timed(false); clock.record(elapsed);
debug_charts.log_tick(...); } apply_publish(); wait_for_next_server_tick(&mut next,
paused); }`. Это зеркало `MinecraftServer.runServer`. `wait_for_next_server_tick`
(levels.rs ≈ 349) спит до следующего тика (`clock.tick_duration()`), либо не спит во
время `/tick sprint`.

### 2.2 `tick_server` — проход по измерениям
`crates/horizon-client/src/game/levels.rs`, `fn tick_server` (≈ 283). Берёт
`levels_to_tick()` (≈ 377 — `[Overworld, Nether, End]`, отфильтрованные по наличию
игрока, иначе Overworld), затем для каждого измерения `k`:
1. `enter_level(None, dim)` — `mem::swap` всех per-level полей `Game` (один уровень
   «в месте» за раз; остальные лежат как `Parked` в `game/levels.rs`).
2. `self.level_pass = Pass { first: k==0, last: k+1==n }`.
3. `update_dedicated_streaming()`, затем `update_world()` (стриминг чанков).
4. `k==0` → `self.tick()` (полный тик + сеть + общие раз-в-тик части); иначе —
   `tick_weather_effects()` + `tick_world(running)`. В паузе — только `tick_lan()`.

После цикла — `close_idle_levels()` (≈ 395): сохраняет и выгружает не-Overworld
уровни, пустые 300 тиков.

`tick_server_timed` (levels.rs ≈ 321) оборачивает `tick_server` логгером медленных
тиков (`HORIZON_SPIKES=MS`).

### 2.3 `fn tick` — «первый проход» уровня
`crates/horizon-client/src/game.rs`, `fn tick` (≈ 4048). Порядок как в коде:
- `tick_debug_keys` / `tick_debug_charts`.
- **Ранний выход клиента:** `if self.remote.is_some() { return self.tick_remote(); }`
  (≈ 4051–4053) — локальная симуляция не идёт вовсе (см. §5).
- `self.ticks += 1`.
- **Время/погода:** если `running` (`/tick freeze` даёт `running=false`):
  `world.game_time += 1`; `world.weather.tick(&mut world.random)` под геймрулом
  `advance_weather`; `tick_weather_effects()`.
- **Автосохранение:** `if self.ticks.is_multiple_of(6000) { save_game_with(false)? }`
  (≈ 4072) — каждые 6000 тиков (5 минут).
- `advance_clock()`, отсчёт `use_delay`, `tick_totem`, `update_totem_ready`,
  `tick_command_extras`, `tick_mods`.
- **Действия игрока/транспорта:** ввод верховой езды, `tick_riding`, движение игрока
  (`MotionWorld::controlled_simulation_tick`), события движения (звуки, частицы,
  запись блоков farmland/turtle-egg через `with_level`/`notify_written`), смерть.
- **Выделенный сервер без локального игрока** (`player.is_none()` и
  `options.dedicated`): `return self.tick_world(running)` (≈ 4108–4113).
- Последним оператором `tick` вызывает `self.tick_world(running)` (≈ 4365).

### 2.4 `fn tick_world` — мир после игрока
`crates/horizon-client/src/game.rs`, `fn tick_world(&mut self, running: bool)`
(≈ 4367). Каждый шаг обёрнут в `horizon_client::spans::time(name, ...)` — встроенный
профайлер фаз (см. §6). Порядок:
1. `update_chunk_tickets()`.
2. `tick_effects(running)` — статус-эффекты.
3. откат `shield_cooldown`; `tick_using`; `tick_hud`.
4. `tick_local_hand()` — копка/установка локального игрока (до блок-тиков, как в
   vanilla `ServerLevel.tick`).
5. `postprocess_chunks()` — достройка освещённых чанков.
6. **`if running { tick_blocks() }`** — запланированные, случайные, блок-события,
   блок-сущности (см. §3).
7. `if running { tick_paintings, tick_item_frames, tick_armor_stands, tick_boats,
   tick_carts, tick_thunder, tick_lecterns, tick_maps, tick_bolts, tick_fishing,
   tick_tridents, tick_rockets, tick_ominous_item_spawners }`.
8. `if running { handle_entity_portals, tick_items, touch_arrows }`.
9. `spawn_loaded_entities()` — всегда (не под `running`).
10. `if running { tick_mobs()?; recheck_golem_openers; tick_experience;
    find_entity_portals + resolve_arrivals }`.
11. `tick_lan()`, `tick_friends()`, затем `tick_hand, tick_sleep, tick_nether_portal,
    tick_end, tick_falling, tick_tnt`.
12. `if running { tick_block_entities()?; tick_sculk_entities(); tick_sculk_blocks()? }`.
13. Клиентские анимации/частицы (`tick_block_entity_anims`, `tick_particles`) под
    `running`, и повтор удержанного использования предмета.

### 2.5 `fn tick_mobs`
`crates/horizon-client/src/game.rs`, `fn tick_mobs` (≈ 7430): `tick_creatures()`
(тики мобов + AI, `game/creatures.rs`), `position_rider`, `tick_stats`,
`tick_scoreboard`, `load_bossbars`, `tick_functions`, `tick_worldborder`,
`tick_subtitles`, `spawn_phantoms`, `tick_advancements`, `tick_player_model`.
Естественный спавн — под `tick_creatures` (`game/natural_spawning.rs`). *Тела
`tick_creatures`/`tick_block_entities` не читались построчно — подтверждены только
их вызовы.*

## 3. Где и как мутируется мир

### 3.1 Блочные тики
`crates/horizon-client/src/game.rs`, `fn tick_blocks` (≈ 2511). Логика:
`sync_tick_chunks` → `start_ticking_chunks` → собирает random-ticking чанки
(`tickets.levels().random_ticking` + `ticking_chunk`) → собирает боксы
`entity_inside` (игроки/гости/вагонетки) → вся мутация внутри замыканий
`with_level(...)`:
```
with_level(|b, level, ticks| { for pos in inside { b.entity_inside(level, ticks, pos) } });
process_level_events()?;
tick_scheduled()?;                                  // запланированные тики
with_level(|b, level, ticks| {
    for (x,z) in ticking { b.tick_chunk_random(level, ticks, pos, ...) }  // случайные
    b.run_block_events(level, ticks);               // блок-события
    b.tick_block_entities(level, ticks);            // блок-сущности
});
sync_moving_pistons();
process_level_events()
```
Это vanilla-фазы `ServerLevel.tick`: scheduled → random → block events → moving pistons.

### 3.2 Мост в блочное поведение: `with_level` / `GameLevel`
`crates/horizon-client/src/game.rs`, `fn with_level` (≈ 2334) строит временный
`GameLevel<'w>` (определён ≈ 513), который заимствует `world: &'w mut World` плюс
`&mut self.level_events`, `&mut self.random_tick_sections` и т. д., и запускает
`f: impl FnOnce(&Behaviours, &mut GameLevel, &mut Ticks) -> R`. **Мутация — это
прямое `&mut`-заимствование собственных полей `Game`: без блокировок, без
разделения между потоками.**

### 3.3 Библиотека поведения блоков
`crates/horizon-world/src/level/`: `tick.rs` (запланированный блок-тик, драйверы
случайных тиков), `ticks.rs` (`struct Ticks` — очереди scheduled block/fluid,
`comparator_outputs`, `torch_toggles`, `moving_pistons`, `block_events`), `hooks.rs`,
`redstone.rs`, `place.rs`. Docstring `Ticks` прямо говорит: «Owned by the caller next
to its chunk storage» — один владелец, мутация через `&mut`. (`docs/scheduled-ticks.md`
подтверждает: планировщик реализует механику очередей, но сам игровой мир вокруг него
исторически описан как «remain unimplemented» — тик собран уже в `horizon-client`.)

### 3.4 Изменяемое состояние мира — всё это поля одного `Game`
`struct Game` (game.rs, ~290 полей): `world: World` (чанки `world.chunks`,
`block_entities`, `moving_pistons`, `weather`, `random`, `game_time`, `loaded_entities`);
`block_ticks: Ticks`; `tickets`/`tick_chunks`/`random_tick_sections`; коллекции
сущностей `mobs: Vec<Mob>`, `carts`, `tnts`, `falling`, `armor_stands`, `boats` и др.;
`levels: levels::Levels` (припаркованные измерения). **Всё принадлежит одному `Game` в
данный момент.** Сервер держит один уровень на измерение и свопит активный в `Game`
через `enter_level` на время прохода этого уровня — ровно один уровень «в месте» и
изменяем в любой момент.

## 4. Существующая многопоточность, пулы и синхронизация

**Симуляционный тик строго однопоточный.** Поиск по всем `Voxaryn/crates/**/*.rs`:
`rayon|crossbeam|par_iter|par_chunks` — **0 совпадений**. Единственный `tokio` — в
`horizon-telemetry-server` (отдельный, исключённый из workspace крейт, не игровой код).
Весь серверный тик идёт на одном потоке, мутируя один `Game` через `&mut self`. Вся
конкуррентность — `std::thread` + `std::sync::mpsc` + `Arc`/`Mutex`/`RwLock`/`Condvar`/
атомики.

Что реально на отдельных потоках:
- **Пул воркеров чанков** — `crates/horizon-client/src/streaming.rs`.
  `struct Shared` (≈ 201): `queue: Mutex<BinaryHeap<Queued>>`, `ready: Condvar`,
  `stop: AtomicBool`, `context: RwLock<Arc<WorkerContext>>`. `struct Streaming` (≈ 209):
  `shared: Arc<Shared>`, `results: mpsc::Receiver<Done>`, `threads: Vec<JoinHandle>`.
  Воркеры — `thread::Builder::new().name("horizon-worker-{i}")` (≈ 437), вызывают
  `horizon_threads::behind_server()` и обрабатывают только `Generate / Decorate /
  Finish / Save / Light / Mesh`. Это чистый worldgen, освещение, меширование —
  **симуляция тут не идёт**; результаты возвращаются через `mpsc`. Главный поток
  забирает их в `fn update_world` (≈ 10541) через `self.streaming.update(&mut self.world)`.
- **Сетевой поток** — `crates/horizon-client/src/net.rs`. `struct Session`:
  `writer: Arc<Mutex<Option<ConnWriter>>>`, `events: Receiver<Event>`,
  `configuring: Arc<AtomicBool>`, поток `thread::Builder::new().name("horizon-net")`
  (≈ 108). Декодирует пакеты и шлёт `Event` через `mpsc`; игровой поток их вычерпывает.
- **Server thread** — `integrated.rs`. Связь с окном: `Arc<Flags>` (атомики
  `paused/stop/failure/published/render_distance/simulation_distance/smoothed_tick_ms`),
  канал `Sender<Task>`/`Receiver<Task>` где `Task = Box<dyn FnOnce(&mut Game)>`, и
  `Integrated::call` (≈ 88) — закрытие + `sync_channel(1)` для выполнения между тиками
  (`executeBlocking`). Окно присоединяется через in-memory `Pipe` как `remote`-клиент
  своего же сервера.
- **`horizon-threads`** (`crates/horizon-threads/src/lib.rs`) — **не пул и не
  планировщик**, а только подсказки приоритета ОС: `favour_current()` (повышает
  приоритет server/render-потока: macOS QoS user-interactive / Windows above-normal,
  только при >4 ядрах) и `behind_server()`/`put_helpers_behind()` (понижает
  вспомогательные: Linux nice/SCHED_IDLE).

Вывод: генерация, освещение, меширование, сетевой I/O — вне главного потока и общаются
с симуляцией только через каналы + `Arc`/`Mutex`/атомики. Симуляция мира (блок-тики,
сущности, мобы, блок-сущности, redstone, физика) — строго однопоточна на владеющем `Game`.

## 5. Разделение клиентских задач и серверной симуляции

Поля `Game` (game.rs ≈ 1497–1506), решающие, что делает данный `Game`:
- `remote: Option<Remote>` — окно является клиентом сервера (мультиплеер, LAN-гость
  или одиночка на своём server thread). `fn tick` сразу `return self.tick_remote()`;
  локальная симуляция не выполняется — только `tick_remote` (обработка пакетов,
  интерполяция, `game/remote.rs`).
- `lan: Option<LanHost>` — мир открыт в сеть; хаб держит гостей (`host.hub.guests`).
  `tick_lan()` крутит хаб каждый тик; гости симулируются свопом их состояния в `Game`
  на один вызов (`as_guest`/`tick_guests`, `game/lan_actions.rs`).
- `integrated: Option<Integrated>` — ручка окна к собственному server thread.
- `serving: Option<Serving>` — **этот `Game` и есть** server thread.
- `options.dedicated` — у выделенного сервера нет локального игрока; `tick` уходит
  прямо в `tick_world`.

Короче: `Game` с `remote` — чистый клиент (`tick_remote`, без симуляции). `Game` с
`serving`/`lan`/`dedicated` — полная симуляция через `tick_server → tick/tick_world`,
а до клиентов изменения доходят через лог блоков, события уровня и трекер сущностей
(`lan/tracking.rs`, `game/lan_entities.rs`).

## 6. Встроенное профилирование фаз (важно для фаз 4–5)

Каждый шаг `tick_world` обёрнут в `horizon_client::spans::time("<name>", || ...)`.
`tick_server_timed` (levels.rs) при `HORIZON_SPIKES=MS` логирует медленные тики с
их CPU-временем и разбивкой по спанам (`spans::describe`), плюс число мобов. То есть
в коде уже есть покадровый/потиковый тайминг фаз — его стоит использовать как основу
для бенчмарков (см. `benchmark-plan.md`).

## 7. Карта «где что лежит» (кратко)

| Подсистема | Файлы |
|---|---|
| Цикл server thread | `game/integrated.rs` (`run_integrated`), `game/levels.rs` (`tick_server`, `enter_level`, `close_idle_levels`) |
| Тик уровня | `game.rs` (`tick`, `tick_world`, `tick_mobs`, `tick_blocks`, `with_level`, `GameLevel`) |
| Блочное поведение | `crates/horizon-world/src/level/` (`tick.rs`, `ticks.rs`, `hooks.rs`, `redstone.rs`, `place.rs`) |
| Примитивы симуляции | `crates/horizon-core/src/` (`random`, `jmath`, `ticks`, `position`, `paletted`) |
| Пул воркеров (gen/light/mesh) | `crates/horizon-client/src/streaming.rs` |
| Сеть | `crates/horizon-client/src/net.rs` |
| Приоритеты потоков | `crates/horizon-threads/src/lib.rs` |

## 8. Проверено / выведено

- **Проверено чтением кода:** порядок всех подсистем в §2 (`run_integrated`,
  `tick_server`, `tick`, `tick_world`, `tick_mobs`); модель мутации `with_level`/
  `Ticks`/`GameLevel` и единоличное владение (§3); отсутствие rayon/crossbeam и
  механика воркеров/сети/server-thread (§4); гейтинг `remote`/`serving`/`lan`/
  `dedicated` и ранний выход `tick_remote` (§5); обёртки `spans::time` (§6).
- **Выведено (не читалось построчно):** внутренности `tick_creatures`/естественного
  спавна и `tick_block_entities` — в фазе 1 были подтверждены только вызовы.
  **В фазе 1.5 тела прочитаны — см. §9–§12 ниже.**

---

# Фаза 1.5 — разбор тел функций и зависимостей

> Ниже прочитаны тела (а не только вызовы) ключевых фаз, с привязкой к коду. Это
> основа для анализа зависимостей и `parallel-candidates.md`.

## 9. `tick_creatures` / `tick_creature_entities` (мобы, AI, pathfinding)

`crates/horizon-client/src/game/creatures.rs`.

`fn tick_creatures` (≈ 447): `attack_ticks += 1`; спавн существ в свежих освещённых
чанках (`spawn_generation_creatures`); `tick_leads`; `spawn_natural`; кастомные
спавнеры под геймрулом `spawn_mobs` (`tick_wandering_trader`, `tick_patrol_spawner`);
`tick_raids`; затем **`tick_creature_entities`** — главный цикл.

`fn tick_creature_entities` (≈ 480) — ядро сущностной фазы. Структура:
1. **Смерти с прошлого тика:** мобы, получившие смертельный удар (игрок, взрыв),
   умирают первыми (`mob_died`).
2. **Снимок:** `snapshot: Vec<Seen> = self.mobs.iter().map(seen)` — то, что мобы видят
   друг о друге.
3. `let mut mobs = std::mem::take(&mut self.mobs)` — коллекция изымается из `Game` на
   время цикла.
4. **Общий RNG:** `let mut scratch = self.world.random.clone()` — «level random, lent
   to the mobs … and taken back». После цикла `self.world.random = level_random`.
5. **Пространственный индекс:** `SeenGrid` + `index: FxHashMap<id, slot>` строятся раз
   на тик; `base = self.mob_world_base(&[])` — общий для всех мобов вид мира.
6. **Цикл `for i in 0..mobs.len()`** — каждый моб тикается по очереди:
   - строит `MobWorld { others: &snapshot, others_grid, others_index, .. }` —
     **read-view** мира и соседей;
   - `nearest_player`, привязка гостя (`lan_mobs::player_for`);
   - пропуск, если чанк не загружен / вне ticking-зоны (тогда только despawn-check);
   - `apply_mob_pushes` (толчки, накопленные от других мобов);
   - **`m.tick(&w, &mut scratch, &mut events)`** — сам тик моба (AI/движение/физика),
     пишет события в общий `events` и тянет из общего `scratch` RNG;
   - события, действующие на игрока-гостя, вынимаются в `guest_events`;
   - **ключевой инвариант порядка:** `snapshot[i] = seen(m, uuid)` и `grid.update(i, …)`
     — комментарий: *«ServerLevel ticks entities one after another: the ones after
     this mob see it as it is now.»* То есть каждый последующий моб видит предыдущего
     **уже обновлённым**. Это ванильная семантика последовательного тика списка.
   - `apply_brain_events(&mut mobs, &mut events, from)` — события мозга применяются к
     другим мобам сразу.
7. После цикла: `self.world.random = level_random`; `self.mobs.append(&mut mobs)`;
   `sync_riders`; репутация/торговля жителей; `handle_mob_events(events)`;
   `tick_projectiles`, `tick_fangs`, `tick_allays`, эффекты, подборы.

**Зависимости фазы мобов (критично для параллелизма):**
- **Общий RNG `scratch`** — строгая последовательная зависимость: порядок draws должен
  совпасть с vanilla (детерминизм).
- **`snapshot` обновляется после каждого моба** — моб N+1 видит моба N обновлённым
  (порядок списка значим).
- **`events` / `PendingPushes`** — мобы влияют друг на друга (толчки, цели, брачные
  события) в пределах тика.
- **`m.tick` мутирует** сам моб; события применяются к `&mut self` и другим мобам.

**Что внутри `m.tick` read-mostly:** pathfinding. `crates/horizon-client/src/mobs/path.rs`
— `struct Finder` + `fn find(...)` (≈ 1272) — полноценный A* с бинарной кучей
(`heap_insert`/`heap_pop`), читает блоки/коллизии через `MobWorld` (read-view) и
кэширует типы клеток. **Путь вычисляется по снимку мира и возвращает owned `Path`** —
это самостоятельное, не мутирующее мир вычисление на моба (главный кандидат для
параллельной предподготовки).

## 10. `tick_blocks` → `tick_scheduled` / `process_level_events` (блоки, игровые события)

`crates/horizon-client/src/game.rs`.

`fn tick_scheduled` (≈ 2605): для `fluids in [false, true]` — `scheduled_begin`, затем
`loop { scheduled_step; if !level_events.is_empty() { process_level_events()? } }`,
`scheduled_finish`. То есть **запланированные тики и их побочные эффекты чередуются
строго по очереди**, inline, как в vanilla («a shrieker summoning a warden, drops,
game events happen before the next tick runs»).

`fn process_level_events` (≈ 2625): `let events = std::mem::take(&mut self.level_events)`;
`lan_record_events`; затем `for event in events { match event { … } }` — диспетчеризация
побочных эффектов **в ванильном порядке**: частицы разрушения + звук (id 2001),
sculk-заряд (3006), шрик (3007), спавнер-вспышка (2004), частицы роста (1505) и т. д.
Каждый эффект мутирует `&mut self` (аудио, частицы, блоки).

**Зависимости блок-фазы:** всё идёт через `with_level(|b, level, ticks| …)` под
эксклюзивным `&mut`; `level_events` — последовательная очередь, порядок применения
фиксирован (детерминизм redstone/дропов зависит от него).

## 11. `tick_block_entities` (блок-сущности)

`crates/horizon-client/src/game/block_entity_ticks.rs`, `fn tick_block_entities` (≈ 16).
- Готовит состояние (`ticking_states` — какие блоки имеют тикер: хопперы, крафтеры,
  маяки, спавнеры, trial-спавнеры, хранилища, ульи, печи, пивоварни, музыкальные блоки,
  creaking heart).
- Цикл по `self.world.block_entity_tickers()` **в ванильном порядке списка**; перед
  каждым — проверка `is_live(slot)` (предыдущий тик мог удалить этот), `ticking(x,z)`
  (в block-ticking чанке) и границы мира.
- `match self.world.block_entities.get(&pos)` → соответствующий `tick_hopper` /
  `tick_crafter` / `tick_furnace` / … — все мутируют `self.world` напрямую.

**Зависимости:** блок-сущности **влияют друг на друга** (хоппер двигает предметы между
контейнерами — кросс-pos зависимость), и тик может удалить другую блок-сущность
(`is_live`). Порядок списка значим.

## 12. Карта изменяемого доступа к `Game` по фазам

Какие структуры требуют `&mut` в каждой фазе (из тел функций):

| Фаза | Мутирует | Характер зависимости |
|---|---|---|
| clock/weather | `world.game_time`, `world.weather`, `world.random` | глобально, один раз/тик |
| `tick_blocks`/`tick_scheduled` | `world.chunks` (блоки), `block_ticks: Ticks`, `level_events`, `random_tick_sections`, `world.moving_pistons` | очередь событий строго по порядку; redstone-каскады |
| `tick_creature_entities` | `self.mobs` (take/append), `world.random` (общий scratch), `events`, `died`, `lan.mob_players` | RNG + snapshot последовательны; мобы толкают/видят друг друга |
| pathfinding (внутри `m.tick`) | ничего в мире (читает через `MobWorld`), возвращает owned `Path` | **read-only по миру** — независимо на моба |
| `tick_block_entities` | `world.block_entities`, `world` (блоки), `item_columns` | хопперы двигают предметы между pos; порядок списка |
| `tick_items`/`tick_carts`/`tick_falling`/`tick_tnt` | соответствующие `Vec` в `Game`, `world` | физика сущностей, толчки |
| `process_level_events` | `audio`, частицы, `world` (блоки), `lan` лог | порядок событий = детерминизм |

**Общий барьер:** почти каждая фаза берёт `&mut self` и/или `&mut self.world`. Чистая
по миру только **pathfinding** (и, частично, расчёт намерений AI до применения). Общий
`world.random` — главный сериализующий ресурс сущностной фазы.
