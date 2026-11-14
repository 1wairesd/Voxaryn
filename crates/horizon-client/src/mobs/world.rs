//! What mobs read from the level: block solidity, ray clipping for line of
//! sight (`ClipContext.Block.COLLIDER`), sky visibility, light-dependent
//! values and the positions of other entities.
use super::{MobWorld, Seen, Who, data};
use crate::{
    blocks::{BlockFacts, Fluid},
    collisions::SceneCollisions,
};
use horizon_core::{
    geometry::{Aabb, Vec3, traverse_blocks},
    position::BlockPos,
};
use horizon_world::{
    block_collision::{BlockCollisionRegistry, DynamicCollisionWorld},
    registry::BlockStateId,
};

/// Where a target entity is this tick.
#[derive(Clone, Copy, Debug)]
pub struct TargetInfo {
    pub position: Vec3,
    pub eye_y: f64,
    pub bounds: Aabb,
    pub alive: bool,
    pub can_be_seen_as_enemy: bool,
    pub is_player: bool,
    pub creative: bool,
}
impl MobWorld<'_> {
    /// The biome's base temperature at a position.
    pub fn temperature(&self, x: i32, y: i32, z: i32) -> f32 {
        self.world
            .chunks
            .get(&(x >> 4, z >> 4))
            .map_or(0.8, |c| self.biomes.get(c.biome(x, y, z)).temperature)
    }
    /// `Heightmap.Types.MOTION_BLOCKING`: above the highest blocking block.
    pub fn motion_blocking_height(&self, x: i32, z: i32) -> i32 {
        let top = crate::chunk::MIN_Y + crate::chunk::HEIGHT;
        (crate::chunk::MIN_Y..top)
            .rev()
            .find(|&y| {
                let f = self.facts.get(self.world.block(x, y, z));
                f.full_collision || f.fluid != crate::blocks::Fluid::None
            })
            .map_or(crate::chunk::MIN_Y, |y| y + 1)
    }
    /// BlockState.isSolid (the legacy solid flag).
    pub fn solid(&self, x: i32, y: i32, z: i32) -> bool {
        self.data.state(self.world.block(x, y, z)).flags & data::SOLID != 0
    }
    pub fn fluid(&self, x: i32, y: i32, z: i32) -> Fluid {
        self.facts.get(self.world.block(x, y, z)).fluid
    }
    pub fn block_name(&self, x: i32, y: i32, z: i32) -> &str {
        &self.facts.get(self.world.block(x, y, z)).name
    }
    /// [`Self::block_name`] after the block changes the mobs before this one
    /// made this tick ([`BlockEdits`], when the pass keeps them): vanilla sets
    /// those blocks during the tick of the mob that changed them.
    pub fn block_name_live(&self, x: i32, y: i32, z: i32) -> &str {
        self.block_edits
            .and_then(|e| e.name_at([x, y, z]))
            .unwrap_or_else(|| self.block_name(x, y, z))
    }
    /// The block at a position as vanilla's level holds it during the mob pass:
    /// after the changes the mobs before this one made this tick
    /// ([`BlockEdits`], P2.4). Pathfinding reads it (P2.5).
    #[inline]
    pub fn block_now(&self, x: i32, y: i32, z: i32) -> BlockStateId {
        match self.block_edits {
            Some(e) => e
                .state_at([x, y, z])
                .unwrap_or_else(|| self.world.block(x, y, z)),
            None => self.world.block(x, y, z),
        }
    }
    /// A block state property's value.
    pub fn prop(&self, x: i32, y: i32, z: i32, key: &str) -> Option<&str> {
        self.registry
            .state(self.world.block(x, y, z))
            .and_then(|s| s.properties.get(key))
            .map(String::as_str)
    }
    pub fn has_tag(&self, tag: &str, block: &str) -> bool {
        self.block_tags.get(tag).is_some_and(|s| s.contains(block))
    }
    /// Level.clip with COLLIDER shapes (optionally stopping at any fluid): true on a miss.
    pub fn clear_between(&self, from: Vec3, to: Vec3, fluids_block: bool) -> bool {
        let mut scene = SceneCollisions {
            edits: None,
            world: self.world,
            binding: self.collision,
            subject: None,
        };
        let result = traverse_blocks(from, to, 512, |p: BlockPos| {
            let state = self.world.block(p.x, p.y, p.z);
            if fluids_block && self.facts.get(state).fluid != Fluid::None {
                return Some(());
            }
            if self.facts.is_air(state) {
                return None;
            }
            let shape = self
                .collision
                .collision_shape(&mut scene, state, [p.x, p.y, p.z])
                .ok()?;
            shape.clip(from, to, p).map(|_| ())
        });
        matches!(result, Ok(None))
    }
    /// Level.clip with VISUAL shapes and no fluids: true on a miss.
    pub fn visual_clear_between(&self, from: Vec3, to: Vec3) -> bool {
        self.visual_clip(from, to) == Some(None)
    }
    /// Level.clip with VISUAL shapes and no fluids: the block the segment
    /// first hits, if it does.
    pub fn visual_hit(&self, from: Vec3, to: Vec3) -> Option<[i32; 3]> {
        self.visual_clip(from, to).flatten()
    }
    fn visual_clip(&self, from: Vec3, to: Vec3) -> Option<Option<[i32; 3]>> {
        let mut scene = SceneCollisions {
            edits: None,
            world: self.world,
            binding: self.collision,
            subject: None,
        };
        let state = |x, y, z| self.world.block(x, y, z);
        visual_clip(self.facts, self.collision, &mut scene, &state, from, to)
    }
    /// Level.clip with COLLIDER shapes and no fluids: where the segment
    /// first hits a block, if it does.
    pub fn clip_block(&self, from: Vec3, to: Vec3) -> Option<Vec3> {
        let mut scene = SceneCollisions {
            edits: None,
            world: self.world,
            binding: self.collision,
            subject: None,
        };
        let result = traverse_blocks(from, to, 512, |p: BlockPos| {
            let state = self.world.block(p.x, p.y, p.z);
            if self.facts.is_air(state) {
                return None;
            }
            let shape = self
                .collision
                .collision_shape(&mut scene, state, [p.x, p.y, p.z])
                .ok()?;
            shape.clip(from, to, p).map(|hit| hit.location)
        });
        result.ok().flatten()
    }
    /// Level.clip with COLLIDER shapes and no fluids: the block the segment
    /// first hits, if it does.
    pub fn clip_block_at(&self, from: Vec3, to: Vec3) -> Option<[i32; 3]> {
        let mut scene = SceneCollisions {
            edits: None,
            world: self.world,
            binding: self.collision,
            subject: None,
        };
        let result = traverse_blocks(from, to, 512, |p: BlockPos| {
            let state = self.world.block(p.x, p.y, p.z);
            if self.facts.is_air(state) {
                return None;
            }
            let shape = self
                .collision
                .collision_shape(&mut scene, state, [p.x, p.y, p.z])
                .ok()?;
            shape.clip(from, to, p).map(|_| [p.x, p.y, p.z])
        });
        result.ok().flatten()
    }
    /// The bounds of the block's collision shape, relative to the block.
    pub fn collision_bounds(&self, p: [i32; 3]) -> Option<horizon_core::geometry::Aabb> {
        let mut scene = SceneCollisions {
            edits: None,
            world: self.world,
            binding: self.collision,
            subject: None,
        };
        let state = self.world.block(p[0], p[1], p[2]);
        self.collision
            .collision_shape(&mut scene, state, p)
            .ok()?
            .bounds()
    }
    /// LivingEntity.hasLineOfSight: eye to eye within 128 blocks.
    pub fn line_of_sight(&self, from_eye: Vec3, to: Vec3) -> bool {
        let d =
            (to.x - from_eye.x).powi(2) + (to.y - from_eye.y).powi(2) + (to.z - from_eye.z).powi(2);
        if d > 128.0 * 128.0 {
            return false;
        }
        // Phase 2B diagnostics: count the ray-cast and its length in voxels
        // (a close proxy for the DDA steps `clear_between` walks).
        super::path::stats::record_los(d.sqrt().ceil() as u64);
        self.clear_between(from_eye, to, false)
    }
    /// Level.canSeeSky: sky light 15 at the position (no roof above).
    pub fn can_see_sky(&self, x: i32, y: i32, z: i32) -> bool {
        if self.nether {
            return false;
        }
        self.world.light(x, y, z) >> 4 == 15
    }
    /// Level.getLightLevelDependentMagicValue: the dimension's brightness curve
    /// at the raw (sky darkened) light level.
    pub fn brightness(&self, x: i32, y: i32, z: i32) -> f32 {
        let light = self.world.light(x, y, z);
        let sky = i32::from(light >> 4) - self.sky_darken();
        let level = sky.max(i32::from(light & 15)).clamp(0, 15) as f32;
        // DimensionType.getBrightness with ambient light 0 (overworld) / 0.1 (nether).
        let f = level / 15.0;
        let base = f / (4.0 - 3.0 * f);
        if self.nether { 0.1 + base * 0.9 } else { base }
    }
    /// Whether mob `id` was killed during this tick by the ticking mob's hits
    /// (P1.3.2): the snapshot still shows it alive until the tick ends.
    pub fn gone(&self, id: u32) -> bool {
        self.victims.is_some_and(|v| v.gone(id))
    }
    pub fn sky_darken(&self) -> i32 {
        if self.daylight { 0 } else { 11 }
    }
    pub fn target_info(&self, who: Who) -> Option<TargetInfo> {
        match who {
            Who::Player => self.player.as_ref().map(|p| TargetInfo {
                position: p.position,
                eye_y: p.eye[1],
                bounds: p.bounds,
                alive: p.alive,
                can_be_seen_as_enemy: true,
                is_player: true,
                creative: p.creative,
            }),
            Who::Mob(id) => self.seen(id).map(|s| TargetInfo {
                position: s.position,
                eye_y: s.eye_y,
                bounds: s.bounds,
                alive: s.alive && !self.gone(id),
                can_be_seen_as_enemy: !s.playing_dead,
                is_player: false,
                creative: false,
            }),
        }
    }
    /// The other mobs whose boxes may meet `area` (callers test the boxes),
    /// in snapshot order: through the grid when there is one, else all.
    pub fn others_near(&self, area: Aabb) -> impl Iterator<Item = &Seen> {
        #[cfg(feature = "p2-speculate")]
        super::spec_probe::note_read();
        let mut near = Vec::new();
        let indexed = self
            .others_grid
            .is_some_and(|grid| grid.candidates(area, &mut near));
        // Phase 2B diagnostics: candidates scanned, and full-scan fallbacks.
        let candidates = if indexed {
            near.len() as u64
        } else {
            self.others.len() as u64
        };
        super::path::stats::record_near(candidates, !indexed);
        let all = (!indexed).then(|| self.others.iter());
        near.into_iter()
            .map(|i| &self.others[i as usize])
            .chain(all.into_iter().flatten())
    }
    pub fn seen(&self, id: u32) -> Option<&Seen> {
        #[cfg(feature = "p2-speculate")]
        super::spec_probe::note_read();
        match self.others_index {
            Some(index) => index
                .get(&id)
                .and_then(|&i| self.others.get(i as usize))
                .filter(|s| s.id == id),
            None => self.others.iter().find(|s| s.id == id),
        }
    }
    /// Living mobs of the given species within `range` of `from`, nearest first.
    pub fn nearby<'s>(
        &'s self,
        from: Vec3,
        range: f64,
        pred: impl Fn(&Seen) -> bool + 's,
    ) -> impl Iterator<Item = &'s Seen> + 's {
        // A position within `range` puts the box's bottom face in this area
        // (one block wider, as the box test is strict).
        let r = range + 1.0;
        let area = Aabb::new(
            Vec3::new(from.x - r, from.y - r, from.z - r),
            Vec3::new(from.x + r, from.y + r, from.z + r),
        );
        let mut v: Vec<&Seen> = self
            .others_near(area)
            .filter(|s| {
                s.alive
                    && !self.gone(s.id)
                    && (s.position.x - from.x).abs() <= range
                    && (s.position.y - from.y).abs() <= range
                    && (s.position.z - from.z).abs() <= range
            })
            .filter(move |s| pred(s))
            .collect();
        v.sort_by(|a, b| {
            let da = (a.position.x - from.x).powi(2)
                + (a.position.y - from.y).powi(2)
                + (a.position.z - from.z).powi(2);
            let db = (b.position.x - from.x).powi(2)
                + (b.position.y - from.y).powi(2)
                + (b.position.z - from.z).powi(2);
            da.total_cmp(&db)
        });
        v.into_iter()
    }
}

