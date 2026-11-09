# pumpkin-analysis.md — Сравнение архитектур Horizon и Pumpkin

> Фаза 1, задача 3. Pumpkin изучается **только как технический источник**. Код
> Pumpkin не переносится в Voxaryn без отдельного анализа совместимости лицензий
> (Pumpkin — **GPLv3**; Voxaryn/Horizon — **MIT**: прямой перенос кода несовместим
> по лицензии, см. §7). Все утверждения ниже подтверждены чтением кода Pumpkin в
> локальной копии репозитория Pumpkin (вне этого проекта); где это гипотеза, отмечено. Исходники Pumpkin не
> изменялись.

## 0. Вводная: два разных фундамента

| | **Horizon / Voxaryn** | **Pumpkin** |
|---|---|---|
| Назначение | клиент + сервер (единый `Game`) | только сервер |
| Лицензия | MIT | GPLv3 (API-крейты: MIT/Apache-2.0) |
| Async-рантайм | нет (чистые потоки + `mpsc`) | **tokio** (`#[tokio::main]`) |
| Параллелизм симуляции | **нет** — тик однопоточный | **rayon** глобальный пул + «entity islands» |
| Владение миром | `Game` владеет `&mut World` | `Arc<World>` с атомарным состоянием |
| Коллекции сущностей | `Vec<Mob>` и др. в `Game` | `ArcSwap<Vec<Arc<dyn EntityBase>>>` |
| Измерения | один уровень «в месте», своп через `mem::swap` | `Arc<World>` на измерение |

Pumpkin — это то, к чему Voxaryn хочет прийти (региональный параллелизм). Главная
ценность источника: **как сохранить корректность порядка игровых событий при
параллельном тике.**

## 1. Модель владения миром

`crates/pumpkin-core/src/world/mod.rs`, `pub struct World` (≈ 233, `#[repr(C)]`):
- `players: ArcSwap<Vec<Arc<Player>>>` (≈ 240)
- `entities: ArcSwap<Vec<Arc<dyn EntityBase>>>` (≈ 243)
- `active_chunks: RwLock<Arc<FxHashSet<Vector2<i32>>>>` (≈ 274)
- `island_schedule: std::sync::Mutex<entity_islands::Schedule>` (≈ 246)
- `navigation_change_sequence: AtomicU64`, `tick_spawns: Mutex<Vec<...>>`.

Сам мир — `Arc<World>` (есть `WorldPortal(pub Arc<World>)` ≈ 7725). Позиции и боксы
сущностей — атомарные ячейки: в `tick_chunks` видно `entity.get_entity().pos.load()`,
`bounding_box.load()`, `health.load()`, `tick_count.fetch_add(1, Relaxed)`,
`inhabited_time.fetch_add(1, Relaxed)`. **Это фундаментальное отличие от Horizon:**
Pumpkin спроектирован так, что несколько rayon-воркеров могут одновременно читать/писать
состояние разных сущностей и чанков, потому что состояние лежит за `Arc` + атомиками +
`ArcSwap`, а не за единым `&mut`.

## 2. Rayon и организация рабочих потоков

- Глобальный пул: `crates/pumpkin/src/main.rs` — `rayon::ThreadPoolBuilder::new()
  .thread_name(|i| format!("Rayon-Worker-{i}")).build_global()` (≈ 60). Предупреждение
  в коде (≈ 47): «All rayon calls from the tokio runtime must be non-blocking!» — то
  есть tokio и rayon сосуществуют, но блокирующую rayon-работу надо уводить с tokio-потоков
  через каналы (пример — `Level::fetch_chunks`).
- Приватный пул генерации чанков: `pumpkin-world/src/chunk_system/schedule.rs` —
  `generation_pool: Arc<rayon::ThreadPool>`, `gen_threads = (cpus/2).clamp(2,16)`,
  имена `ChunkGen-{i}` (≈ 88, 146).
- Приватный пул поиска крепостей: `pumpkin-world/src/generation/structure/placement.rs`
  — отдельный пул `Stronghold-Rings-{i}`, чтобы не блокировать глобальный (≈ 127), плюс
  `into_par_iter().panic_fuse()` (≈ 227).
