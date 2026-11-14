//! Voxaryn P2.6: when to try the parallel mob pass at all.
//!
//! P2.5.1 redoes a parallel pass sequentially whenever an island changed a
//! block the navigation reads. Where that happens nearly every tick (lambs
//! eating short grass), each tick pays the parallel attempt *and* the redo,
//! more than the sequential pass alone. This policy watches the outcomes of
//! the real parallel attempts and, while most of them end in a redo, sends
//! the passes straight to the sequential `mob_pass` instead. It still tries
//! again from time to time, so that the islands come back once the changes
//! stop.
//!
//! It changes *which* of the two existing, equal paths a pass takes, never
//! what either does, so the game's results, events and random draws are the
//! same in every mode. It reads no game state and no clock (only outcomes),
//! and draws nothing from any random source.
//!
//! Known limit (P2.6 audit): the state is one per *thread*, not per level. A
//! server thread ticks every dimension that has a player, one after the
//! other, and they all feed and read the same window. The window counts
//! attempts, so a redo-heavy dimension next to a clean one is judged on the
//! mix: with one attempt per tick each, half of them are redone, which is not
//! more than half, so the shared policy stays parallel (one switch at most,
//! when the redo-heavy dimension is ticked first and an odd window of 7 holds
//! 4 redos; it costs one cooldown). It goes sequential for good only when the
//! redo-heavy dimension makes up most of the attempts, and then a clean
//! dimension that does attempt also runs its mob pass sequentially until the
//! next probe (the cost of sharing, bounded by the cooldown). Results are
//! never affected: both paths produce the same state. A policy per level
//! would remove that cost; it is not implemented (P2.6 finalization notes).
//!//! The numbers come from P2.6 measurements: a redo tick costs about 5.3 ms
//! more than a sequential one and a clean parallel tick saves about 6.2 ms
//! (`lambs_shortgrass_8x250`, 8 threads), so the parallel pass pays off
//! while fewer than ~54% of its attempts are redone: it is left at more than
//! half. A probe that finds the conflicts still there is cut short (three
//! redone attempts in a row), since each such attempt costs those 5.3 ms.

