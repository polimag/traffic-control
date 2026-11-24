"""The four TrafficControl agent roles."""

from functools import lru_cache
from math import ceil

CROSSINGS = ("P1", "P2", "P3", "P4")
APPROACHES = {"NS": ("N", "S"), "EW": ("W", "E")}
OPPOSITE = {"NS": "EW", "EW": "NS"}
NEIGHBORS = {
    "P1": {"E": "P2", "S": "P3"}, "P2": {"W": "P1", "S": "P4"},
    "P3": {"N": "P1", "E": "P4"}, "P4": {"N": "P2", "W": "P3"},
}
MIN_GREEN, MAX_GREEN, EMERGENCY_MIN = 15, 60, 5
YELLOW, ALL_RED, MAX_WAIT = 3, 2, 120
CHANGE = YELLOW + ALL_RED


class Agent:
    def __init__(self, name, bus):
        self.name = name
        self.bus = bus

    async def send(self, time, receiver, event, conversation=None, **data):
        await self.bus.send(time, self.name, receiver, event, conversation, **data)

    def log(self, time, event, **data):
        self.bus.journal.add(time, self.name, event, **data)


class Sensor(Agent):
    def __init__(self, crossing, bus):
        super().__init__(f"sensor-{crossing}", bus)
        self.light = f"light-{crossing}"
        self.failed = False
        self.bad = self.good = 0

    async def receive(self, message):
        pass

    async def measure(self, time, reading):
        if reading is None or any(
            item[field] < 0 for item in reading.values() for field in ("queue", "flow")
        ):
            self.good, self.bad = 0, self.bad + 1
            self.log(time, "invalid_measurement", consecutive=self.bad)
            if self.bad == 3 and not self.failed:
                self.failed = True
                await self.send(time, self.light, "sensor_failure", reason="three_invalid")
            return
        self.bad, self.good = 0, self.good + 1
        if self.failed:
            self.log(time, "valid_measurement", consecutive=self.good)
            if self.good < 3:
                return
            self.failed = False
            await self.send(time, self.light, "sensor_restored")
        await self.send(time, self.light, "traffic_data", approaches=reading)


def alignment(axis, since, earliest, target, lengths):
    """Find safe yellow-start times leading to an exact east–west green."""
    @lru_cache(maxsize=None)
    def search(current, started):
        if started == target:
            return () if current == "EW" else None
        if started + MIN_GREEN + CHANGE > target:
            return None
        options = sorted(range(MIN_GREEN, MAX_GREEN + 1),
                         key=lambda duration: abs(duration - lengths[current]))
        for duration in options:
            yellow_at = started + duration
            if yellow_at < earliest or yellow_at + CHANGE > target:
                continue
            tail = search(OPPOSITE[current], yellow_at + CHANGE)
            if tail is not None:
                return ((started, yellow_at), *tail)
        return None

    schedule = search(axis, since)
    return dict(schedule) if schedule is not None else None