- Параллельный I/O чанков: `pumpkin-world/src/chunk/io/file_manager.rs` —
  `into_par_iter()` для загрузки/сохранения, `rayon::spawn` + `par_iter` для декодирования
  (≈ 155, 208, 443). Формат `pump.rs` сжимает чанки zstd параллельно на rayon.

Вывод: Pumpkin использует rayon и для **фоновой** работы (генерация, I/O — как у
Horizon его воркеры), и, в отличие от Horizon, **внутри самого тика**.

## 3. Параллельный тик сущностей: Entity Islands

`crates/pumpkin-core/src/world/mod.rs`, `fn tick` (главный серверный тик, ≈ 1583) и
`fn tick_chunks` (≈ 2126). Логика ticking сущностей (≈ 1613–1775):

1. **Игроки:** `players.par_iter().for_each(|p| p.tick(server))` — игроки тикаются
   параллельно безусловно (≈ 1628).
2. **Отбор тикаемых сущностей:** `entities.par_iter().filter_map(...)` оставляет только
   те, что в `active_chunks` и загруженных чанках → `tickable: Vec<(entity, chunk)>`.
3. **Решение «планировать ли острова»** (`plan_islands`, ≈ 1664): только если
   (а) ни один плагин не слушает события тика сущностей
   (`plugins_observe_entity_ticks`) и (б) `island_schedule.should_plan(len)` разрешает.
4. **Построение островов** (если планируем):
   - `tickable.par_iter().map(|(e,_)| EntityReach::of(e, cfg))` — для каждой сущности
     считается **reach** (область чтения/записи) параллельно.
   - `resolve(reports, cfg)` → `prune_block_writes(...)` → `partition(&nodes,
     &idle_entities, &player_bodies)` — разбиение на острова.
   - острова сортируются по убыванию размера («Start the largest islands first»).
5. **Исполнение:**
   - есть острова → `islands.islands.par_iter().with_max_len(1).for_each(|island| {
     for &index in island { tick_entity(index) } })` — **каждый остров на своём потоке,
     но внутри острова — строго в порядке списка.**
   - нет островов (или дорого планировать) → обычный последовательный цикл
     `for index in 0..tickable.len() { tick_entity(index) }`.

### 3.1 Dependency / reach analysis — суть корректности
`crates/pumpkin-core/src/world/entity_islands.rs`. Docstring модуля (≈ 1):
«Splits a tick's entities into islands that touch disjoint state. Each island ticks in
list order on its own thread, which gives the same result as ticking the whole list in
order.» То есть **ключевой инвариант**: острова не пересекаются по состоянию, а внутри
острова сохраняется ванильный порядок — результат эквивалентен последовательному тику.

Что входит в «reach» (`struct TickReach`, ≈ 143): `body` (куда сущность может попасть),
`entities`/`players` (кого может задеть), `block_reads`, `block_writes: Option<BoundingBox>`,
`global: bool` (может действовать где угодно → остров-одиночка со всем миром), `links:
Vec<i32>` (прямые связи, напр. упряжь/пассажиры). Радиус соседних блок-апдейтов —
`BLOCK_UPDATE_REACH = SIGNAL_MAX + 2` (длина сигнала redstone + компонент).

`AuditedReach::scan_contact_blocks` (≈ 191) сканирует блоки, на которые сущность может
наступить/приземлиться, и помечает те, что меняют блоки/измерение при контакте
(`changes_on_contact`: нажимные плиты, detector rail, tripwire, redstone-руда, big
dripleaf, turtle egg, powder snow, sculk sensor/shrieker, lily pad, farmland, порталы).
End gateway → `global = true`. Если область слишком велика (`MAX_CONTACT_BLOCKS = 4096`,
`MAX_CONTACT_SECTIONS = 64`) — консервативный fallback в `global`.