/// Outcomes remembered (the newest `WINDOW` attempts).
const WINDOW: u32 = 16;
/// Attempts needed before the window is judged.
const MIN_SAMPLES: u32 = 6;
/// A probe (the first attempts after a cooldown) is given up after this many
/// attempts if every one of them was redone.
const PROBE_GIVE_UP: u32 = 3;
/// Sequential passes after the first switch; doubled at every switch that
/// follows a failed probe, up to `MAX_COOLDOWN`, back to `BASE_COOLDOWN`
/// after a full healthy window. A failed probe costs `PROBE_GIVE_UP` redone
/// ticks (~16 ms), so at the cap that is ~0.1 ms per tick while the conflicts
/// last, and the islands are back within `MAX_COOLDOWN` ticks (6 s) of the
/// last change.
const BASE_COOLDOWN: u32 = 30;
const MAX_COOLDOWN: u32 = 120;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Decision {
    /// Try the parallel pass.
    Parallel,
    /// Run the sequential pass: the recent parallel attempts were mostly redone.
    Sequential,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum Mode {
    Parallel,
    /// Sequential passes left before the next parallel attempt.
    Sequential(u32),
}

/// The policy's state; one per thread that runs the pass (the server thread).
#[derive(Clone, Debug)]
pub(super) struct Policy {
    mode: Mode,
    /// Bit `i` set: the attempt `i` places ago was redone. Cleared at a switch.
    history: u32,
    len: u32,
    cooldown: u32,
    /// The attempts since the last cooldown are a probe still being judged.
    probing: bool,
    /// Switches to sequential and probes (cooldown over: attempts resume), for PARSTATS.
    pub switches: u64,
    pub probes: u64,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            mode: Mode::Parallel,
            history: 0,
            len: 0,
            cooldown: BASE_COOLDOWN,
            probing: false,
            switches: 0,
            probes: 0,
        }
    }
}
impl Policy {
    /// What the next pass that could run in parallel should do. In
    /// sequential mode this counts the pass down; when the cooldown is over
    /// the next attempt is a probe with a fresh window.
    pub fn decide(&mut self) -> Decision {
        match self.mode {
            Mode::Parallel => Decision::Parallel,
            Mode::Sequential(0) => {
                self.mode = Mode::Parallel;
                self.probing = true;
                self.probes += 1;
                Decision::Parallel
            }
            Mode::Sequential(left) => {
                self.mode = Mode::Sequential(left - 1);
                Decision::Sequential
            }
        }
    }
    /// A parallel attempt finished: `redone` whether it had to be redone for
    /// a navigation-relevant block change. Returns the redo count of the
    /// window when this attempt made the policy switch to sequential.
    pub fn record(&mut self, redone: bool) -> Option<(u32, u32)> {
        let mask = (1u32 << WINDOW) - 1;
        self.history = ((self.history << 1) | u32::from(redone)) & mask;
        self.len = (self.len + 1).min(WINDOW);
        let redos = (self.history & ((1u32 << self.len) - 1)).count_ones();
        let gave_up = self.probing && self.len == PROBE_GIVE_UP && redos == self.len;
        if self.len >= PROBE_GIVE_UP {
            self.probing = false;
        }
        if gave_up || (self.len >= MIN_SAMPLES && redos * 2 > self.len) {
            // More than half redone: sequential is cheaper. A second switch
            // soon after a probe waits longer.
            let seen = (self.len, redos);
            self.mode = Mode::Sequential(self.cooldown);
            self.cooldown = (self.cooldown * 2).min(MAX_COOLDOWN);
            self.history = 0;
            self.len = 0;
            self.probing = false;
            self.switches += 1;
            return Some(seen);
        }
        if self.len == WINDOW && redos * 4 <= WINDOW {
            // A whole window of mostly clean attempts: the next trouble
            // starts from the short cooldown again.
            self.cooldown = BASE_COOLDOWN;
        }
        None
    }
    /// The cooldown the next switch will use (for logs and tests).
    pub fn cooldown(&self) -> u32 {
        self.cooldown
    }
    #[cfg(test)]
    pub fn sequential(&self) -> bool {
        matches!(self.mode, Mode::Sequential(_))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn feed(p: &mut Policy, pattern: &[bool]) -> Vec<Option<(u32, u32)>> {
        pattern
            .iter()
            .map(|&redo| {
                assert_eq!(p.decide(), Decision::Parallel);
                p.record(redo)
            })
            .collect()
    }

    /// Without data the existing parallel path runs.
    #[test]
    fn no_data_means_parallel() {
        let mut p = Policy::default();
        assert_eq!(p.decide(), Decision::Parallel);
        assert!(!p.sequential());
    }

    /// Redone attempts are judged only from `MIN_SAMPLES` on; then the
    /// policy goes sequential for exactly the cooldown and probes again.
    #[test]
    fn constant_redo_switches_after_the_minimum_then_probes_after_the_cooldown() {
        let mut p = Policy::default();
        let r = feed(&mut p, &[true; MIN_SAMPLES as usize]);
        assert!(r[..MIN_SAMPLES as usize - 1].iter().all(Option::is_none));
        assert_eq!(
            r[MIN_SAMPLES as usize - 1],
            Some((MIN_SAMPLES, MIN_SAMPLES))
        );
        assert!(p.sequential());
        for _ in 0..BASE_COOLDOWN {
            assert_eq!(p.decide(), Decision::Sequential);
        }
        assert_eq!(p.decide(), Decision::Parallel, "the probe");
        assert_eq!((p.switches, p.probes), (1, 1));
    }

    /// Exactly half redone is not "more than half": no switch (hysteresis
    /// at the break-even point), nor a mostly clean stream with a few redos.
    #[test]
    fn half_or_fewer_redone_stays_parallel() {
        let mut p = Policy::default();
        // F T F T ...: every prefix has at most half redone.
        let half: Vec<bool> = (0..40).map(|i| i % 2 == 1).collect();
        assert!(feed(&mut p, &half).iter().all(Option::is_none));
        let mut p = Policy::default();
        let rare: Vec<bool> = (0..64).map(|i| i % 5 == 0).collect();
        assert!(feed(&mut p, &rare).iter().all(Option::is_none));
        assert_eq!(p.switches, 0);
    }

    /// A probe that is redone again waits twice as long, up to the cap; a
    /// whole healthy window brings the short cooldown back. No switching
    /// back and forth every tick: between two switches lie the cooldown
    /// and at least `MIN_SAMPLES` attempts.
    #[test]
    fn a_failed_probe_backs_off_and_a_healthy_window_resets() {
        let mut p = Policy::default();
        feed(&mut p, &[true; MIN_SAMPLES as usize]);
        for expected in [30, 60, 120, 120, 120] {
            let mut skipped = 0;
            while p.decide() == Decision::Sequential {
                skipped += 1;
            }
            assert_eq!(skipped, expected, "sequential passes before the probe");
            // That `decide` handed out the probe; it is redone again.
            let mut switched = p.record(true);
            for _ in 1..PROBE_GIVE_UP {
                assert!(switched.is_none(), "not before the give-up count");
                assert_eq!(p.decide(), Decision::Parallel);
                switched = p.record(true);
            }
            assert!(
                switched.is_some(),
                "a probe where every attempt is redone is cut short"
            );
        }
        assert_eq!(p.cooldown(), MAX_COOLDOWN);
        // Clean attempts for a whole window: the next trouble starts short.
        while p.decide() == Decision::Sequential {}
        p.record(false);
        feed(&mut p, &[false; WINDOW as usize - 1]);
        assert_eq!(p.cooldown(), BASE_COOLDOWN);
    }

    /// A probe that is not entirely redone gets the full judgement: two of
    /// three redone is not enough to give up, six attempts decide.
    #[test]
    fn a_probe_with_a_clean_attempt_is_judged_on_the_full_minimum() {
        let mut p = Policy::default();
        feed(&mut p, &[true; MIN_SAMPLES as usize]);
        while p.decide() == Decision::Sequential {}
        assert!(p.record(true).is_none());
        for redo in [false, true] {
            assert_eq!(p.decide(), Decision::Parallel);
            assert!(p.record(redo).is_none());
        }
        // 2 of 3 so far; 3 of 6 is not more than half, 4 of 6 is.
        for (redo, switch) in [(true, false), (false, false), (true, true)] {
            assert_eq!(p.decide(), Decision::Parallel);
            assert_eq!(p.record(redo).is_some(), switch);
        }
    }

    /// Conflicts that stop: after the probe the window fills with clean
    /// attempts and the policy stays parallel.
    #[test]
    fn it_returns_to_parallel_when_the_conflicts_stop() {
        let mut p = Policy::default();
        feed(&mut p, &[true; MIN_SAMPLES as usize]);
        while p.decide() == Decision::Sequential {}
        // The probe was handed out; the changes are gone.
        let mut switched = p.record(false);
        for _ in 0..100 {
            assert_eq!(p.decide(), Decision::Parallel);
            switched = switched.or(p.record(false));
        }
        assert!(switched.is_none());
        assert!(!p.sequential());
    }

    /// The window slides: redos older than `WINDOW` attempts are forgotten,
    /// and only the newest `WINDOW` outcomes are counted.
    #[test]
    fn the_window_slides_and_forgets_old_redos() {
        let mut p = Policy::default();
        // 8 clean, 5 redone, 3 clean: a full window with 5 redos (never > half).
        assert!(feed(&mut p, &[false; 8]).iter().all(Option::is_none));
        assert!(feed(&mut p, &[true; 5]).iter().all(Option::is_none));
        assert!(feed(&mut p, &[false; 3]).iter().all(Option::is_none));
        assert_eq!((p.len, p.history.count_ones()), (WINDOW, 5));
        // The 8 oldest outcomes were clean: the redos stay for 8 more attempts...
        for _ in 0..8 {
            feed(&mut p, &[false]);
            assert_eq!(p.history.count_ones(), 5);
        }
        // ...then each further attempt pushes one of them out.
        for gone in 1..=5 {
            feed(&mut p, &[false]);
            assert_eq!(p.history.count_ones(), 5 - gone);
        }
        assert_eq!(p.len, WINDOW, "the window never grows past WINDOW");
        assert_eq!(p.switches, 0);
    }

    /// Threshold at a full window: 8 of 16 is not more than half; the
    /// attempt that makes it 9 of 16 switches. And at the minimum: 3 of 6
    /// stays, 4 of 6 switches.
    #[test]
    fn the_redo_threshold_is_strictly_more_than_half() {
        let mut p = Policy::default();
        feed(&mut p, &[false; 8]);
        feed(&mut p, &[true; 7]);
        assert_eq!(p.record_next(true), None, "8 of 16 stays parallel");
        assert_eq!((p.len, p.history.count_ones()), (WINDOW, 8));
        // The oldest outcome was clean and drops out: 9 of 16.
        assert_eq!(p.record_next(true), Some((WINDOW, 9)));
        assert!(p.sequential());

        let mut p = Policy::default();
        let r = feed(&mut p, &[true, false, true, false, true, false]);
        assert!(r.iter().all(Option::is_none), "3 of 6 stays");
        let mut p = Policy::default();
        let r = feed(&mut p, &[true, false, true, true, false, true]);
        assert_eq!(r[5], Some((6, 4)), "4 of 6 switches");
    }

    /// Alternation: a redo share above half switches, one at or below it
    /// never does, whatever the phase of the pattern.
    #[test]
    fn alternating_clean_and_redo_follows_the_share() {
        for phase in 0..3 {
            let mut p = Policy::default();
            let two_in_three: Vec<bool> = (0..60).map(|i| (i + phase) % 3 != 0).collect();
            assert!(
                feed_until_switch(&mut p, &two_in_three).is_some(),
                "2 of 3 redone must switch (phase {phase})"
            );
            let mut p = Policy::default();
            let one_in_three: Vec<bool> = (0..300).map(|i| (i + phase) % 3 == 0).collect();
            assert!(feed(&mut p, &one_in_three).iter().all(Option::is_none));
            assert_eq!(p.switches, 0);
        }
    }

    /// The conflicts disappear in the middle of a long run: after the
    /// cooldown the probe is clean, the policy stays parallel for good and
    /// the cooldown is back to the base value once a window is healthy.
    #[test]
    fn recovery_after_conflicts_vanish_resets_the_cooldown() {
        let mut p = Policy::default();
        feed(&mut p, &[true; MIN_SAMPLES as usize]);
        for _ in 0..2 {
            // Two failed probes: the cooldown grows to 120.
            while p.decide() == Decision::Sequential {}
            p.record(true);
            for _ in 1..PROBE_GIVE_UP {
                assert_eq!(p.decide(), Decision::Parallel);
                p.record(true);
            }
        }
        assert_eq!(p.cooldown(), MAX_COOLDOWN);
        while p.decide() == Decision::Sequential {}
        p.record(false);
        feed(&mut p, &[false; 40]);
        assert!(!p.sequential());
        assert_eq!(p.cooldown(), BASE_COOLDOWN);
        assert_eq!(p.switches, 3);
    }

    /// 100 000 steps through phases of no, rare, mixed and constant conflicts:
    /// the invariants of the policy hold at every step.
    #[test]
    fn long_run_keeps_the_invariants() {
        let mut p = Policy::default();
        let mut rng = 0x2545_F491_4F6C_DD1Du64;
        let mut next = move || {
            rng ^= rng << 13;
            rng ^= rng >> 7;
            rng ^= rng << 17;
            (rng >> 11) as f64 / (1u64 << 53) as f64
        };
        let (mut run, mut switched, mut sequential_run) = (0u64, 0u64, None::<u32>);
        for step in 0..100_000u64 {
            let share = [0.0, 0.1, 0.5, 0.9, 1.0][(step / 5_000 % 5) as usize];
            match p.decide() {
                Decision::Sequential => run += 1,
                Decision::Parallel => {
                    if run > 0 {
                        // A sequential stretch is exactly one cooldown long.
                        assert_eq!(Some(run as u32), sequential_run);
                        run = 0;
                    }
                    let redone = next() < share;
                    if let Some((len, redos)) = p.record(redone) {
                        switched += 1;
                        assert!(len <= WINDOW && redos <= len);
                        assert!(redos * 2 > len || redos == len, "switch without a majority");
                        assert!(len >= PROBE_GIVE_UP);
                        sequential_run = Some(p.cooldown_at_switch());
                    }
                }
            }
            assert!((BASE_COOLDOWN..=MAX_COOLDOWN).contains(&p.cooldown()));
            assert!(p.len <= WINDOW && p.history.count_ones() <= p.len);
            assert_eq!(p.switches, switched);
            assert!(p.probes <= p.switches);
        }
        assert!(p.switches > 0, "the conflict phases must have switched");
    }

    impl Policy {
        /// `record` one outcome, in parallel mode (test helper).
        fn record_next(&mut self, redone: bool) -> Option<(u32, u32)> {
            assert_eq!(self.decide(), Decision::Parallel);
            self.record(redone)
        }
        /// The cooldown that the switch just recorded is using.
        fn cooldown_at_switch(&self) -> u32 {
            match self.mode {
                Mode::Sequential(n) => n,
                Mode::Parallel => unreachable!(),
            }
        }
    }

    /// One server thread ticks several dimensions in turn and they share one
    /// `Policy` (it lives in the thread's TLS). `Levels` plays that loop: per
    /// server tick every dimension gets one pass, in order; a pass is either
    /// a parallel attempt (decided, run, recorded) or sequential, never both
    /// and never neither.
    struct Levels {
        policy: Policy,
        parallel: Vec<u64>,
        sequential: Vec<u64>,
        ticks: u64,
    }
    impl Levels {
        fn new(dims: usize) -> Self {
            Self {
                policy: Policy::default(),
                parallel: vec![0; dims],
                sequential: vec![0; dims],
                ticks: 0,
            }
        }
        /// One server tick. `redo[d]`: dimension `d`'s attempt needs a nav
        /// redo. `attempts[d]`: it can run in parallel at all (else the pass
        /// is a gate fallback that never reaches the policy).
        fn tick(&mut self, redo: &[bool], attempts: &[bool]) {
            for d in 0..self.parallel.len() {
                if attempts[d] && self.policy.decide() == Decision::Parallel {
                    self.parallel[d] += 1;
                    self.policy.record(redo[d]);
                } else {
                    self.sequential[d] += 1;
                }
            }
            self.ticks += 1;
        }
        fn run(&mut self, ticks: u64, redo: &[bool], attempts: &[bool]) {
            for _ in 0..ticks {
                self.tick(redo, attempts);
            }
        }
        /// Each dimension had exactly one pass per server tick, whichever path it took.
        fn assert_every_pass_ran_once(&self) {
            for d in 0..self.parallel.len() {
                assert_eq!(
                    self.parallel[d] + self.sequential[d],
                    self.ticks,
                    "dimension {d}"
                );
            }
        }
        fn sequential_total(&self) -> u64 {
            self.sequential.iter().sum()
        }
    }

    /// The Overworld (redo every pass) ticks before the Nether (clean). Their
    /// attempts alternate redo, clean, redo...: seven attempts hold four
    /// redos, which is more than half, so the shared policy switches once.
    /// After the cooldown the probe starts with the clean dimension (the
    /// cooldown has an even length), the pattern is then clean, redo, clean...
    /// which never exceeds half: parallel from then on, for both dimensions,
    /// and one cooldown (30 passes) is all it cost.
    #[test]
    fn a_redo_heavy_dimension_ticked_first_costs_one_cooldown_then_both_run_parallel() {
        let mut s = Levels::new(2);
        s.run(3, &[true, false], &[true, true]);
        assert_eq!(
            s.policy.switches, 0,
            "6 attempts: 3 of 6 is not more than half"
        );
        s.tick(&[true, false], &[true, true]);
        assert_eq!(
            (s.policy.switches, s.sequential_total()),
            (1, 1),
            "the 7th attempt: 4 of 7"
        );
        s.run(2_000, &[true, false], &[true, true]);
        assert_eq!((s.policy.switches, s.policy.probes), (1, 1));
        assert_eq!(
            s.sequential_total(),
            BASE_COOLDOWN as u64,
            "one cooldown in all"
        );
        s.assert_every_pass_ran_once();
    }

    /// The Nether (clean) ticks before the redo-heavy dimension: the pattern
    /// is clean, redo, clean...: never more than half, the policy never switches.
    #[test]
    fn the_same_pair_in_the_other_order_never_switches() {
        let mut s = Levels::new(2);
        s.run(5_000, &[false, true], &[true, true]);
        assert_eq!(s.policy.switches, 0);
        assert_eq!(s.sequential_total(), 0);
        s.assert_every_pass_ran_once();
    }

    /// A third, clean dimension dilutes the conflicts to a third.
    #[test]
    fn a_conflict_among_three_dimensions_never_switches() {
        let mut s = Levels::new(3);
        s.run(5_000, &[true, false, false], &[true, true, true]);
        assert_eq!(s.policy.switches, 0);
        s.assert_every_pass_ran_once();
    }

    /// The redo-heavy dimension is the only one that reaches the policy (the
    /// other cannot attempt: a gate fallback). Then the policy switches after
    /// the minimum, the clean one's passes are sequential anyway, and a probe
    /// that finds the conflicts still there is cut short with a longer wait.
    /// Every pass runs exactly once.
    #[test]
    fn a_lone_attempting_dimension_switches_and_backs_off_while_the_other_is_unaffected() {
        let mut s = Levels::new(2);
        s.run(MIN_SAMPLES as u64, &[true, false], &[true, false]);
        assert_eq!(s.policy.switches, 1);
        let before = s.sequential[0];
        // The cooldown is spent on the redo-heavy dimension's own passes only.
        s.run(BASE_COOLDOWN as u64, &[true, false], &[true, false]);
        assert_eq!(s.sequential[0] - before, BASE_COOLDOWN as u64);
        assert_eq!(s.policy.probes, 0);
        // The probe (three redone attempts) is cut short; the next wait is doubled.
        s.run(PROBE_GIVE_UP as u64, &[true, false], &[true, false]);
        assert_eq!((s.policy.switches, s.policy.probes), (2, 1));
        let before = s.sequential[0];
        s.run(2 * BASE_COOLDOWN as u64, &[true, false], &[true, false]);
        assert_eq!(s.sequential[0] - before, 2 * BASE_COOLDOWN as u64);
        // The other dimension never attempted: all its passes were sequential, none lost.
        assert_eq!(s.parallel[1], 0);
        s.assert_every_pass_ran_once();
    }

    /// The conflicts stop in the middle of a cooldown: the probe is clean,
    /// both dimensions are parallel again right after it, and the policy
    /// returns to the short cooldown.
    #[test]
    fn both_dimensions_return_to_parallel_when_the_conflicts_stop() {
        let mut s = Levels::new(2);
        s.run(MIN_SAMPLES as u64, &[true, false], &[true, false]);
        s.run(10, &[false, false], &[true, true]);
        let seq_at_clear = s.sequential_total();
        s.run(100, &[false, false], &[true, true]);
        // 20 of the 30 cooldown passes were spent in the 10 ticks before; the other 10 follow,
        // then the probe is clean and everything is parallel; nothing is switched off again.
        assert_eq!(
            s.sequential_total() - seq_at_clear,
            BASE_COOLDOWN as u64 - 20
        );
        assert!(!s.policy.sequential());
        assert_eq!(s.policy.switches, 1);
        s.assert_every_pass_ran_once();
    }
    /// A policy left in a cooldown (a world closed, a level swapped in on the same
    /// thread) is simply carried on: it hands out parallel again after the
    /// cooldown's passes, from whichever dimension asks.
    #[test]
    fn state_survives_a_world_change_on_the_same_thread() {
        let mut p = Policy::default();
        feed(&mut p, &[true; MIN_SAMPLES as usize]);
        assert!(p.sequential());
        // "A new world": the next passes come from another level; nothing is reset.
        let mut sequential = 0;
        while p.decide() == Decision::Sequential {
            sequential += 1;
        }
        assert_eq!(sequential, BASE_COOLDOWN);
        assert_eq!((p.switches, p.probes), (1, 1));
    }
    fn feed_until_switch(p: &mut Policy, pattern: &[bool]) -> Option<(u32, u32)> {
        pattern.iter().find_map(|&r| p.record_next(r))
    }
}
