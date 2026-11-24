"""Replay every demonstration through RabbitMQ and check its observations."""

import argparse
import asyncio
from pathlib import Path

from agents import CROSSINGS
from simulation import SCENARIOS, Simulation
from traffic import STOPS, World


def verify_signals(rows):
    for cross in CROSSINGS:
        stage, axis, since = "green", "NS", 0
        last_off = {"NS": 0, "EW": 0}
        lights = [row for row in rows if row["sender"] == f"light-{cross}" and row["event"] == "signal"]
        for row in lights:
            time, data = row["time"], row["data"]
            age = time - since
            if stage == "green":
                assert data["stage"] == "yellow" and data["axis"] == axis, row
                minimum = 5 if data["reason"] in ("emergency", "prepare_corridor") else 15
                assert minimum <= age <= 60, row
                last_off[axis] = time
            elif stage == "yellow":
                assert data["stage"] == "all_red" and data["axis"] == axis and age == 3, row
            else:
                assert data["stage"] == "green" and data["axis"] != axis and age == 2, row
                assert time - last_off[data["axis"]] <= 120, row
            stage, axis, since = data["stage"], data["axis"], time


def verify_routes():
    for first, second, side, axis in (("P1", "P2", "W", "EW"), ("P1", "P3", "N", "NS")):
        world = World("UC-01")
        lane = next(lane for lane in world.lanes
                    if lane.crossings == (first, second) and lane.side == side)
        lane.vehicles.clear()
        car = world.vehicle(STOPS[0])
        lane.vehicles.append(car)
        outputs = {cross: {"stage": "all_red", "axis": axis} for cross in CROSSINGS}
        world.tick(1, outputs)
        assert car.position == STOPS[0]
        outputs[first]["stage"] = "green"
        world.tick(2, outputs)
        assert car.position > STOPS[0]
        assert any(item["id"] == car.id and item["next"] == second for item in world.vehicles())
        for time in range(3, 59):
            world.tick(time, outputs)
        assert car.position == STOPS[1]
        assert world.sample(second)[side]["queue"] >= 1
        assert world.sample(second)[side]["flow"] > 0
        outputs[second]["stage"] = "yellow"
        world.tick(59, outputs)
        assert car.position == STOPS[1]
        outputs[second]["stage"] = "green"
        world.tick(60, outputs)
        assert car.position > STOPS[1]
        assert any(item["id"] == car.id and item["next"] is None for item in world.vehicles())


def check(run):
    rows = run.journal.events

    def find(event, sender=None, receiver=None, **fields):
        return [row for row in rows if row["event"] == event
                and (sender is None or row["sender"] == sender)
                and (receiver is None or row["receiver"] == receiver)
                and all(row["data"].get(key) == value for key, value in fields.items())]

    def require(*args, **kwargs):
        matches = find(*args, **kwargs)
        assert matches, (args, kwargs)
        return matches

    assert run.status == "finished", run.error
    assert len({row["sender"] for row in rows if row["sender"].startswith("sensor-")}) == 4
    verify_signals(rows)
    match run.scenario:
        case "UC-01":
            require("signal", "light-P1", reason="empty_current_queue", green=15)
            require("green_extended", "light-P1")
            require("intersection_status", "light-P1")
        case "UC-02":
            first = require("plan_active", "light-P1", offset=0)[0]
            second = require("plan_active", "light-P2", offset=22)[0]
            assert first["data"]["plan"] == second["data"]["plan"]
            for start in (first, second):
                assert any(row["time"] == start["time"] for row in
                           require("signal", start["sender"], axis="EW", stage="green"))
            assert second["time"] - first["time"] == 22
            repeated = {cross: require("signal", f"light-{cross}", axis="EW", stage="green")
                        for cross in ("P1", "P2")}
            assert any(east["time"] - west["time"] == 22 and west["time"] > first["time"]
                       for west in repeated["P1"] for east in repeated["P2"])
        case "UC-03":
            detected = require("congestion_detected", crossing="P2", side="W")[0]
            assert detected["data"]["fill"] > .8 and detected["data"]["consecutive"] >= 2
            require("cancel_plan", reason="congestion_priority")
            first = require("plan_active", "light-P1", lengths={"NS": 45, "EW": 15})[0]
            second = require("plan_active", "light-P2", lengths={"NS": 15, "EW": 45})[0]
            assert first["data"]["plan"] == second["data"]["plan"]
            cancelled = require("cancel_plan", receiver="light-P2", reason="fill_below_50_percent")[0]
            assert cancelled["conversation"] == second["data"]["plan"]
            assert max(first["time"], second["time"]) < cancelled["time"]
        case "UC-04":
            failed = require("sensor_failure", "sensor-P3", reason="three_invalid")[0]
            fixed = require("mode_changed", "light-P3", mode="FIXED")[0]
            reserved = require("plan_active", "light-P3")[0]
            restored = require("sensor_restored", "sensor-P3")[0]
            adaptive = require("mode_changed", "light-P3", mode="ADAPTIVE")[-1]
            assert failed["time"] == fixed["time"] < reserved["time"] < restored["time"] == adaptive["time"]
        case "UC-05":
            for cross, arrival in (("P1", 36), ("P2", 72)):
                agreement = require("corridor_reply", f"light-{cross}", decision="AGREE")[0]
                green = require("corridor_ready", f"light-{cross}", axis="EW")[0]
                passed = require("vehicle_passed", crossing=cross)[0]
                cancelled = require("corridor_cancel", receiver=f"light-{cross}")[0]
                assert agreement["time"] <= green["time"] <= passed["time"] == cancelled["time"] == arrival
            require("emergency_finished")
            assert not find("dispatch_warning")


async def main(arguments):
    verify_routes()
    for scenario in SCENARIOS:
        run = Simulation(scenario, speed=0)
        await run.run()
        if arguments.logs:
            arguments.logs.mkdir(parents=True, exist_ok=True)
            (arguments.logs / f"{scenario}.log").write_text(run.journal.text(), encoding="utf-8")
        check(run)
        print(f"{scenario} OK — {SCENARIOS[scenario][0]} ({len(run.journal.events)} записей)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, help="Save the five complete journals")
    asyncio.run(main(parser.parse_args()))