/// Level.clip with VISUAL shapes and no fluids: the block the segment first
/// hits, None inside when it hits nothing and outside when the segment is
/// too long to walk. Glass and copper grates (`TransparentBlock`) and bars and panes
/// (`IronBarsBlock`) have no visual shape; mud, soul sand and powder snow
/// look like full blocks.
pub fn visual_clip<W: DynamicCollisionWorld>(
    facts: &BlockFacts,
    collision: &BlockCollisionRegistry,
    scene: &mut W,
    state: &dyn Fn(i32, i32, i32) -> BlockStateId,
    from: Vec3,
    to: Vec3,
) -> Option<Option<[i32; 3]>> {
    let full = Aabb::new(Vec3::ZERO, Vec3::new(1.0, 1.0, 1.0));
    let result = traverse_blocks(from, to, 512, |p: BlockPos| {
        let id = state(p.x, p.y, p.z);
        if facts.is_air(id) {
            return None;
        }
        let name = facts.get(id).name.trim_start_matches("minecraft:");
        if name.ends_with("glass")
            || name.ends_with("glass_pane")
            || name.ends_with("_bars")
            || name.ends_with("copper_grate")
        {
            return None;
        }
        if matches!(name, "mud" | "soul_sand" | "powder_snow") {
            return full
                .move_by(Vec3::new(f64::from(p.x), f64::from(p.y), f64::from(p.z)))
                .clip(from, to)
                .map(|_| [p.x, p.y, p.z]);
        }
        let shape = collision.collision_shape(scene, id, [p.x, p.y, p.z]).ok()?;
        shape.clip(from, to, p).map(|_| [p.x, p.y, p.z])
    });
    result.ok()
}