### 3.2 Адаптивное расписание (стоимость планирования)
`Schedule` (entity_islands.rs ≈ 95): острова планируются только при
`entity_count >= MIN_PARALLEL_ENTITIES = 16`. `record_plan` (≈ 118): если крупнейший
остров забирает слишком большую долю (> `count − count/4`), параллелизм не окупает
планирование → кэшируется «последовательный» режим на `SERIAL_PROBE_INTERVAL = 20`
тиков. Это прямой ответ на риск: **планирование само по себе стоит CPU**, и Pumpkin
динамически решает, когда оно оправдано.

## 4. Параллельные блок/флюид/случайные тики

`fn tick_chunks` (mod.rs ≈ 2126), `BATCH_SIZE = 32`:
1. `tick_data.block_ticks.par_chunks(32).for_each(...)` — запланированные блок-тики
   параллельно батчами (≈ 2139).
2. `tick_data.fluid_ticks.par_chunks(32).for_each(...)` — флюиды (≈ 2159).
3. `tick_data.random_ticks.par_chunks(32).for_each(...)` — случайные тики (≈ 2175).
4. Спавн: последовательная подготовка списка категорий → `spawning_chunks.par_chunks(8)
   .for_each(...)` (≈ 2236); перед этим `shuffle` чанков.
5. `active_chunks_vec.par_iter().with_min_len(1024).for_each(|pos| chunk.inhabited_time
   .fetch_add(1, Relaxed))` — батчинг дешёвых атомарных инкрементов, чтобы «не будить
   rayon-воркеров ради мелких задач» (комментарий ≈ 2262).

`tick_data = self.level.get_tick_data(&active_chunks, random_tick_speed)` собирает все
запланированные позиции заранее, затем они льются в rayon. Запись блоков из этих тиков
идёт через `World` (атомики/локи секций), а каскады соседних апдейтов буферизуются:
`pumpkin-core/src/world/neighbor_updater.rs` держит `thread_local! { PENDING_UPDATES }`
«to allow each Rayon worker to drain its cascade» (≈ 90) — то есть каждый воркер копит
свой каскад апдейтов локально.

## 5. Tick scheduler (крейт `pumpkin-scheduler`) — самое важное для Voxaryn

`crates/pumpkin-scheduler/src/domain.rs` вводит **домены исполнения**
(`enum ExecutionDomain`):
- `Global` — серверная работа, которую ещё нельзя отдать более узкому владельцу
  (сейчас реализован **только Global**, docstring: «Only Global admission is currently
  implemented»).
- `World(WorldDomainId)` — один мир/измерение (жизненный цикл, погода, время).
- `Region(RegionDomainId)` — **один независимо планируемый регион мира**: «Chunk ticks,
  block entities, scheduled block work, spawning, and region-scoped events should execute
  with region ownership. … work in the same region is ordered while work over different,
  unrelated regions are parallel.»
- `Entity(EntityDomainId)` — одна подвижная сущность/игрок (тик, движение, бой, AI,
  транспорт). Телепорт/смена измерения/посадка/спавн/деспавн **должны передать или
  аннулировать прежнее поколение владения** перед продолжением работы.
- `External(ExternalDomainId)` — внешние интеграции (не замена tokio для I/O).

Инвариант домена (docstring ≈ 27): «A domain serializes task polls… An operation
targeting another domain must be submitted to that domain using owned inputs… Domain-owned
references and lock guards must never cross that suspension boundary.» То есть
кросс-доменные операции идут через **owned-данные и каналы**, а не разделяемые ссылки.

Это **в точности** региональная модель, заявленная в `README.md` Voxaryn («worker-owned
regions and controlled cross-region event delivery»). Pumpkin уже спроектировал API, но
пока включил только `Global` — остальное в разработке (*гипотеза: по README Pumpkin «under
heavy development», полноценные Region/Entity домены ещё не на всех путях*).

## 6. Сопоставление с Horizon

