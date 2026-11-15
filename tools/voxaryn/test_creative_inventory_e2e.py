#!/usr/bin/env python3
"""Exercise creative mouse interactions through the real window and server.

Run with --bin-dir, --resources and --output (outside the public tree). Each
mode starts in fresh isolated homes; no existing player data is opened.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


class Game:
    def __init__(self, binaries, resources, output, in_frame):
        self.output = output
        self.root = Path(tempfile.mkdtemp(prefix="vci-"))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("HORIZON_")}
        for key in ("HORIZON_HOME", "HORIZON_LAUNCHER_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            home = self.root / key.lower()
            home.mkdir()
            self.env[key] = str(home)
        self.env.update(HORIZON_NO_AUDIO="1", HORIZON_LAUNCHER_NO_SELF_UPDATE="1", HORIZON_TELEMETRY_URL="off")
        self.socket = self.root / "c.sock"
        suffix = ".exe" if os.name == "nt" else ""
        self.control = binaries / ("horizon-ctl" + suffix)
        command = [str(binaries / ("horizon-client" + suffix)), "--resources", str(resources),
                   "--world", "CreativeRegression", "--seed", "1", "--creative",
                   "--render-distance", "2", "--offscreen", "--control-socket", str(self.socket)]
        if in_frame:
            command.append("--in-frame")
        self.log = (output / "game.log").open("wb")
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
        self.process = subprocess.Popen(command, cwd=self.root, env=self.env, stdout=self.log,
                                        stderr=self.log, startupinfo=startup)
        self.calls = []

    def ctl(self, *args, timeout=15):
        reply = subprocess.run([str(self.control), "--socket", str(self.socket), "--timeout", str(timeout),
                                *map(str, args)], cwd=self.root, env=self.env, capture_output=True,
                               text=True, encoding="utf8", timeout=timeout + 5)
        result = json.loads(reply.stdout.strip().splitlines()[-1])
        self.calls.append({"args": args, "exit": reply.returncode, "result": result})
        (self.output / "calls.json").write_text(json.dumps(self.calls, indent=2), encoding="utf8")
        if reply.returncode or "error" in result:
            raise RuntimeError((args, result, reply.stderr))
        return result

    def ready(self):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"game exited: {self.process.returncode}")
            if self.socket.exists():
                try:
                    state = self.ctl("state", timeout=2)
                    if state.get("ticks", 0) > 5 and state.get("gamemode") == "creative":
                        self.ctl("wait-ready", 45, timeout=50)
                        return
                except (RuntimeError, subprocess.TimeoutExpired):
                    pass
            time.sleep(0.5)
        raise RuntimeError("world did not become ready")

    def stop(self):
        try:
            if self.process.poll() is None:
                self.ctl("quit", timeout=8)
                self.process.wait(timeout=15)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.log.close()


def slot_count(inventory, slot):
    return next((s.get("count", 0) for s in inventory["hotbar"] + inventory["main"] if s["slot"] == slot), 0)


def exercise(game):
    def click(slot, *flags):
        if "shift" in flags:
            game.ctl("key", "shift", "down")
        game.ctl("click-at", *center(slot), *(["right"] if "right" in flags else []))
        if "shift" in flags:
            game.ctl("key", "shift", "up")
        # Separate intentional clicks from the menu's double-click gesture.
        time.sleep(0.3)

    def inventory():
        game.ctl("wait", 5)
        return game.ctl("inventory")

    def total(expected):
        inv = inventory()
        count = sum(s.get("count", 0) for s in inv["hotbar"] + inv["main"]
                    if s.get("item") == "minecraft:dirt") + (inv["carried"] or {}).get("count", 0)
        assert count == expected, inv
        return inv

    game.ctl("command", "clear @p")
    game.ctl("command", "item replace entity @p hotbar.0 with minecraft:dirt 12")
    game.ctl("key", "e", "tap")
    ui = game.ctl("ui")
    bounds = next(w["bounds"] for w in ui["widgets"] if w["id"] == "slot-900")
    game.ctl("click-at", bounds[0] - 9 + 182, bounds[1] - 18 + 145)
    ui = game.ctl("ui")
    def center(slot):
        b = next(w["bounds"] for w in ui["widgets"] if w["id"] == f"slot-{slot}")
        return b[0] + 8, b[1] + 8

    click(0)
    for close_key in ("e", "esc"):
        before = total(12)["carried"]
        game.ctl("key", close_key, "tap")
        game.ctl("key", "e", "tap")
        assert inventory()["carried"] == before
    click(9)  # Empty slot, then an occupied slot, then right-click splitting.
    click(9)
    click(10, "right")
    inv = total(12)
    assert slot_count(inv, 10) == 1 and inv["carried"]["count"] == 11, inv
    click(10, "right")
    inv = total(12)
    assert slot_count(inv, 10) == 2 and inv["carried"]["count"] == 10, inv
    click(10)
    click(10, "right")
    inv = total(12)
    assert slot_count(inv, 10) == 6 and inv["carried"]["count"] == 6, inv
    click(10)
    click(10)
    game.ctl("drag-at", *center(9), *center(10), *center(11))
    inv = total(12)
    assert [slot_count(inv, s) for s in (9, 10, 11)] == [4, 4, 4] and not inv["carried"], inv
    # Vanilla's second press on the now-empty source clears lastQuickMoved;
    # it must neither collect the other stacks nor duplicate the first one.
    game.ctl("key", "shift", "down")
    game.ctl("click-at", *center(9))
    game.ctl("click-at", *center(9))
    game.ctl("key", "shift", "up")
    inv = total(12)
    assert [slot_count(inv, s) for s in (0, 9, 10, 11)] == [4, 0, 4, 4] and not inv["carried"], inv
    click(10, "shift")
    click(11, "shift")
    assert slot_count(total(12), 0) == 12
    click(0)
    game.ctl("drag-at", "right", *center(9), *center(10), *center(11))
    inv = total(12)
    assert [slot_count(inv, s) for s in (9, 10, 11)] == [1, 1, 1] and inv["carried"]["count"] == 9, inv
    click(0)
    for slot in (9, 10, 11):
        click(slot, "shift")
    assert slot_count(total(12), 0) == 12
    click(0)
    game.ctl("drag-at", *center(9), *center(10), *center(11))
    inv = total(12)
    assert [slot_count(inv, s) for s in (9, 10, 11)] == [4, 4, 4] and not inv["carried"], inv
    click(9, "shift")
    click(10, "shift")
    click(11, "shift")
    assert slot_count(total(12), 0) == 12
    click(0)
    game.ctl("key", "shift", "down")
    game.ctl("drag-at", *center(9), *center(10), *center(11), *center(9))
    game.ctl("key", "shift", "up")
    inv = total(12)
    assert [slot_count(inv, s) for s in (9, 10, 11)] == [4, 4, 4] and not inv["carried"], inv
    game.ctl("key", "esc", "tap")
    server = game.ctl("command", "data get entity @p Inventory")
    text = "\n".join(server["chat"])
    stacks = re.findall(r"\{[^{}]*minecraft:dirt[^{}]*\}", text)
    assert len(stacks) == 3 and all(re.search(r"count: 4\b", s) for s in stacks), server
    assert sorted(int(re.search(r"Slot: (\d+)b", s)[1]) for s in stacks) == [9, 10, 11], server
    game.ctl("save")
    return {"cursor_e_escape": True, "left_right_empty_occupied": True, "ordinary_drag": True,
            "shift_click_drag": True, "shift_double_click": True, "right_drag": True,
            "total_dirt": 12, "server_inventory": server, "scratch": str(game.root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bin-dir", "resources", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    # Windows cannot relink a running executable; run copies so Cargo remains usable.
    binaries = args.output / "bin"
    binaries.mkdir()
    for name in ("horizon-client", "horizon-ctl"):
        name += ".exe" if os.name == "nt" else ""
        shutil.copy2(args.bin_dir / name, binaries / name)
    results = {}
    for mode in ("integrated", "in-frame"):
        output = args.output / mode
        output.mkdir()
        game = Game(binaries.resolve(), args.resources.resolve(), output, mode == "in-frame")
        try:
            game.ready()
            results[mode] = exercise(game)
            print(mode, "PASS", flush=True)
        finally:
            game.stop()
            (args.output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