/// The blocks the mobs of one pass changed so far (P1.3 D): the game applies
/// `BreakBlock` and `SetBlockName` after the pass, vanilla at once, so the
/// mobs ticking later read them here, by name
/// ([`MobWorld::block_name_live`]) and, for their movement (P2.4), as the
/// state the game will set (`SceneCollisions::edits`, `MobWorld::supporting_block`).
/// One per pass (per island in the parallel pass: a block two mobs read from
/// where they stand is within the reach that joins them), dropped after it.
#[derive(Default, Debug)]
pub struct BlockEdits {
    map: crate::fast_hash::FxHashMap<[i32; 3], &'static str>,
    /// The state each change leaves, as the game sets it after the pass
    /// (`BreakBlock`: the world's air; `SetBlockName`: the name's default
    /// state, no change when the registry has none).
    states: crate::fast_hash::FxHashMap<[i32; 3], BlockStateId>,
}
impl BlockEdits {
    /// Takes in the block changes among `events`, in order.
    pub fn record(
        &mut self,
        events: &[super::MobEvent],
        registry: &horizon_world::registry::BlockRegistry,
        air: BlockStateId,
    ) {
        for e in events {
            match e {
                // Level.destroyBlock: what the fluid there leaves, air
                // (Horizon's BreakBlock sets air: no mob breaks a fluid-logged block).
                super::MobEvent::BreakBlock { pos, .. } => {
                    self.map.insert(*pos, "minecraft:air");
                    self.states.insert(*pos, air);
                }
                super::MobEvent::SetBlockName { pos, name, .. } => {
                    self.map.insert(*pos, name);
                    if let Some(state) = registry.default_state(name) {
                        self.states.insert(*pos, state);
                    }
                }
                _ => {}
            }
        }
    }
    pub fn name_at(&self, pos: [i32; 3]) -> Option<&'static str> {
        self.map.get(&pos).copied()
    }
    /// The position and the state one block change event leaves (the
    /// state as [`Self::record`] keeps it; None: no change of state).
    pub fn change(
        event: &super::MobEvent,
        registry: &horizon_world::registry::BlockRegistry,
        air: BlockStateId,
    ) -> Option<([i32; 3], Option<BlockStateId>)> {
        match event {
            super::MobEvent::BreakBlock { pos, .. } => Some((*pos, Some(air))),
            super::MobEvent::SetBlockName { pos, name, .. } => {
                Some((*pos, registry.default_state(name)))
            }
            _ => None,
        }
    }
    /// The state a change of this pass left at `pos`, if any.
    pub fn state_at(&self, pos: [i32; 3]) -> Option<BlockStateId> {
        self.states.get(&pos).copied()
    }
    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }
}