class Light(Agent):
    def __init__(self, crossing, bus):
        super().__init__(f"light-{crossing}", bus)
        self.crossing = crossing
        self.axis, self.stage, self.since, self.deadline = "NS", "green", 0, 0
        self.mode = "ADAPTIVE"
        self.last_data, self.reading, self.failed = 0, {}, False
        self.good_samples = 0
        self.last_off = {"NS": 0, "EW": 0}
        self.plan = self.pending = self.offer = None
        self.plan_replies = {}
        self.schedule = {}
        self.corridor = None
        self.waiting = {}
        self.emergency_replies = {}
        self.goal = 30

    async def set_mode(self, time, mode, reason):
        if self.mode != mode:
            self.mode = mode
            await self.send(time, "coordinator", "mode_changed", mode=mode, reason=reason)

    def base_mode(self):
        return self.plan["mode"] if self.plan else ("FIXED" if self.failed else "ADAPTIVE")

    async def sensor_state(self, time, failed, reason):
        if self.failed == failed:
            return
        self.failed, self.good_samples = failed, 0
        self.offer = self.pending = self.plan = None
        self.schedule = {}
        if not self.corridor:
            await self.set_mode(time, self.base_mode(), reason)

    def wave_schedule(self, time, plan):
        axis, since = self.axis, self.since
        if self.stage != "green":
            axis = OPPOSITE[axis]
            since = self.deadline + (ALL_RED if self.stage == "yellow" else 0)
        return alignment(axis, since, time, plan["start"], plan["lengths"])

    async def receive(self, message):
        time, event, data = message["time"], message["event"], message["data"]
        key = message["conversation"]
        if event == "traffic_data":
            if self.failed:
                self.good_samples = self.good_samples + 1 if time - self.last_data == 5 else 1
                if self.good_samples == 3:
                    await self.sensor_state(time, False, "measurements_resumed")
            self.last_data, self.reading = time, data["approaches"]
        elif event == "sensor_failure":
            await self.sensor_state(time, True, data["reason"])
        elif event == "sensor_restored":
            await self.sensor_state(time, False, "sensor_restored")
        elif event == "apply_plan":
            await self.offer_plan(time, key, data)
        elif event == "commit_plan" and self.offer and self.offer["id"] == key:
            self.pending, self.offer = self.offer, None
            if self.pending["mode"] == "COORDINATED":
                schedule = self.wave_schedule(time, self.pending)
                if schedule is None:
                    self.pending = None
                    await self.send(time, "coordinator", "plan_reply", key,
                                    decision="REFUSE", reason="offset_unreachable")
                    return
                self.schedule = schedule
        elif event == "cancel_plan":
            await self.cancel_plan(time, key, "plan_cancelled")
        elif event in ("corridor_request", "corridor_update"):
            await self.request_corridor(time, key, data, update=event == "corridor_update")
        elif event == "corridor_cancel":
            self.waiting.pop(key, None)
            if self.corridor and self.corridor["id"] == key:
                await self.end_corridor(time, "passage_completed")

    async def offer_plan(self, time, key, data):
        if key in self.plan_replies:
            await self.send(time, "coordinator", "plan_reply", key, **self.plan_replies[key])
            return
        mode, lengths = data["mode"], data["lengths"]
        valid = (
            set(lengths) == set(APPROACHES)
            and all(MIN_GREEN <= value <= MAX_GREEN for value in lengths.values())
            and data["cycle"] == sum(lengths.values()) + 2 * CHANGE
            and data["expires"] > max(time, data["start"])
        )
        reason = None
        if self.corridor:
            reason = "emergency_active"
        elif mode not in ("FIXED", "CONGESTION", "COORDINATED") or not valid:
            reason = "invalid_timing"
        elif self.failed != (mode == "FIXED"):
            reason = "sensor_mode"
        elif self.plan and self.plan["mode"] == "CONGESTION" and mode == "COORDINATED":
            reason = "congestion_active"
        elif mode == "COORDINATED" and self.wave_schedule(time, data) is None:
            reason = "offset_unreachable"
        response = {"decision": "REFUSE", "reason": reason} if reason else {"decision": "AGREE"}
        if not reason:
            self.offer = {**data, "id": key}
        self.plan_replies[key] = response
        await self.send(time, "coordinator", "plan_reply", key, **response)

    async def cancel_plan(self, time, key, reason):
        if self.offer and self.offer["id"] == key:
            self.offer = None
        if self.pending and self.pending["id"] == key:
            self.pending = None
            self.schedule = {}
        if self.plan and self.plan["id"] == key:
            self.plan = None
            if not self.corridor:
                await self.set_mode(time, self.base_mode(), reason)

    def ready_at(self, time, axis, arrival):
        if self.stage == "yellow":
            next_green = self.deadline + ALL_RED
        elif self.stage == "all_red":
            next_green = self.deadline
        elif self.axis != axis:
            return max(time, self.since + EMERGENCY_MIN) + CHANGE
        elif arrival < self.since + MAX_GREEN:
            return time
        else:
            return min(self.since + MAX_GREEN, max(time, self.since + EMERGENCY_MIN)) + 2 * CHANGE + EMERGENCY_MIN
        return next_green if OPPOSITE[self.axis] == axis else next_green + EMERGENCY_MIN + CHANGE

    async def request_corridor(self, time, key, data, update=False):
        arrival, axis = data["arrival"], data["axis"]
        if not update and key in self.emergency_replies:
            await self.send(time, "emergency", "corridor_reply", key, **self.emergency_replies[key])
            return
        candidate = {"id": key, "axis": axis, "arrival": arrival, "ready": False}
        current = self.corridor
        if axis not in APPROACHES:
            response = {"decision": "REFUSE", "reason": "invalid_axis"}
        elif current and current["id"] != key and (current["ready"] or current["arrival"] <= arrival):
            self.waiting[key] = candidate
            response = {"decision": "REFUSE", "reason": "corridor_busy",
                        "available_at": max(time + 1, current["arrival"] + 1)}
        else:
            ready_at = self.ready_at(time, axis, arrival)
            if ready_at > arrival:
                response = {"decision": "REFUSE", "reason": "arrival_too_soon",
                            "available_at": ready_at}
                if current and current["id"] == key:
                    await self.end_corridor(time, "arrival_too_soon")
            else:
                if current and current["id"] != key:
                    self.waiting[current["id"]] = current
                    replaced = {"decision": "REFUSE", "reason": "earlier_vehicle",
                                "available_at": arrival + 1}
                    self.emergency_replies[current["id"]] = replaced
                    await self.send(time, "emergency", "corridor_reply", current["id"], **replaced)
                if current and current["id"] == key:
                    candidate["ready"] = current["ready"]
                self.corridor = candidate
                self.schedule = {}
                await self.set_mode(time, "EMERGENCY", "corridor_request")
                currently_green = self.stage == "green" and self.axis == axis and ready_at == time
                predicted = time if currently_green else max(ready_at, arrival - 1)
                response = {"decision": "AGREE", "ready_at": predicted}
        self.emergency_replies[key] = response
        await self.send(time, "emergency", "corridor_reply", key, **response)

    async def end_corridor(self, time, reason):
        self.corridor = None
        if self.waiting:
            candidate = min(self.waiting.values(), key=lambda item: item["arrival"])
            self.waiting.pop(candidate["id"])
            if candidate["arrival"] >= time:
                await self.request_corridor(time, candidate["id"], candidate, update=True)
                if self.corridor:
                    return
        if self.plan and self.plan["expires"] <= time:
            self.plan = None
        wave = self.pending if self.pending and self.pending["mode"] == "COORDINATED" else self.plan
        if wave and wave["mode"] == "COORDINATED":
            # Rejoin the same periodic anchor at a feasible future green.
            start = wave["start"]
            while start <= time:
                start += wave["cycle"]
            self.plan = None
            self.pending = None
            while start < wave["expires"]:
                candidate = {**wave, "start": start}
                schedule = self.wave_schedule(time, candidate)
                if schedule is not None:
                    self.pending, self.schedule = candidate, schedule
                    break
                start += wave["cycle"]
        await self.set_mode(time, self.base_mode(), reason)

    async def tick(self, time):
        if not self.failed and time - self.last_data >= 15:
            await self.sensor_state(time, True, "sensor_timeout")
        for plan in (self.offer, self.pending, self.plan):
            if plan and time >= plan["expires"]:
                await self.cancel_plan(time, plan["id"], "plan_expired")

        if self.stage == "yellow" and time >= self.deadline:
            self.stage, self.deadline = "all_red", time + ALL_RED
            self.log(time, "signal", axis=self.axis, stage=self.stage)
        elif self.stage == "all_red" and time >= self.deadline:
            await self.start_green(time)
        elif self.stage == "green":
            if self.corridor and self.corridor["axis"] == self.axis:
                if time - self.since >= MAX_GREEN:
                    await self.send(time, "emergency", "corridor_unavailable",
                                    self.corridor["id"], reason="green_limit")
                    await self.end_corridor(time, "green_limit")
                    self.start_yellow(time, "green_limit")
                elif self.corridor["arrival"] < self.since + MAX_GREEN:
                    if not self.corridor["ready"]:
                        self.corridor["ready"] = True
                        await self.send(time, "emergency", "corridor_ready",
                                        self.corridor["id"], axis=self.axis)
                elif time >= self.since + EMERGENCY_MIN:
                    self.start_yellow(time, "prepare_corridor")
            else:
                reason = self.switch_reason(time)
                if reason:
                    self.start_yellow(time, reason)

    async def start_green(self, time):
        self.axis = OPPOSITE[self.axis]
        self.stage, self.since, self.goal = "green", time, 30
        if self.pending and not self.corridor:
            mode = self.pending["mode"]
            activate = (mode == "COORDINATED" and time == self.pending["start"] and self.axis == "EW")
            activate |= mode != "COORDINATED" and self.axis == "NS" and time >= self.pending["start"]
            if activate:
                self.plan, self.pending = self.pending, None
                self.schedule = {}
                await self.set_mode(time, self.plan["mode"], "plan_started")
                self.log(time, "plan_active", plan=self.plan["id"], offset=self.plan["offset"],
                         lengths=self.plan["lengths"])
        self.log(time, "signal", axis=self.axis, stage=self.stage)
        if self.corridor and self.corridor["axis"] == self.axis:
            self.corridor["ready"] = True
            await self.send(time, "emergency", "corridor_ready", self.corridor["id"], axis=self.axis)
        if self.axis == "NS":
            await self.send(time, "coordinator", "intersection_status",
                            mode=self.mode, approaches=self.reading)

    def start_yellow(self, time, reason):
        self.stage, self.deadline = "yellow", time + YELLOW
        self.last_off[self.axis] = time
        self.log(time, "signal", axis=self.axis, stage=self.stage,
                 green=time - self.since, reason=reason)

    def switch_reason(self, time):
        age = time - self.since
        if age >= MAX_GREEN:
            return "maximum_green"
        if self.corridor:
            if age >= EMERGENCY_MIN and time >= self.corridor["arrival"] - CHANGE - 1:
                return "emergency"
            return None
        if age < MIN_GREEN:
            return None
        if time >= self.last_off[OPPOSITE[self.axis]] + MAX_WAIT - CHANGE:
            return "wait_limit"
        if self.since in self.schedule:
            return "wave_alignment" if time >= self.schedule[self.since] else None
        if self.plan:
            return "active_plan" if age >= self.plan["lengths"][self.axis] else None
        if self.failed:
            return "fixed_default" if age >= 30 else None
        if age % 5:
            return None
        queue = lambda axis: sum(self.reading.get(side, {}).get("queue", 0)
                                 for side in APPROACHES[axis])
        current, cross = queue(self.axis), queue(OPPOSITE[self.axis])
        if current == 0 and cross > 0:
            return "empty_current_queue"
        if age >= self.goal:
            if current > 3 and age + 5 <= MAX_GREEN:
                self.goal = age + 5
                self.log(time, "green_extended", axis=self.axis, queue=current, until=self.goal)
            else:
                return "base_green_completed"
        return None

    def snapshot(self):
        return {"axis": self.axis, "stage": self.stage, "mode": self.mode,
                "since": self.since, "sensor": "FAILED" if self.failed else "OK"}


