#!/usr/bin/env python3
"""Differential mob AI harness.

The Java oracle (VanillaMobAiOracle) runs vanilla mobs in a real, headless, flat vanilla server
level: each scenario builds a scene, places a target player and mobs with seeded random sources
and ticks the mobs the way ServerLevel.tick does. The Rust probe (mob_ai_probe) replays the same
scenarios on Horizon's mob AI. Both print every mob's AI-visible state after every tick (position,
motion, rotations, running goals and target goals, attack target, navigation path, move and look
controls, movement input, brain activities and memories) and the sounds played; this harness
reports, per scenario and mob, how many ticks match before the first divergence and what differs.

  python3 tools/test_mob_ai_parity.py [--only NAME ...] [--rust-only] [--java-only] [--report-only] [--trace]

The oracle traces every mob random draw with its call stack; --trace builds the probe with
the rng-trace feature and line tables so the report shows Horizon's draws next to vanilla's.
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

try:
    from . import lab, oracle_harness, test_rust_parity as base, test_model_baking_parity as baking
except ImportError:
    import lab
    import oracle_harness
    import test_rust_parity as base
    import test_model_baking_parity as baking

FLOOR = -60  # top of the flat preset's grass layer + 1


def ring(x0, z0, size, state, y=FLOOR):
    return [f'fill {x0 - size} {y} {z0 - size} {x0 + size} {y} {z0 + size} {state}']


# Each scenario is (name, ticks, options, lines); positions are relative to the scenario origin
# and translated by the generator so scenarios never share chunks.
def scenarios():
    s = []

    def add(name, ticks, lines, opts=''):
        s.append((name, ticks, opts, lines))

    # Passive and hostile land mobs on open grass with a survival player nearby.
    for species, seed in [('zombie', 1), ('skeleton', 2), ('creeper', 3), ('spider', 4), ('husk', 5),
                          ('cow', 6), ('pig', 7), ('chicken', 8), ('sheep', 9), ('wandering_trader', 10),
                          ('villager', 11), ('drowned', 12), ('enderman', 13), ('witch', 14)]:
        add(f'{species}_player', 200, [
            'player 6.5 -60 0.5',
            f'mob minecraft:{species} 0.5 -60 0.5 0 {seed} persistent',
        ])
        add(f'{species}_alone', 200, [
            f'mob minecraft:{species} 0.5 -60 0.5 90 {seed * 7 + 1} persistent',
        ])
    # Drowned in a pool with the player on the shore and in the water.
    add('drowned_pool', 200, ['fill -6 -64 -6 6 -61 6 minecraft:water',
                              'player 9.5 -60 0.5',
                              'mob minecraft:drowned 0.5 -63 0.5 0 21 persistent'])
    add('drowned_pool_swimmer', 200, ['fill -6 -64 -6 6 -61 6 minecraft:water',
                                      'player 3.5 -62 0.5',
                                      'mob minecraft:drowned -3.5 -63 0.5 0 22 persistent'])
    # Night: undead do not burn, monsters seek darkness.
    add('zombie_night', 200, ['player 10.5 -60 0.5', 'mob minecraft:zombie 0.5 -60 0.5 0 31 persistent'], 'time=18000')
    add('skeleton_day_burn', 120, ['mob minecraft:skeleton 0.5 -60 0.5 0 32 persistent'])
    # The wither (summoned, not built: no invulnerable spawn phase).
    add('wither_player', 200, ['player 10.5 -60 0.5', 'mob minecraft:wither 0.5 -58 0.5 0 41 persistent'])
    # A villager with a job site, a bed and a bell.
    add('villager_village', 300, [
        'block 4 -60 0 minecraft:composter',
        'block -4 -60 2 minecraft:red_bed[facing=north,part=foot]',
        'block -4 -60 1 minecraft:red_bed[facing=north,part=head]',
        'block 0 -60 -6 minecraft:bell',
        'player 12.5 -60 0.5',
        'mob minecraft:villager 0.5 -60 0.5 0 51 persistent',
    ])
    # The warden: idle, sniffing out a player, roaring and fighting (WardenAi).
    # Without finalizeSpawn's dig cooldown a warden digs straight back down. warden_player
    # and warden_cow stop before the first blow kills the player or the cow (the probe has no dying
    # player, nor the attack knockback attribute or a target killed within the attacker's tick).
    add('warden_digs', 140, ['mob minecraft:warden 0.5 -60 0.5 0 60'])
    add('warden_alone', 400, ['mob minecraft:warden 0.5 -60 0.5 0 61 digcooldown'])
    add('warden_player', 565, ['player 5.5 -60 0.5', 'mob minecraft:warden 0.5 -60 0.5 0 62 digcooldown'])
    add('warden_player_far', 600, ['player 14.5 -60 3.5', 'mob minecraft:warden 0.5 -60 0.5 90 63 digcooldown'])
    add('warden_creative', 300, ['player 4.5 -60 0.5 creative', 'mob minecraft:warden 0.5 -60 0.5 0 64 digcooldown'])
    add('warden_persistent', 400, ['player 7.5 -60 0.5', 'mob minecraft:warden 0.5 -60 0.5 0 65 persistent digcooldown'])
    add('warden_emerging', 300, ['player 6.5 -60 0.5', 'mob minecraft:warden 0.5 -60 0.5 0 66 emerge'])
    add('warden_cow', 176, ['mob minecraft:warden 0.5 -60 0.5 0 67 digcooldown', 'mob minecraft:cow 4.5 -60 0.5 0 68 persistent'])
    # Two villagers gossip (TradeWithVillager): what one heard of the player
    # passes to the other, weakened, once in 1200 ticks; idle and at the bell.
    gossips = ('gossip=player:minor_negative:100 gossip=player:major_positive:15 '
               'gossip=player:trading:20 gossip=player:minor_positive:3')
    for name, opts, extra in [('villager_gossip_idle', 'time=100', []),
                              ('villager_gossip_bell', 'time=9500', ['block 0 -60 -6 minecraft:bell'])]:
        add(name, 400, extra + [
            'player 14.5 -60 0.5',
            f'mob minecraft:villager 0.5 -60 0.5 0 61 persistent {gossips}',
            'mob minecraft:villager 2.5 -60 0.5 180 62 persistent gossip=player:minor_positive:50',
        ], opts)
    # Two villagers with bread breed (VillagerMakeLove): they eat, a baby is
    # born and takes a free bed, the parents rest for 6000 ticks.
    # PoiManager.take hands out the free beds of a section in hash order:
    # the parents claim the two near them, the child gets the only one left.
    beds = []
    for x in (-6, 3, 6):
        beds += [f'block {x} -60 6 minecraft:red_bed[facing=north,part=foot]',
                 f'block {x} -60 5 minecraft:red_bed[facing=north,part=head]']
    for name, seeds in [('villager_breed', (71, 72)), ('villager_breed_2', (73, 74))]:
        add(name, 900, beds + [
            'player 14.5 -60 0.5',
            f'mob minecraft:villager 0.5 -60 0.5 0 {seeds[0]} persistent inv=bread*3',
            f'mob minecraft:villager 2.5 -60 0.5 180 {seeds[1]} persistent inv=bread*3 food=4',
        ], 'time=100')
    # Too little food: no breeding.
    add('villager_breed_hungry', 400, beds[:2] + [
        'mob minecraft:villager 0.5 -60 0.5 0 75 persistent inv=bread*2',
        'mob minecraft:villager 2.5 -60 0.5 180 76 persistent inv=bread*3',
    ], 'time=100')
    # Breezes: idle, and fighting a player near, in, and beyond the inner jump circle.
    add('breeze_alone', 200, ['mob minecraft:breeze 0.5 -60 0.5 90 61 persistent'])
    add('breeze_player', 200, ['player 8.5 -60 0.5', 'mob minecraft:breeze 0.5 -60 0.5 0 62 persistent'])
    add('breeze_close', 200, ['player 2.5 -60 1.5', 'mob minecraft:breeze 0.5 -60 0.5 0 63 persistent'])
    add('breeze_far', 200, ['player 13.5 -60 6.5', 'mob minecraft:breeze 0.5 -60 0.5 0 64 persistent'])
    # Creakings: the player faces +z, so it watches one and turns its back on the other.
    add('creaking_alone', 200, ['mob minecraft:creaking 0.5 -60 0.5 90 71 persistent'])
    add('creaking_watched', 200, ['player 0.5 -60 -7.5', 'mob minecraft:creaking 0.5 -60 0.5 180 72 persistent'])
    add('creaking_behind', 200, ['player 0.5 -60 8.5', 'mob minecraft:creaking 0.5 -60 0.5 0 73 persistent'])
    # Woken while watched, then free to hunt and hit once the player turns away.
    add('creaking_attack', 240, ['player 0.5 -60 -7.5 turn=40,180', 'mob minecraft:creaking 0.5 -60 0.5 180 74 persistent'])
    # A creaking heart in a pale oak trunk: by night it spawns its creaking near a player,
    # by day it sleeps, without logs it is uprooted, with no player near it waits.
    TRUNK = ['block 3 -60 3 minecraft:pale_oak_log[axis=y]', 'block 3 -58 3 minecraft:pale_oak_log[axis=y]']
    add('heart_night', 300, TRUNK + ['heart 3 -59 3 minecraft:creaking_heart[axis=y,creaking_heart_state=awake,natural=true]', 'player 0.5 -60 -6.5'], 'time=18000 spawning')
    add('heart_day', 120, TRUNK + ['heart 3 -59 3 minecraft:creaking_heart[axis=y,creaking_heart_state=awake,natural=true]', 'player 0.5 -60 -6.5'], 'spawning')
    add('heart_uprooted', 60, ['heart 3 -59 3 minecraft:creaking_heart[axis=y,creaking_heart_state=awake,natural=true]', 'player 0.5 -60 -6.5'], 'time=18000 spawning')
    # A player hits the heart's creaking: no damage, but the heart bleeds resin and trails.
    add('heart_hurt', 260, TRUNK + ['heart 3 -59 3 minecraft:creaking_heart[axis=y,creaking_heart_state=awake,natural=true]', 'player 0.5 -60 -6.5',
                            'hurt 60 0', 'hurt 64 0', 'hurt 200 0'], 'time=18000 spawning')
    add('heart_far', 120, TRUNK + ['heart 3 -59 3 minecraft:creaking_heart[axis=y,creaking_heart_state=awake,natural=true]', 'player 40.5 -60 0.5'], 'time=18000 spawning')
    # Llamas and trader llamas: strolling, looking at the player, a baby following its parent and a
    # llama hunting a wolf (LlamaAttackWolfGoal) and spitting at it.
    for species, seed in [('llama', 61), ('trader_llama', 62)]:
        add(f'{species}_player', 200, ['player 6.5 -60 0.5', f'mob minecraft:{species} 0.5 -60 0.5 0 {seed} persistent'])
        add(f'{species}_alone', 200, [f'mob minecraft:{species} 0.5 -60 0.5 90 {seed * 7 + 1} persistent'])
    add('llama_family', 200, ['mob minecraft:llama 0.5 -60 0.5 0 63 persistent',
                              'mob minecraft:llama 6.5 -60 2.5 0 64 persistent baby'])
    add('llama_wolf', 200, ['mob minecraft:llama 0.5 -60 0.5 0 65 persistent',
                            'mob minecraft:wolf 8.5 -60 0.5 0 66 persistent'])
    # Sniffers on diggable grass: scenting, sniffing, searching and digging.
    add('sniffer_player', 400, ['player 8.5 -60 0.5', 'mob minecraft:sniffer 0.5 -60 0.5 0 71 persistent'])
    for i, seed in enumerate([72, 73, 74]):
        add(f'sniffer_alone_{i}', 600, [f'mob minecraft:sniffer 0.5 -60 0.5 {90 * i} {seed} persistent'])
    add('sniffer_baby', 300, ['mob minecraft:sniffer 0.5 -60 0.5 0 75 persistent baby'])
    # Happy ghasts float above the grass; ghastlings follow players and adults.
    add('happy_ghast_player', 200, ['player 6.5 -60 0.5', 'mob minecraft:happy_ghast 0.5 -55 0.5 0 81 persistent'])
    add('happy_ghast_alone', 200, ['mob minecraft:happy_ghast 0.5 -55 0.5 90 82 persistent'])
    # The driver's seat overlaps the front of the ghast: it must not push its own rider.
    # One tick isolates riding input and turning from later passenger repositioning.
    for name, yaw in [('hover', 0), ('turn', 90)]:
        add(f'happy_ghast_ridden_{name}', 1, ['player 0.5 -51.6 2.2',
            f'mob minecraft:happy_ghast 0.5 -55 0.5 {yaw} 82 persistent ridden'])
    add('ghastling_player', 200, ['player 8.5 -60 0.5', 'mob minecraft:happy_ghast 0.5 -57 0.5 0 83 persistent baby'])
    add('ghastling_alone', 200, ['mob minecraft:happy_ghast 0.5 -57 0.5 0 84 persistent baby'])
    add('ghastling_adult', 200, ['mob minecraft:happy_ghast 10.5 -55 0.5 0 85 persistent',
                                 'mob minecraft:happy_ghast 0.5 -57 0.5 0 86 persistent baby'])
    # Breeding, temptation and growing up.
    add('sniffer_love', 300, ['mob minecraft:sniffer 0.5 -60 0.5 0 91 persistent inlove',
                              'mob minecraft:sniffer 4.5 -60 0.5 0 92 persistent inlove'])
    add('sniffer_tempted', 200, ['player 7.5 -60 0.5 hand=minecraft:torchflower_seeds',
                                 'mob minecraft:sniffer 0.5 -60 0.5 0 93 persistent'])
    add('llama_love', 300, ['mob minecraft:llama 0.5 -60 0.5 0 94 persistent tame inlove strength=3',
                            'mob minecraft:llama 4.5 -60 0.5 0 95 persistent tame inlove strength=2'])
    add('llama_tempted', 200, ['player 7.5 -60 0.5 hand=minecraft:hay_block',
                               'mob minecraft:llama 0.5 -60 0.5 0 96 persistent'])
    add('ghastling_tempted', 200, ['player 8.5 -60 0.5 hand=minecraft:snowball',
                                   'mob minecraft:happy_ghast 0.5 -57 0.5 0 97 persistent baby'])
    add('happy_ghast_tempted', 200, ['player 12.5 -60 0.5 hand=minecraft:snowball',
                                     'mob minecraft:happy_ghast 0.5 -55 0.5 0 98 persistent'])
    add('ghastling_grows', 200, ['player 8.5 -60 0.5',
                                 'mob minecraft:happy_ghast 0.5 -57 0.5 0 99 persistent age=-40'])
    # Llama caravans: two llamas fall in behind one on the player's lead; a wandering trader's two
    # llamas on its leads.
    add('llama_caravan', 300, ['player 0.5 -60 0.5',
                               'mob minecraft:llama 3.5 -60 0.5 0 101 persistent leashed strength=3',
                               'mob minecraft:llama 9.5 -60 2.5 0 102 persistent strength=2',
                               'mob minecraft:llama 13.5 -60 -1.5 0 103 persistent strength=1'])
    add('trader_llamas', 300, ['player 14.5 -60 0.5',
                               'mob minecraft:wandering_trader 0.5 -60 0.5 0 104 persistent',
                               'mob minecraft:trader_llama 3.5 -60 1.5 0 105 persistent leashto=0 strength=2',
                               'mob minecraft:trader_llama 3.5 -60 -1.5 0 106 persistent leashto=0 strength=4'])
    # Copper golems sorting chests (TransportItemsBetweenContainers): from a copper chest to a chest,
    # skipping a chest without the carried item, queueing at a chest another golem has open, a double
    # chest, a blocked copper chest, a part-full chest, and idling with nothing to carry.
    def chest(x, z, slots='-', kind='chest', facing='north', half='single', y=-60):
        return f'chest {x} {y} {z} minecraft:{kind}[facing={facing},type={half},waterlogged=false] {slots}'
    golem = 'mob minecraft:copper_golem {} -60 {} {} {} persistent cooldown=0'
    add('golem_copper_to_chest', 600, [chest(4, 0, 'cobblestone*20,-,iron_ingot*5', 'copper_chest'),
                                       chest(-4, 3), golem.format(0.5, 0.5, 0, 111)])
    add('golem_skips_other_items', 700, [chest(4, 0, 'cobblestone*40', 'copper_chest'),
                                         chest(-3, 0, 'dirt*3'), chest(-6, 5, 'cobblestone*10'),
                                         golem.format(0.5, 0.5, 90, 112)])
    add('golem_queue', 600, [chest(3, 0, 'oak_log*48,stick*7', 'copper_chest'), chest(-4, 0),
                             golem.format(0.5, 0.5, 0, 113), golem.format(0.5, 3.5, 180, 114)])
    add('golem_double_chest', 600, [chest(4, 0, 'apple*30', 'exposed_copper_chest'),
                                    chest(-4, 2, '-', 'chest', 'north', 'left'), chest(-5, 2, 'apple*2', 'chest', 'north', 'right'),
                                    golem.format(0.5, 0.5, 0, 115)])
    add('golem_blocked_chest', 400, [chest(3, 0, 'sand*5', 'copper_chest'), 'block 3 -59 0 minecraft:stone',
                                     chest(-5, -2, 'gravel*9', 'oxidized_copper_chest'), chest(0, 6),
                                     golem.format(0.5, 0.5, 0, 116)])
    add('golem_part_full', 500, [chest(-3, 1, 'cobblestone*60,cobblestone*64,-,cobblestone*63'),
                                 'mob minecraft:copper_golem 0.5 -60 0.5 0 117 persistent cooldown=0 hand=cobblestone*16'])
    add('golem_idle', 400, ['player 4.5 -60 2.5', 'mob minecraft:copper_golem 0.5 -60 0.5 0 118 persistent'])
    # The player feeds animals their food (Animal.mobInteract): an adult falls in love once, a baby
    # grows each time, and each plays its own eating sound or none (Animal.playEatingSound).
    for species, food, seed in [('cow', 'wheat', 120), ('pig', 'carrot', 121), ('sheep', 'wheat', 122),
                                ('chicken', 'wheat_seeds', 123)]:
        for baby in ('', ' baby'):
            add(f'feed_{species}{baby.replace(" ", "_")}', 80, [
                'player 3.5 -60 0.5',
                f'mob minecraft:{species} 0.5 -60 0.5 0 {seed} persistent{baby}',
                f'feed 20 0 minecraft:{food}', f'feed 40 0 minecraft:{food}',
            ])
    # Striders: standing on a lava pool (floatStrider), walking from cold grass to it
    # (StriderGoToLavaGoal, suffocating), and drawn by a player holding warped fungus.
    LAVA = 'fill -5 -63 -5 5 -61 5 minecraft:lava'
    add('strider_lava', 200, [LAVA, 'mob minecraft:strider 0.5 -61 0.5 0 131 persistent'])
    add('strider_lava_player', 200, [LAVA, 'player 8.5 -60 0.5',
                                     'mob minecraft:strider -1.5 -61 0.5 90 132 persistent'])
    add('strider_shore', 300, [LAVA, 'mob minecraft:strider 9.5 -60 0.5 90 133 persistent'])
    add('strider_cold', 200, ['mob minecraft:strider 0.5 -60 0.5 0 134 persistent'])
    add('strider_tempted', 200, [LAVA, 'player 9.5 -60 0.5 hand=minecraft:warped_fungus',
                                 'mob minecraft:strider 0.5 -61 0.5 0 135 persistent'])
    # Bats: hanging under a stone ceiling alone, woken by a player passing beneath, flying off with
    # nothing above, and fluttering about a closed cave where they may hang up again.
    CEILING = 'fill -6 -57 -6 6 -57 6 minecraft:stone'
    add('bat_hanging', 200, [CEILING, 'mob minecraft:bat 0.5 -57.9 0.5 0 141 persistent'])
    add('bat_woken', 200, [CEILING, 'player 1.5 -60 0.5', 'mob minecraft:bat 0.5 -57.9 0.5 0 142 persistent'])
    add('bat_flying', 300, ['mob minecraft:bat 0.5 -57 0.5 0 143 persistent'])
    CAVE = ['fill -5 -60 -5 5 -54 5 minecraft:stone', 'fill -4 -60 -4 4 -55 4 minecraft:air']
    for i, seed in enumerate([144, 145, 146]):
        add(f'bat_cave_{i}', 400, CAVE + [f'mob minecraft:bat 0.5 -58 0.5 {90 * i} {seed} persistent'])
    # Bees: wandering over grass (HoverRandomPos), pollinating a flower bed until they carry nectar,
    # drawn to a player holding a flower, staying home at night and in the rain (no hive to find),
    # drowning in a pool, and angered by a hit: they chase and sting the player once, then die of it.
    FLOWERS = 'fill 3 -60 2 5 -60 4 minecraft:dandelion'
    for i, seed in enumerate([151, 152]):
        add(f'bee_alone_{i}', 400, [f'mob minecraft:bee 0.5 -58 0.5 {90 * i} {seed} persistent'])
    for i, seed in enumerate([153, 154]):
        add(f'bee_flowers_{i}', 700, [FLOWERS, f'mob minecraft:bee 0.5 -58 0.5 {180 * i} {seed} persistent'])
    add('bee_tempted', 200, ['player 6.5 -60 0.5 hand=minecraft:dandelion', 'mob minecraft:bee 0.5 -58 0.5 0 155 persistent'])
    add('bee_night', 300, [FLOWERS, 'mob minecraft:bee 0.5 -58 0.5 0 156 persistent'], 'time=18000')
    add('bee_rain', 300, [FLOWERS, 'mob minecraft:bee 0.5 -58 0.5 0 157 persistent'], 'rain')
    add('bee_pool', 200, ['fill -3 -63 -3 3 -61 3 minecraft:water', 'mob minecraft:bee 0.5 -62 0.5 0 158 persistent'])
    add('bee_sting', 400, ['player 4.5 -60 0.5', 'mob minecraft:bee 0.5 -58 0.5 0 159 persistent', 'hurt 20 0'])
    # Bees and their hive: home at night and in the rain (BeeLocateHiveGoal, BeeGoToHiveGoal,
    # BeeEnterHiveGoal), out again once the morning comes, and nectar turned into honey.
    HIVE = 'block 4 -60 0 minecraft:beehive[facing=west,honey_level=0]'
    add('bee_hive_night', 300, [HIVE, 'mob minecraft:bee 0.5 -58 0.5 0 161 persistent'], 'time=13000')
    add('bee_hive_rain', 300, [HIVE, 'mob minecraft:bee 0.5 -58 0.5 0 162 persistent'], 'rain')
    add('bee_hive_morning', 1000, [HIVE, 'mob minecraft:bee 0.5 -58 0.5 0 163 persistent'], 'time=22900 daycycle')
    add('bee_hive_honey', 2600, [HIVE, 'mob minecraft:bee 0.5 -58 0.5 0 164 persistent nectar'])
    # Mobs dropped into deep water (#542): FloatGoal users float at the surface (their navigation
    # can float, so Mob.jumpInLiquid adds 0.04 a tick), zombies sink.
    for species, seed in [('pig', 171), ('cow', 172), ('wolf', 173), ('cat', 174), ('horse', 175),
                          ('witch', 176), ('vindicator', 177), ('zombie', 178)]:
        add(f'{species}_deep_water', 160, ['fill -4 -64 -4 4 -61 4 minecraft:water',
                                           f'mob minecraft:{species} 0.5 -62 0.5 0 {seed} persistent'])
    # Axolotls (#64): idle swimming, hunting a cod, and playing dead after hits, in a pool sunk into
    # the ground so no water flows out of it (the probe doesn't tick fluids).
    POOL = 'fill -5 -63 -5 5 -61 5 minecraft:water'
    add('axolotl_hunting', 45, [POOL, 'player 12.5 -60 0.5', 'mob minecraft:axolotl 0.5 -62 0.5 0 12 persistent',
                                'mob minecraft:cod 2.5 -62 0.5 0 13 persistent noai'])
    add('axolotl_play_dead', 300, [POOL, 'player 12.5 -60 0.5', 'mob minecraft:axolotl 0.5 -62 0.5 0 28 persistent']
        + [f'hurt {t} 0' for t in range(4, 160, 12)])
    # A jobless villager outside a stone box, its composter inside and the doorway on the far
    # side: the path around needs more than the 256 nodes of the follow range (16 * 16); the
    # villager's setRequiredPathLength(48) raises PathFinder.maxVisitedNodes to 768.
    add('villager_job_in_box', 300, [
        'fill -4 -60 -4 4 -58 4 minecraft:stone',
        'fill -3 -60 -3 3 -58 3 minecraft:air',
        'fill -4 -60 0 -4 -59 0 minecraft:air',
        'block 0 -60 0 minecraft:composter',
        'mob minecraft:villager 10.5 -60 0.5 90 54 persistent',
    ])
    # Villagers that slept today and saw no golem summon an iron golem (Villager.spawnGolemIfNeeded):
    # five gossiping (TradeWithVillager, at tick 200 here), or three panicking at a zombie in a pen
    # (VillagerPanicTrigger, at tick 100 by night). They all remember it (GOLEM_DETECTED_RECENTLY).
    # Under a glass roof three blocks up the golem still fits (IronGolem.checkSpawnObstruction);
    # on dirt paths it cannot stand. The scenarios stop on the summoning tick: the golem's own AI
    # (MoveBackToVillageGoal, GolemRandomStrollInVillageGoal) is not ported yet.
    golem_callers = [f'mob minecraft:villager {x} -60 {z} {r} {seed} persistent slept=1' for x, z, r, seed in
                     [(0.5, 0.5, 0, 81), (2.5, 0.5, 180, 82), (0.5, 2.5, 90, 83), (2.5, 2.5, 270, 84), (1.5, -1.5, 0, 85)]]
    add('villager_golem_gossip', 200, ['player 14.5 -60 0.5'] + golem_callers, 'time=100')
    add('villager_golem_roof', 200, ['fill -10 -57 -12 13 -57 13 minecraft:glass', 'player 14.5 -60 0.5'] + golem_callers,
        'time=100')
    add('villager_golem_paths', 400, ['fill -10 -61 -12 13 -61 13 minecraft:dirt_path', 'player 14.5 -60 0.5'] + golem_callers,
        'time=100')
    add('villager_golem_panic', 100, ['fill -2 -60 -3 4 -60 4 minecraft:oak_fence', 'fill -1 -60 -2 3 -60 3 minecraft:air',
                                      'player 14.5 -60 0.5'] + golem_callers[:3]
        + ['mob minecraft:zombie 6.5 -60 0.5 90 86 persistent noai'], 'time=18000')
    # Entity cramming (LivingEntity.pushEntities): cows crowded into a 2x2 pit, every box meeting
    # every other. With max_entity_cramming at its default of 24, 26 cows hurt themselves one tick
    # in four for 6 and die; at 0 the rule is off (and draws nothing); at 4 a crowd of 6 is enough.
    # The pit sits in the middle of one entity section: vanilla lists a section's entities in the
    # order they entered it, and the sums of the pushes depend on that order (see
    # cows_crammed_sections for a crowd across section borders).
    def pit(c):
        return [f'fill {c - 2} -60 {c - 2} {c + 1} -59 {c + 1} minecraft:stone',
                f'fill {c - 1} -60 {c - 1} {c} -59 {c} minecraft:air']
    PIT = pit(8)

    def crowd(n, seed, c=8):
        return [f'mob minecraft:cow {c - 0.3 + 0.15 * (k % 5):.2f} -60 {c - 0.3 + 0.15 * (k // 5):.2f} '
                f'{k * 37 % 360} {seed + k} persistent' for k in range(n)]
    add('cows_crammed', 120, PIT + crowd(26, 1000))
    add('cows_crammed_off', 60, PIT + crowd(26, 1100), 'cramming=0')
    add('cows_crammed_rule', 120, PIT + crowd(6, 1200), 'cramming=4')
    # Known divergence (P1.3 F): across section borders vanilla's push order follows the sections,
    # Horizon's the mobs' order, and the velocities part in the last bit.
    add('cows_crammed_sections', 60, pit(0) + crowd(26, 1300, 0), 'cramming=0')
    # Hits and alerts act on the other mob inside the attacker's tick (P1.3 B): a hurt bee alerts the
    # bees near it (HurtByTargetGoal.alertOthers -> Mob.setTarget) and those ticking after it take up
    # the chase in the same tick (HORIZON_PROBE_LATE_HITS=1 replays the old order, which parts at tick 20).
    add('bees_alerted', 200, ['player 4.5 -60 0.5', 'mob minecraft:bee 0.5 -58 0.5 0 1401 persistent',
                              'mob minecraft:bee 2.5 -58 2.5 90 1402 persistent',
                              'mob minecraft:bee -1.5 -58 1.5 180 1403 persistent', 'hurt 20 0'])
    # Sheep and mob_griefing (P1.3 D): baby sheep (EatBlockGoal one tick in 25) penned on one grass
    # block. With griefing the first to eat turns it to dirt at once and the ones after it in the same
    # tick find none (HORIZON_PROBE_LATE_BLOCKS=1 replays the old pass); without, they all keep eating.
    # (In the middle of an entity section, as the pit above.)
    LAMBS = (['fill 7 -60 7 9 -60 9 minecraft:oak_fence', 'block 8 -60 8 minecraft:air', 'watch 8 -61 8 8 -60 8']
             + [f'mob minecraft:sheep {8.3 + 0.1 * (k % 4):.1f} -60 {8.3 + 0.1 * (k // 4):.1f} {k * 90} {1430 + k} '
                f'persistent baby' for k in range(12)])
    add('lambs_one_grass_block', 200, LAMBS)
    add('lambs_one_grass_block_nogriefing', 200, LAMBS, 'nogriefing')
    # Seven such pens side by side, short grass at the lambs' feet: two changes a pen (the grass,
    # then the grass block), each a chance for a lamb after the eater to start on a block just gone.
    PENS = ['fill 1 -60 7 15 -60 9 minecraft:oak_fence', 'watch 2 -61 8 14 -60 8']
    for p, c in enumerate(range(2, 15, 2)):
        PENS += [f'block {c} -60 8 minecraft:short_grass']
        PENS += [f'mob minecraft:sheep {c + 0.3 + 0.1 * (k % 4):.1f} -60 {8.3 + 0.2 * (k // 4):.1f} {k * 90} '
                 f'{1450 + 8 * p + k} persistent baby' for k in range(8)]
    add('lambs_seven_pens', 300, PENS)
    # The same with seeds (found by search) where a lamb after the eater, in the same tick, draws its
    # one in 25 on the block just eaten: vanilla finds it gone and so does Horizon now; the old pass
    # (HORIZON_PROBE_LATE_BLOCKS=1) let it start eating.
    PENS = ['fill 1 -60 7 15 -60 9 minecraft:oak_fence', 'watch 2 -61 8 14 -60 8']
    for p, c in enumerate(range(2, 15, 2)):
        PENS += [f'block {c} -60 8 minecraft:short_grass']
        PENS += [f'mob minecraft:sheep {c + 0.3 + 0.1 * (k % 4):.1f} -60 {8.3 + 0.2 * (k // 4):.1f} {k * 90} '
                 f'{6100 + 8 * p + k} persistent baby' for k in range(8)]
    add('lambs_same_tick', 300, PENS)
    # Melee damage inside the attacker's tick (P1.3.2, Mob.doHurtTarget -> hurtServer): zombies on both
    # sides of a 1-health wandering trader; the first kills it in its own tick, then pushes (the trader
    # is no longer pushable), and the zombie ticking after the trader sees it dead.
    # HORIZON_PROBE_POST_TICK_HITS=1 replays P1.3's order (the hit after the attacker's tick).
    # (No `noai` trader: Horizon skips a no-AI mob's whole aiStep, pushes included, where vanilla
    # only skips its goals: a known, separate divergence.)
    # The trader's box overlaps the first zombie's, so that zombie's pushEntities right after the
    # kill (tick 1) would push it if it still counted as alive.
    add('zombie_kills_trader', 200, ['mob minecraft:zombie 0.5 -60 0.5 270 1601 persistent',
                                     'mob minecraft:wandering_trader 0.9 -60 0.5 90 1602 persistent health=1',
                                     'mob minecraft:zombie 1.3 -60 0.5 90 1603 persistent'], 'time=18000')
    # A villager (a trader drinks invisibility at night, which Horizon lacks) hit again and again:
    # each hit's default knockback, hurt cooldown and sounds in the attacker's tick, and the
    # zombies' pushes on the knocked-back villager.
    add('zombie_hits_villager', 120, ['mob minecraft:zombie 0.5 -60 0.5 270 1611 persistent',
                                      'mob minecraft:villager 0.9 -60 0.5 90 1612 persistent',
                                      'mob minecraft:zombie 1.3 -60 0.5 90 1613 persistent'], 'time=18000')
    # Pending pushes before a melee hit (P1.3.2 hotfix): a cow (C) ticking first pushes the trader (B)
    # sharing a cell of a two-deep trench with it, then the zombie (A) in the next cell ticks and hits B
    # in its tick, then B ticks. Vanilla's Entity.push already moved B, so the hit's knockback (which
    # halves B's velocity) reads the pushed velocity. The cows push B every tick (several pushes before
    # one hit, pushes after one), the zombie hits as the hurt cooldown allows (refused hits too). By
    # day (a trader drinks invisibility at night, which Horizon lacks) under a roof (no burning zombie),
    # in the middle of an entity section (see cows_crammed).
    # HORIZON_PROBE_PUSH_AFTER_HIT=1 replays the order before the fix (the pushes wait for B's tick).
    def trench(cows, extra=''):
        return (['fill -4 -56 -4 20 -56 20 minecraft:stone', 'fill 7 -62 8 9 -61 8 minecraft:air'] + cows
                + ['mob minecraft:zombie 9.5 -62 8.5 90 1611 persistent',
                   f'mob minecraft:wandering_trader 8.3 -62 8.5 270 1612 persistent{extra}'])
    COW = 'mob minecraft:cow 8.55 -62 8.5 270 1621 persistent'
    add('cow_pushes_before_zombie_hit', 120, trench([COW]), 'time=1000')
    add('cows_push_before_zombie_hit', 120, trench(['mob minecraft:cow 7.6 -62 8.5 90 1622 persistent', COW]),
        'time=1000')
    # The first hit (tick 24, two pushes pending) kills: B's last velocity, death sound and the pushes
    # after (none: a dying mob is not pushable).
    add('cows_push_before_zombie_kill', 60, trench(['mob minecraft:cow 7.6 -62 8.5 90 1622 persistent', COW], ' health=3'),
        'time=1000')
    # Breeding (P1.3 C): the parent draws its baby's variant (getBreedOffspring) and then the orb
    # (finalizeSpawnChildFromBreeding, with mob_drops only) from its own random during its tick, and
    # the partner is out of love at once. In the middle of a section (as above).
    for species, seed in [('cow', 1501), ('pig', 1511), ('chicken', 1521), ('mooshroom', 1531), ('sheep', 1551)]:
        pair = [f'mob minecraft:{species} 7.5 -60 8.5 0 {seed} persistent inlove',
                f'mob minecraft:{species} 9.5 -60 8.5 90 {seed + 1} persistent inlove']
        add(f'{species}s_breed', 200, pair)
    add('cows_breed_nodrops', 200, ['mob minecraft:cow 7.5 -60 8.5 0 1501 persistent inlove',
                                    'mob minecraft:cow 9.5 -60 8.5 90 1502 persistent inlove'], 'nodrops')
    # The partner is hurt at tick 2 (Animal.actuallyHurt: out of love) and fed again at tick 6 while
    # it still panics: BreedGoal waits for the panic to end.
    add('cows_breed_panicking_partner', 200, ['player 24.5 -60 8.5',
                                              'mob minecraft:cow 7.5 -60 8.5 0 1541 persistent inlove',
                                              'mob minecraft:cow 8.5 -60 9.5 90 1542 persistent inlove',
                                              'hurt 2 1', 'feed 6 1 minecraft:wheat'])
    return s


def write_scenarios(path, chosen):
    lines = []
    for i, (name, ticks, opts, body) in chosen:
        ox, oz = 512 * (i + 1), 0
        lines.append(f'scenario {name} ticks={ticks} {opts}'.rstrip())
        lines.append(f'origin {ox} {oz}')
        for line in body:
            f = line.split()
            # `at TICK block ...` changes blocks before that tick; `watch` boxes stay relative.
            head, f = (f[:2], f[2:]) if f[0] == 'at' else ([], f)
            if f[0] == 'player':
                f[1] = repr(float(f[1]) + ox)
                f[3] = repr(float(f[3]) + oz)
            elif f[0] == 'mob':
                f[2] = repr(float(f[2]) + ox)
                f[4] = repr(float(f[4]) + oz)
            elif f[0] in ('block', 'heart', 'chest'):
                f[1] = str(int(f[1]) + ox)
                f[3] = str(int(f[3]) + oz)
            elif f[0] == 'fill':
                for k in (1, 4):
                    f[k] = str(int(f[k]) + ox)
                for k in (3, 6):
                    f[k] = str(int(f[k]) + oz)
            lines.append(' '.join(head + f))
        lines.append('end')
    path.write_text('\n'.join(lines) + '\n')


def parse(path):
    """{scenario: {tick: {mob: {field: value}}, sounds: {tick: [..]}}}"""
    result, cur = {}, None
    for line in path.read_text().splitlines():
        if line.startswith('== '):
            cur = result.setdefault(line[3:], {'ticks': {}, 'sounds': {}})
            continue
        head, *fields = line.split('\t')
        parts = head.split(' ', 2)
        t = int(parts[0])
        if parts[1] == 'sound':
            cur['sounds'].setdefault(t, []).append(parts[2])
            continue
        if parts[1] == 'block':
            cur.setdefault('blocks', {}).setdefault(t, []).append(parts[2])
            continue
        if parts[1] == 'trace':
            cur.setdefault('trace', {}).setdefault(t, []).append(parts[2])
            continue
        if parts[1] == 'heart':
            cur.setdefault('hearts', {}).setdefault(t, []).append(line.split(' ', 2)[2])
            continue
        if parts[1] == 'hive':
            cur.setdefault('hives', {}).setdefault(t, []).append(parts[2])
            continue
        if parts[1] == 'chest':
            cur.setdefault('chests', {}).setdefault(t, []).append(parts[2])
            continue
        mob = int(parts[1])
        row = {}
        if len(parts) > 2:
            row['removed'] = parts[2]
        for f in fields:
            k, _, v = f.partition(' ')
            row[k] = v
        cur['ticks'].setdefault(t, {})[mob] = row
    return result


def decode(value):
    """Hex float/double fields made readable."""
    def one(x):
        try:
            if len(x) > 8:
                return f'{struct.unpack(">d", int(x, 16).to_bytes(8, "big"))[0]!r}'
            return f'{struct.unpack(">f", int(x, 16).to_bytes(4, "big"))[0]:.9g}'
        except ValueError:
            return x
    out = []
    for word in value.split(' '):
        out.append(','.join(one(p) if all(c in '0123456789abcdef' for c in p) and len(p) >= 5 else p for p in word.split(',')))
    return ' '.join(out)


MULT, MASK = 0x5DEECE66D, (1 << 48) - 1


def draws(before, after, limit=20000):
    """How many LegacyRandomSource steps lead from one state to the next."""
    try:
        s, goal = int(before, 16), int(after, 16)
    except (TypeError, ValueError):
        return '?'
    for n in range(limit):
        if s == goal:
            return n
        s = (s * MULT + 11) & MASK
    return f'>{limit}'


def compare(vanilla, rust, fields=None):
    """Returns (report lines, totals)."""
    report, totals = [], {'mob_ticks': 0, 'matched': 0, 'scenarios': 0, 'clean': 0}
    for name, v in vanilla.items():
        r = rust.get(name)
        totals['scenarios'] += 1
        if r is None:
            report.append(f'{name}: missing from Horizon')
            continue
        mobs = sorted({m for t in v['ticks'].values() for m in t})
        clean = True
        for mob in mobs:
            ticks = sorted(v['ticks'])
            first = None
            for t in ticks:
                a = v['ticks'][t].get(mob)
                b = r['ticks'].get(t, {}).get(mob)
                totals['mob_ticks'] += 1
                if a == b or (b is not None and fields and all(a.get(k) == b.get(k) for k in fields)):
                    if first is None:
                        totals['matched'] += 1
                    continue
                if first is None:
                    first = t
                    diffs = []
                    for k in (a or {}):
                        if b is None or a.get(k) != b.get(k):
                            show = (lambda x: x) if k == 'rng' else decode
                            diffs.append(f'{k}: vanilla {show(a.get(k, "?"))} | horizon {show((b or {}).get(k, "?"))}')
                    for k in (b or {}):
                        if a is None or k not in a:
                            diffs.append(f'{k}: vanilla ? | horizon {decode(b[k])}')
                    prev_v = v['ticks'].get(t - 1, {}).get(mob, {}).get('rng')
                    prev_r = r['ticks'].get(t - 1, {}).get(mob, {}).get('rng')
                    report.append(f'{name} mob {mob}: first divergence at tick {t} of {len(ticks)} '
                                  f'(random draws this tick: vanilla {draws(prev_v, (a or {}).get("rng"))}, '
                                  f'horizon {draws(prev_r, (b or {}).get("rng"))})')
                    for line in [x for x in v.get('trace', {}).get(t, []) if x.split(' ', 1)[0] == str(mob)][:40]:
                        report.append('      vanilla ' + line)
                    # Present when the probe is built with the rng-trace feature (see --trace).
                    for line in [x for x in r.get('trace', {}).get(t, []) if x.split(' ', 1)[0] == str(mob)][:40]:
                        report.append('      horizon ' + line)
                    report.extend('    ' + d for d in diffs[:12])
            if first is not None:
                clean = False
        # Creaking hearts (state, block entity, level random): first differing tick.
        for t in sorted(set(v.get('hearts', {})) | set(r.get('hearts', {}))):
            if v.get('hearts', {}).get(t) != r.get('hearts', {}).get(t):
                report.append(f'{name}: hearts differ at tick {t}: vanilla {v.get("hearts", {}).get(t)} | horizon {r.get("hearts", {}).get(t)}')
                clean = False
                break
        # Sounds, watched block changes, chest contents and hives: first differing tick.
        for kind in ('sounds', 'blocks', 'chests', 'hives'):
            va, ra = v.get(kind, {}), r.get(kind, {})
            for t in sorted(set(va) | set(ra)):
                if va.get(t, []) != ra.get(t, []):
                    report.append(f'{name}: {kind} differ at tick {t}: vanilla {va.get(t, [])} | horizon {ra.get(t, [])}')
                    clean = False
                    break
        totals['clean'] += clean
        if clean:
            report.append(f'{name}: all {len(v["ticks"])} ticks match')
    return report, totals


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--only', nargs='*')
    parser.add_argument('--java-only', action='store_true')
    parser.add_argument('--rust-only', action='store_true', help='reuse the last vanilla output')
    parser.add_argument('--report-only', action='store_true', help='print divergences without failing')
    parser.add_argument('--trace', action='store_true',
                        help="also trace Horizon's mob random draws (rng-trace probe with line tables)")
    args = parser.parse_args()
    build = lab.LOCAL / 'mob-ai-parity'
    build.mkdir(parents=True, exist_ok=True)
    chosen = [(i, s) for i, s in enumerate(scenarios()) if not args.only or any(o in s[0] for o in args.only)]
    inputs = build / 'scenarios.txt'
    write_scenarios(inputs, chosen)
    lock, jars, _ = base.verify_artifacts()
    if not args.rust_only:
        classes, dependencies, _ = oracle_harness.compile_oracle(build, 'VanillaMobAiOracle', jars, baking.client_libraries(),
                                                                lock['minecraft']['java_major'])
        # The oracle's server world: cleared before each run, never left in /tmp.
        world = build / 'world'
        shutil.rmtree(world, ignore_errors=True)
        oracle_harness.run_logged(['java', '-Xmx3g', '-Djava.awt.headless=true', '-cp', str(classes) + os.pathsep + dependencies,
                                   'dev.horizonlab.parity.VanillaMobAiOracle', inputs, build / 'vanilla.txt', world],
                                  build, 'vanilla', timeout=3600)
    if not args.java_only:
        command = ['cargo', 'build', '--locked', '--release', '-p', 'horizon-client', '--example', 'client_probe']
        target, env = lab.cargo_target(), None
        if args.trace:
            target = lab.LOCAL / 'mob-ai-trace-target'
            command += ['--features', 'rng-trace']
            env = dict(os.environ, CARGO_TARGET_DIR=str(target), CARGO_PROFILE_RELEASE_DEBUG='line-tables-only',
                       CARGO_PROFILE_RELEASE_LTO='false', CARGO_PROFILE_RELEASE_CODEGEN_UNITS='16')
        oracle_harness.run_logged(command, build, 'compile-rust', env=env)
        binary = target / 'release/examples/client_probe'
        oracle_harness.run_logged([binary, 'mob_ai_probe', lab.RESOURCES, inputs, build / 'horizon.txt'], build, 'horizon')
    if args.java_only:
        return
    names = {s[0] for _, s in chosen}
    vanilla = {k: v for k, v in parse(build / 'vanilla.txt').items() if k in names}
    report, totals = compare(vanilla, parse(build / 'horizon.txt'))
    (build / 'report.txt').write_text('\n'.join(report) + '\n')
    print('\n'.join(report))
    print(f"{totals['clean']}/{totals['scenarios']} scenarios match entirely; "
          f"{totals['matched']}/{totals['mob_ticks']} mob-ticks match before each mob's first divergence")
    if not args.report_only and totals['clean'] != totals['scenarios']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