/// The block changes of one mob's events (`events`, in order), each as
/// vanilla's `Level.setBlock` makes it (P2.5): into `edits`, then
/// `sendBlockUpdated`'s navigation update ([`super::nav::block_updated`])
/// against the level with it, before the next change. `updated` gets the
/// positions whose change the navigation reads (P2.5.1,
/// [`super::nav::navigation_reads_changed`]), with whether the collision
/// shape changed. `tracked`: the mobs of the pass still in the level.
#[allow(clippy::too_many_arguments)]
pub fn apply_block_changes(
    edits: &mut BlockEdits,
    mobs: &mut [super::Mob],
    tracked: impl Fn(usize) -> bool + Copy,
    base: &MobWorld,
    events: &[super::MobEvent],
    updated: &mut Vec<([i32; 3], bool)>,
    chosen: &mut Vec<usize>,
) {
    for event in events {
        let Some((pos, new)) = BlockEdits::change(event, base.registry, base.world.air) else {
            continue;
        };
        let old = edits
            .state_at(pos)
            .unwrap_or_else(|| base.world.block(pos[0], pos[1], pos[2]));
        edits.record(std::slice::from_ref(event), base.registry, base.world.air);
        let Some(new) = new else {
            continue;
        };
        let w = MobWorld {
            block_edits: Some(&*edits),
            ..base.clone()
        };
        let shape = super::nav::block_updated(mobs, tracked, &w, pos, old, new, chosen);
        if shape || super::nav::navigation_reads_changed(&w, pos, old, new) {
            updated.push((pos, shape));
        }
    }
}