class Coordinator(Agent):
    def __init__(self, bus):
        super().__init__("coordinator", bus)
        self.latest, self.history, self.high = {}, {}, {}
        self.routes, self.plans, self.fixed = {}, {}, set()
        self.sequence = 0
        self.wave_after = 0

    async def receive(self, message):
        time, event, data = message["time"], message["event"], message["data"]
        key, crossing = message["conversation"], message["sender"].removeprefix("light-")
        if event == "intersection_status":
            self.latest[crossing] = (time, data["approaches"])
            if data["mode"] != "FIXED":
                for side, reading in data["approaches"].items():
                    history = self.history.setdefault((crossing, side), [])
                    history.append(reading["flow"])
                    del history[:-6]
                    pair = (crossing, side)
                    self.high[pair] = self.high.get(pair, 0) + 1 if reading["fill"] > .80 else 0
        elif event == "mode_changed":
            if data["mode"] == "FIXED":
                self.fixed.add(crossing)
            elif data["mode"] == "ADAPTIVE":
                self.fixed.discard(crossing)
                for plan_id, plan in list(self.plans.items()):
                    if plan["mode"] == "FIXED" and crossing in plan["payloads"]:
                        await self.cancel(time, plan_id, "sensor_restored")
        elif event == "emergency_started":
            self.routes[key] = set(data["route"])
            for plan in self.plans.values():
                if not plan["committed"] and self.routes[key].intersection(plan["payloads"]):
                    plan["blocked"] = True
        elif event == "emergency_finished":
            self.routes.pop(key, None)
        elif event == "plan_reply" and key in self.plans:
            self.plans[key]["answers"][crossing] = data["decision"]

    async def cancel(self, time, key, reason):
        plan = self.plans.pop(key)
        for crossing in plan["payloads"]:
            await self.send(time, f"light-{crossing}", "cancel_plan", key, reason=reason)
        if plan["mode"] == "COORDINATED":
            self.wave_after = time + 30

    async def propose(self, time, mode, allocations, *, focus=None):
        self.sequence += 1
        key = f"plan-{self.sequence:02d}"
        duration = 240 if mode != "COORDINATED" else 300
        payloads = {}
        for crossing, (ns, ew, offset) in allocations.items():
            payloads[crossing] = {
                "mode": mode, "lengths": {"NS": ns, "EW": ew}, "offset": offset,
                "cycle": ns + ew + 2 * CHANGE, "expires": time + duration,
                "start": time + (80 + offset if mode == "COORDINATED" else 1),
            }
        self.plans[key] = {"mode": mode, "payloads": payloads, "answers": {},
                           "sent": time, "retried": False, "committed": False, "focus": focus}
        for crossing, payload in payloads.items():
            await self.send(time, f"light-{crossing}", "apply_plan", key, **payload)

    async def advance_protocols(self, time):
        for key, plan in list(self.plans.items()):
            payloads, answers = plan["payloads"], plan["answers"]
            missing = payloads.keys() - answers.keys()
            if time >= min(data["expires"] for data in payloads.values()):
                await self.cancel(time, key, "plan_expired")
            elif plan.get("blocked") or "REFUSE" in answers.values():
                await self.cancel(time, key, "plan_refused")
            elif not missing and not plan["committed"]:
                plan["committed"] = True
                for crossing in payloads:
                    await self.send(time, f"light-{crossing}", "commit_plan", key)
            elif missing and time - plan["sent"] >= 3:
                if plan["retried"]:
                    await self.cancel(time, key, "plan_reply_timeout")
                else:
                    plan["sent"], plan["retried"] = time, True
                    for crossing in sorted(missing):
                        await self.send(time, f"light-{crossing}", "apply_plan", key, **payloads[crossing])

    async def tick(self, time):
        await self.advance_protocols(time)
        blocked = set().union(*self.routes.values())
        for crossing in sorted(self.fixed - blocked):
            if not any(crossing in plan["payloads"] for plan in self.plans.values()):
                totals = {}
                for axis, sides in APPROACHES.items():
                    totals[axis] = sum(
                        sum(values) / len(values) if values else 300
                        for values in (self.history.get((crossing, side), []) for side in sides)
                    )
                share = round(60 * totals["NS"] / max(1, sum(totals.values())) / 5) * 5
                ns = max(15, min(45, share))
                await self.propose(time, "FIXED", {crossing: (ns, 60 - ns, 0)})
        if not time or time % 30:
            return
        available = {
            crossing for crossing, (stamp, _) in self.latest.items()
            if time - stamp <= 90 and crossing not in blocked | self.fixed
        }
        for key, plan in list(self.plans.items()):
            if plan["mode"] in ("COORDINATED", "CONGESTION"):
                if not set(plan["payloads"]) <= available | blocked:
                    await self.cancel(time, key, "intersection_unavailable")
                elif plan["mode"] == "CONGESTION" and self.fill(*plan["focus"]) < .50:
                    await self.cancel(time, key, "fill_below_50_percent")
        for (downstream, side), count in sorted(self.high.items()):
            upstream = NEIGHBORS[downstream].get(side)
            pair = {upstream, downstream}
            if count < 2 or not pair <= available:
                continue
            overlapping = [(key, plan) for key, plan in self.plans.items()
                           if pair.intersection(plan["payloads"])]
            if any(plan["mode"] == "CONGESTION" for _, plan in overlapping):
                continue
            for key, _ in overlapping:
                await self.cancel(time, key, "congestion_priority")
            self.log(time, "congestion_detected", crossing=downstream, side=side,
                     fill=self.fill(downstream, side), consecutive=count)
            horizontal = side in ("W", "E")
            await self.propose(time, "CONGESTION",
                               {upstream: (45, 15, 0) if horizontal else (15, 45, 0),
                                downstream: (15, 45, 0) if horizontal else (45, 15, 0)},
                               focus=(downstream, side))
        if time < self.wave_after:
            return
        occupied = set().union(*(plan["payloads"].keys() for plan in self.plans.values()))
        for west, east in (("P1", "P2"), ("P3", "P4")):
            if not {west, east} <= available - occupied:
                continue
            forward, reverse = self.flow(west, "W"), self.flow(east, "E")
            if reverse > forward:
                west, east, forward, reverse = east, west, reverse, forward
            if forward > 600 and forward >= 1.5 * reverse:
                self.log(time, "wave_detected", first=west, second=east, forward=forward, reverse=reverse)
                await self.propose(time, "COORDINATED", {west: (30, 30, 0), east: (30, 30, 22)})

    def fill(self, crossing, side):
        return self.latest[crossing][1].get(side, {}).get("fill", 0)

    def flow(self, crossing, side):
        return self.latest[crossing][1].get(side, {}).get("flow", 0)


