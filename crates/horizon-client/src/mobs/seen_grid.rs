//! The tick's mob snapshot indexed by where the boxes are, so a box query
//! (`LivingEntity.pushEntities`) reads the mobs near it instead of every
//! mob, and still meets them in snapshot order.
use crate::fast_hash::FxHashMap;
use horizon_core::geometry::{Aabb, Axis};

/// Width of a grid column, in blocks.
const CELL: f64 = 4.0;

/// Snapshot indices by the column holding each box's minimum corner.
#[derive(Default)]
pub struct SeenGrid {
    cells: FxHashMap<(i32, i32), Vec<u32>>,
    cell_of: Vec<(i32, i32)>,
    /// The widest box (along x or z) the grid has held: no box reaches
    /// further than this past its minimum corner.
    reach: f64,
}

fn column(x: f64, z: f64) -> (i32, i32) {
    ((x / CELL).floor() as i32, (z / CELL).floor() as i32)
}
fn corner(b: Aabb) -> (i32, i32) {
    column(b.min(Axis::X), b.min(Axis::Z))
}
fn width(b: Aabb) -> f64 {
    (b.max(Axis::X) - b.min(Axis::X)).max(b.max(Axis::Z) - b.min(Axis::Z))
}

impl SeenGrid {
    /// The boxes of the snapshot's entries, in order.
    pub fn new(boxes: impl Iterator<Item = Aabb>) -> Self {
        let mut grid = Self::default();
        for (i, b) in boxes.enumerate() {
            let at = corner(b);
            grid.reach = grid.reach.max(width(b));
            grid.cells.entry(at).or_default().push(i as u32);
            grid.cell_of.push(at);
        }
        grid
    }
    /// Entry `i`'s box is now `b` (the mob moved or resized).
    pub fn update(&mut self, i: usize, b: Aabb) {
        self.reach = self.reach.max(width(b));
        let at = corner(b);
        let old = std::mem::replace(&mut self.cell_of[i], at);
        if old == at {
            return;
        }
        if let Some(list) = self.cells.get_mut(&old)
            && let Some(k) = list.iter().position(|&j| j == i as u32)
        {
            list.swap_remove(k);
        }
        self.cells.entry(at).or_default().push(i as u32);
    }
    /// The indices, ascending, of every entry whose box can meet `area`
    /// (a superset: callers still test it). False when the area spans more
    /// columns than the grid holds (or is not finite): scan them all.
    pub fn candidates(&self, area: Aabb, out: &mut Vec<u32>) -> bool {
        out.clear();
        // A box meets `area` only if its minimum corner is below area.max
        // and within `reach` (plus a margin for rounding) of area.min.
        let margin = self.reach + 1.0;
        let lo = column(area.min(Axis::X) - margin, area.min(Axis::Z) - margin);
        let hi = column(area.max(Axis::X), area.max(Axis::Z));
        let span = |a: i32, b: i32| i64::from(b) - i64::from(a) + 1;
        let (nx, nz) = (span(lo.0, hi.0), span(lo.1, hi.1));
        if nx < 1 || nz < 1 || nx.saturating_mul(nz) > self.cells.len() as i64 {
            return false;
        }
        for x in lo.0..=hi.0 {
            for z in lo.1..=hi.1 {
                if let Some(list) = self.cells.get(&(x, z)) {
                    out.extend_from_slice(list);
                }
            }
        }
        out.sort_unstable();
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use horizon_core::geometry::Vec3;

    fn boxed(x: f64, y: f64, z: f64, w: f64) -> Aabb {
        Aabb::new(Vec3::new(x, y, z), Vec3::new(x + w, y + 1.8, z + w))
    }

    #[test]
    fn candidates_hold_every_meeting_box_in_order() {
        // A small LCG keeps the test free of dependencies.
        let mut seed = 0x2545_f491_4f6c_dd1du64;
        let mut next = |range: f64| {
            seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
            (seed >> 11) as f64 / (1u64 << 53) as f64 * range
        };
        let mut boxes: Vec<Aabb> = (0..300)
            .map(|_| {
                boxed(
                    next(64.0) - 32.0,
                    next(8.0),
                    next(64.0) - 32.0,
                    0.3 + next(1.2),
                )
            })
            .collect();
        let mut grid = SeenGrid::new(boxes.iter().copied());
        let mut out = Vec::new();
        let mut indexed = 0;
        for round in 0..2000 {
            // Move one box, sometimes far and wider, as mobs do between queries.
            let i = round % boxes.len();
            let w = if round % 97 == 0 { 3.9 } else { 0.6 };
            boxes[i] = boxed(next(80.0) - 40.0, next(8.0), next(80.0) - 40.0, w);
            grid.update(i, boxes[i]);
            let area = boxed(next(70.0) - 35.0, next(8.0), next(70.0) - 35.0, next(2.0));
            let expected: Vec<u32> = (0..boxes.len() as u32)
                .filter(|&j| boxes[j as usize].intersects(area))
                .collect();
            if grid.candidates(area, &mut out) {
                indexed += 1;
                assert!(out.windows(2).all(|p| p[0] < p[1]), "ascending, no repeats");
                let met: Vec<u32> = out
                    .iter()
                    .copied()
                    .filter(|&j| boxes[j as usize].intersects(area))
                    .collect();
                assert_eq!(met, expected);
            }
        }
        assert!(indexed > 1900, "the grid answered {indexed} of 2000");
    }

    #[test]
    fn unbounded_areas_fall_back_to_a_full_scan() {
        let grid = SeenGrid::new([boxed(0.0, 0.0, 0.0, 0.6)].into_iter());
        let mut out = Vec::new();
        let huge = Aabb::new(
            Vec3::new(f64::NEG_INFINITY, 0.0, 0.0),
            Vec3::new(f64::INFINITY, 1.0, 1.0),
        );
        assert!(!grid.candidates(huge, &mut out));
    }

    /// The entries whose boxes meet `area`, by looking at every box.
    fn scan(boxes: &[Aabb], area: Aabb) -> Vec<u32> {
        (0..boxes.len() as u32)
            .filter(|&j| boxes[j as usize].intersects(area))
            .collect()
    }
    /// What a caller gets from the grid: the candidates that really meet
    /// `area`, or `None` when the grid declines (an area spanning more columns
    /// than the grid holds: the caller then scans every entry, which is the
    /// scan by definition) and leaves the buffer empty.
    fn through_grid(
        grid: &SeenGrid,
        boxes: &[Aabb],
        area: Aabb,
        out: &mut Vec<u32>,
    ) -> Option<Vec<u32>> {
        if !grid.candidates(area, out) {
            assert!(out.is_empty(), "a declined query leaves no candidates");
            return None;
        }
        assert!(out.windows(2).all(|p| p[0] < p[1]), "ascending, no repeats");
        Some(
            out.iter()
                .copied()
                .filter(|&j| boxes[j as usize].intersects(area))
                .collect(),
        )
    }
    /// Far-off boxes, each in a column of its own, so the grid has many
    /// columns and answers small queries instead of declining them.
    fn strays(n: u32) -> Vec<Aabb> {
        (0..n)
            .map(|k| boxed(1000.0 + f64::from(k) * 8.0, 0.0, 1000.0, 0.6))
            .collect()
    }

    /// One buffer serves many queries: whatever an earlier query left in it,
    /// the next answers as a fresh buffer would, and a query the grid cannot
    /// answer leaves it empty.
    #[test]
    fn a_reused_buffer_answers_as_a_fresh_one() {
        let boxes: Vec<Aabb> = (0..120)
            .map(|i| boxed(f64::from(i % 12) * 3.0, 0.0, f64::from(i / 12) * 3.0, 0.6))
            .collect();
        let grid = SeenGrid::new(boxes.iter().copied());
        let mut reused = vec![u32::MAX; 500];
        for k in 0..60 {
            let (x, z) = (f64::from(k % 8) * 5.0 - 3.0, f64::from(k / 8) * 5.0 - 3.0);
            let area = boxed(x, 0.0, z, 1.0 + f64::from(k % 5) * 3.0);
            let mut fresh = Vec::new();
            assert!(grid.candidates(area, &mut fresh));
            assert!(grid.candidates(area, &mut reused));
            assert_eq!(reused, fresh, "query {k}");
            // An empty answer clears the buffer too.
            let far = boxed(1.0e5, 0.0, 1.0e5, 1.0);
            assert!(grid.candidates(far, &mut reused));
            assert!(reused.is_empty());
        }
        let huge = Aabb::new(
            Vec3::new(f64::NEG_INFINITY, 0.0, 0.0),
            Vec3::new(f64::INFINITY, 1.0, 1.0),
        );
        reused.extend([1, 2, 3]);
        assert!(!grid.candidates(huge, &mut reused));
        assert!(reused.is_empty(), "no stale candidates behind a fallback");
    }

    /// A pile on one spot (a farm, a crowd at a player): every box lands in
    /// the same column, and the grid still returns exactly what a scan finds,
    /// in snapshot order, for areas of every size around and inside the pile.
    #[test]
    fn a_dense_pile_matches_the_scan() {
        let mut boxes: Vec<Aabb> = (0..400).map(|_| boxed(10.0, 4.0, 10.0, 0.9)).collect();
        // Near neighbours of the pile, one wide box, and far strays that give
        // the grid enough columns to answer.
        boxes.extend([
            boxed(10.5, 4.0, 10.5, 0.9),
            boxed(14.1, 4.0, 10.0, 0.9),
            boxed(-30.0, 4.0, 2.0, 0.9),
            boxed(9.0, 4.0, 9.0, 3.5),
        ]);
        boxes.extend(strays(40));
        let grid = SeenGrid::new(boxes.iter().copied());
        let mut out = Vec::new();
        let areas = [
            boxes[0],
            boxed(10.9, 4.0, 10.9, 0.5),
            // Touching only on a face: not an intersection, not in the answer.
            boxed(10.9, 4.0, 10.0, 0.5),
            boxed(14.0, 4.0, 10.0, 0.2),
            boxed(100.0, 4.0, 100.0, 1.0),
            boxed(-30.0, 4.0, 2.0, 0.1),
        ];
        for area in areas {
            let answered = through_grid(&grid, &boxes, area, &mut out);
            assert_eq!(answered, Some(scan(&boxes, area)), "{area:?}");
        }
        // The whole pile is found, in snapshot order, by a query on its spot.
        let pile = through_grid(&grid, &boxes, boxes[0], &mut out).unwrap();
        assert!(pile.len() >= 400 && pile.windows(2).all(|p| p[0] < p[1]));
        // An area over the whole world is declined; the scan answers it.
        assert_eq!(
            through_grid(
                &grid,
                &boxes,
                boxed(-2000.0, 0.0, -2000.0, 5000.0),
                &mut out
            ),
            None
        );
    }

    /// Nothing in the grid, and areas exactly on the edge of a column.
    #[test]
    fn empty_grids_and_column_edges() {
        // An empty grid has no columns to read: it declines, with nothing
        // left in the buffer, and the caller scans its (empty) list.
        let empty = SeenGrid::new(std::iter::empty());
        let mut out = vec![7];
        assert!(!empty.candidates(boxed(0.0, 0.0, 0.0, 1.0), &mut out));
        assert!(out.is_empty());
        // Boxes whose minimum corner sits exactly on column edges (4 blocks).
        let mut boxes: Vec<Aabb> = [-8.0, -4.0, -0.0, 0.0, 4.0, 8.0]
            .iter()
            .map(|&x| boxed(x, 0.0, x, 0.6))
            .collect();
        boxes.extend(strays(30));
        let grid = SeenGrid::new(boxes.iter().copied());
        for x in [-8.0, -4.0, -3.4, -0.6, 0.0, 3.4, 3.99, 4.0, 4.6, 8.0] {
            let area = boxed(x, 0.0, x, 0.6);
            assert_eq!(
                through_grid(&grid, &boxes, area, &mut out),
                Some(scan(&boxes, area)),
                "x {x}"
            );
        }
    }

    /// Mobs move between the queries of one tick, as `grid.update` follows them
    /// after each tick: across columns, far away, wider, and back. After every
    /// move the grid answers as a scan of the moved boxes does.
    #[test]
    fn moving_boxes_are_found_where_they_are() {
        let mut boxes: Vec<Aabb> = (0..40)
            .map(|i| boxed(f64::from(i) * 0.5, 0.0, 0.0, 0.6))
            .collect();
        // Static far-off boxes: enough columns that small queries are answered.
        boxes.extend(strays(60));
        let mut grid = SeenGrid::new(boxes.iter().copied());
        let mut out = Vec::new();
        let probe = boxed(0.0, 0.0, 0.0, 6.0);
        let mut answered = 0;
        let mut check = |boxes: &[Aabb], grid: &SeenGrid, note: &str| {
            for area in [
                probe,
                boxed(12.0, 0.0, 0.0, 3.0),
                boxed(-50.0, 0.0, 70.0, 5.0),
            ] {
                // Declined queries are scans by definition; answered ones must agree.
                if let Some(found) = through_grid(grid, boxes, area, &mut out) {
                    assert_eq!(found, scan(boxes, area), "{note}");
                    answered += 1;
                }
            }
        };
        check(&boxes, &grid, "start");
        for step in 0u32..120 {
            let i = (step as usize * 7) % boxes.len();
            let (x, z, w) = match step % 4 {
                0 => (f64::from(step) * 0.7 - 20.0, 3.9, 0.6),
                1 => (-50.0 + f64::from(step % 5), 70.0, 0.6),
                2 => (1.0, 0.0, 3.9),
                _ => (f64::from(i as u32) * 0.5, 0.0, 0.6),
            };
            boxes[i] = boxed(x, 0.0, z, w);
            grid.update(i, boxes[i]);
            check(&boxes, &grid, &format!("step {step}"));
        }
        assert!(answered > 100, "the grid answered only {answered} queries");
    }

    /// Cost of one crowd tick's queries: a new `Vec` for each, as
    /// `MobWorld::others_near` makes, against one buffer kept across them.
    #[test]
    #[ignore]
    fn buffer_reuse_cost() {
        for (n, spread) in [(500u32, false), (2000, false), (2000, true)] {
            let boxes: Vec<Aabb> = (0..n)
                .map(|i| {
                    let (x, z) = if spread {
                        (f64::from(i % 4) * 400.0, f64::from(i / 4 % 4) * 400.0)
                    } else {
                        (0.0, 0.0)
                    };
                    boxed(x + f64::from(i % 3) * 0.01, 0.0, z, 0.9)
                })
                .collect();
            // The queries are the crowd's own boxes. A few far-off boxes give
            // the grid enough columns to answer (as a world's other mobs do;
            // a grid of one pile alone declines every query).
            let mut boxes = boxes;
            boxes.extend(strays(40));
            let grid = SeenGrid::new(boxes.iter().copied());
            let queries = &boxes[..n as usize];
            let reps = 20;
            let (mut fresh_ms, mut reused_ms, mut sum_a, mut sum_b) = (0.0, 0.0, 0usize, 0usize);
            for _ in 0..reps {
                let t = std::time::Instant::now();
                for b in queries {
                    let mut near = Vec::new();
                    grid.candidates(*b, &mut near);
                    sum_a += near.len();
                }
                fresh_ms += t.elapsed().as_secs_f64() * 1e3;
                let t = std::time::Instant::now();
                let mut near = Vec::new();
                for b in queries {
                    grid.candidates(*b, &mut near);
                    sum_b += near.len();
                }
                reused_ms += t.elapsed().as_secs_f64() * 1e3;
            }
            assert_eq!(sum_a, sum_b);
            println!(
                "{n} boxes{}: {} queries/tick, {:.1} candidates each; per tick fresh Vec {:.3} ms, reused {:.3} ms",
                if spread {
                    " in 16 piles"
                } else {
                    " in one pile"
                },
                n,
                sum_a as f64 / f64::from(reps) / f64::from(n),
                fresh_ms / f64::from(reps),
                reused_ms / f64::from(reps),
            );
        }
    }
}