| Механизм | Horizon / Voxaryn | Pumpkin |
|---|---|---|
| Тик сущностей | последовательный цикл по `Vec<Mob>` (`tick_creatures`) | `par_iter` + Entity Islands (reach-анализ) |
| Блок/случайные тики | `with_level` под `&mut` (`tick_blocks`) | `par_chunks(32)` на rayon |
| Корректность порядка | гарантирована единственностью `&mut` | гарантирована непересечением островов + list-order внутри острова |
| Состояние сущности | поля в `Game`, `&mut` | `Arc<dyn EntityBase>` + атомики (`pos.load()`) |
| Мир | `World` в `Game`, своп измерений `mem::swap` | `Arc<World>` на измерение |
| Соседние апдейты | синхронно в `with_level`, `level_events` | буфер per-worker (`thread_local PENDING_UPDATES`) |
| Фоновые потоки | `streaming.rs` (gen/light/mesh), `net.rs` — `mpsc` | rayon-пулы (gen/I/O) + tokio (сеть) |
| Планировщик доменов | нет | `pumpkin-scheduler` (`Global/World/Region/Entity`) |
| Детерминизм RNG | `horizon_core::random`, порядок как в vanilla | reach-анализ сохраняет порядок внутри острова |

### Что из Pumpkin ценно концептуально (не как код)
1. **Reach/dependency analysis как способ сохранить порядок событий.** Главная идея:
   параллелить можно только непересекающиеся по состоянию множества; внутри множества —
   строгий порядок. Это даёт результат, эквивалентный последовательному тику.
2. **Адаптивное расписание:** планировать параллелизм только когда он окупается
   (порог сущностей, доля крупнейшего острова, кэш «последовательного» режима).
3. **Региональные домены** с кросс-доменной связью через owned-данные/каналы — точный
   шаблон для цели Voxaryn.
4. **Консервативный fallback в `global`** для сущностей/блоков с непредсказуемым reach
   (порталы, end gateway, крупные области) — безопасность важнее параллелизма.

### Чего Horizon стоит остерегаться при копировании подхода
- Pumpkin расплачивается **атомарным состоянием сущностей** (`ArcSwap`, `pos.load()`) —
  это глубокая перестройка модели данных, несовместимая с нынешним `&mut Game`.
  Прямой перенос невозможен без переписывания хранения состояния.
- Pumpkin допускает плагины, которые **отключают** островной параллелизм
  (`plugins_observe_entity_ticks`) — признание, что произвольные побочные эффекты ломают
  reach-анализ. У Horizon есть WASM-моды (`horizon-mods`) с похожим риском.
- Лицензионный барьер (§7).

## 7. Лицензии (обязательное ограничение)

- Pumpkin (сервер) — **GPLv3** (`Pumpkin/LICENSE`, README). GPLv3-код **нельзя**
  включать в MIT-проект Voxaryn/Horizon.
- Исключение: `pumpkin-plugin-api` и `pumpkin-plugin-wit` — dual MIT/Apache-2.0, но это
  plugin-API, а не движок тика.
- **Вывод:** из Pumpkin берутся только **идеи и архитектурные подходы** (reach-анализ,
  домены, адаптивное расписание). Любой код пишется заново под MIT. Прямое копирование
  исходников запрещено и ограничением задачи, и лицензией.

## 8. Источники

- Код Pumpkin, прочитанный напрямую: `crates/pumpkin/src/main.rs`,
  `crates/pumpkin-core/src/world/mod.rs` (`tick`, `tick_chunks`, `World`),
  `crates/pumpkin-core/src/world/entity_islands.rs`,
  `crates/pumpkin-scheduler/src/{lib.rs,domain.rs}`,
  `crates/pumpkin-world/src/chunk_system/schedule.rs`,
  `crates/pumpkin-world/src/chunk/io/file_manager.rs`,
  `crates/pumpkin-core/src/world/neighbor_updater.rs`, `Pumpkin/README.md`,
  `Pumpkin/LICENSE`.
- Онлайн-источники (документация, Issues, PR, инженерные обсуждения): в этой сессии
  веб-поиск **не выполнялся** — хватило чтения актуального кода локальной копии Pumpkin.
  Для следующей итерации полезно свериться с трекером Pumpkin по entity-islands и
  scheduler (например, issue «Entity AI» #1406, «Redstone» #1402, «Chunk Generation» #36
  из README) — это **указатели, не подтверждённые факты**.