class Emergency(Agent):
    def __init__(self, bus):
        super().__init__("emergency", bus)
        self.calls = {}

    async def start(self, time, key, route, distance, speed):
        stops = []
        for index, (crossing, marker) in enumerate(route):
            neighbor = route[index - 1 if index else 1][0]
            side = next(side for side, adjacent in NEIGHBORS[crossing].items()
                        if adjacent == neighbor)
            axis = "NS" if side in "NS" else "EW"
            stops.append({"crossing": crossing, "axis": axis, "marker": marker})
        self.calls[key] = {"stops": stops}
        self.update_position(time, key, distance, speed)
        await self.send(time, "coordinator", "emergency_started", key,
                        route=[stop["crossing"] for stop in stops])

    def update_position(self, time, key, distance, speed):
        call = self.calls.get(key)
        if not call or speed <= 0:
            return
        for stop in call["stops"]:
            if not stop.get("passed"):
                remaining = max(0, stop["marker"] - distance)
                stop["arrival"] = time + ceil(remaining / (speed / 3.6) - 1e-9)

    async def receive(self, message):
        time, event, data = message["time"], message["event"], message["data"]
        key = message["conversation"]
        if ":" not in key:
            return
        call_id, crossing = key.split(":", 1)
        if call_id not in self.calls:
            return
        stop = next(stop for stop in self.calls[call_id]["stops"] if stop["crossing"] == crossing)
        if event == "corridor_reply":
            stop["reply"] = data["decision"]
            if data["decision"] == "REFUSE":
                self.log(time, "dispatch_warning", crossing=crossing, reason=data["reason"],
                         available_at=data.get("available_at"))
        elif event == "corridor_ready":
            stop["ready"] = True
        elif event == "corridor_unavailable":
            stop["ready"] = False
            self.log(time, "dispatch_warning", crossing=crossing, reason=data["reason"])

    async def tick(self, time):
        for call_id, call in list(self.calls.items()):
            for stop in call["stops"]:
                if stop.get("passed"):
                    continue
                crossing, axis, arrival = stop["crossing"], stop["axis"], stop["arrival"]
                key, target = f"{call_id}:{crossing}", f"light-{crossing}"
                sent = stop.get("sent")
                if sent is None and 0 <= arrival - time <= 30:
                    stop.update(sent=time, last_eta=arrival, retried=False)
                    await self.send(time, target, "corridor_request", key, axis=axis,
                                    arrival=arrival, eta=arrival - time)
                elif sent is not None and abs(arrival - stop["last_eta"]) > 5:
                    stop.update(sent=time, last_eta=arrival, retried=False, reply=None)
                    await self.send(time, target, "corridor_update", key, axis=axis,
                                    arrival=arrival, eta=max(0, arrival - time))
                elif sent is not None and not stop.get("reply") and time - sent >= 3:
                    if not stop["retried"]:
                        stop.update(sent=time, retried=True)
                        await self.send(time, target, "corridor_request", key, axis=axis,
                                        arrival=arrival, eta=max(0, arrival - time))
                    else:
                        stop["reply"] = "TIMEOUT"
                        self.log(time, "dispatch_warning", crossing=crossing, reason="no_reply")
                if time >= arrival and stop.get("ready"):
                    stop["passed"] = True
                    self.log(time, "vehicle_passed", crossing=crossing, call=call_id)
                    await self.send(time, target, "corridor_cancel", key)
            if all(stop.get("passed") for stop in call["stops"]):
                await self.send(time, "coordinator", "emergency_finished", call_id)
                del self.calls[call_id]
