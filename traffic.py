"""Vehicle movement and detector readings on the connected road grid."""

from collections import deque
from dataclasses import dataclass

from agents import APPROACHES, CROSSINGS, NEIGHBORS

DISTANCE = 500
STOP_LINE = 17
SPEED = 50 / 3.6
GAP = 18
ENTRY = -460
EXIT = DISTANCE - ENTRY
REVERSE = {"N": "S", "S": "N", "E": "W", "W": "E"}
STOPS = (-STOP_LINE, DISTANCE - STOP_LINE)
DETECTOR = STOPS[1] - 80
HISTORY = 60


@dataclass
class Vehicle:
    id: int
    position: float


class Lane:
    def __init__(self, first, second, side):
        self.crossings = (first, second)
        self.side = side
        self.vehicles = deque()
        self.demand = 0.0
        self.detected = deque()
        self.last_entry = [-10, -10]

    def advance(self, time, outputs, headway):
        leader = EXIT + GAP
        for vehicle in self.vehicles:
            old = vehicle.position
            target = min(old + SPEED, leader - GAP)
            stop_index = 0 if old <= STOPS[0] else 1
            stop = STOPS[stop_index]
            if old <= stop < target:
                crossing = self.crossings[stop_index]
                output = outputs[crossing]
                green = output["stage"] == "green" and self.side in APPROACHES[output["axis"]]
                if green and time - self.last_entry[stop_index] >= headway(crossing, self.side, time):
                    self.last_entry[stop_index] = time
                else:
                    target = stop
            if old < DETECTOR <= target:
                self.detected.append(time)
            vehicle.position = max(old, target)
            leader = vehicle.position
        while self.vehicles and self.vehicles[0].position >= EXIT:
            self.vehicles.popleft()
        while self.detected and self.detected[0] <= time - HISTORY:
            self.detected.popleft()

    def queue_lengths(self):
        counts = []
        for lower, stop in zip((ENTRY, STOPS[0] + 2 * STOP_LINE), STOPS):
            count, front = 0, stop
            for vehicle in self.vehicles:
                if vehicle.position > stop:
                    continue
                if vehicle.position < lower or front - vehicle.position > GAP + 2:
                    break
                count += 1
                front = vehicle.position
            counts.append(count)
        return counts

    def snapshot(self):
        first, second = self.crossings
        return [
            {"id": car.id, "first": first, "side": self.side, "position": round(car.position, 2),
             "next": first if car.position <= STOPS[0] else second if car.position <= STOPS[1] else None}
            for car in self.vehicles
        ]


class World:
    capacity = 25

    def __init__(self, scenario):
        self.scenario = scenario
        self.lanes = []
        self.sequence = 0
        for first in CROSSINGS:
            for side in REVERSE:
                if side in NEIGHBORS[first]:
                    continue
                second = NEIGHBORS[first][REVERSE[side]]
                lane = Lane(first, second, side)
                for index, crossing in reversed(tuple(enumerate(lane.crossings))):
                    count = self.initial_queue(crossing, side)
                    for place in range(count):
                        lane.vehicles.append(self.vehicle(STOPS[index] - place * GAP))
                self.lanes.append(lane)
        self.update_readings(0)

    def vehicle(self, position):
        self.sequence += 1
        return Vehicle(self.sequence, position)

    def initial_queue(self, crossing, side):
        if self.scenario == "UC-01" and crossing == "P1":
            return {"N": 0, "S": 0, "W": 23, "E": 16}[side]
        return {("P1", "W"): 9, ("P3", "N"): 7}.get((crossing, side), 5)

    def source_flow(self, crossing, side, time):
        if self.scenario == "UC-01" and (crossing, side) == ("P1", "N"):
            return 0 if time < 32 else 290
        if self.scenario == "UC-01" and (crossing, side) in (("P1", "W"), ("P2", "E")):
            return 850
        if self.scenario in ("UC-02", "UC-03") and (crossing, side) == ("P1", "W"):
            return 3300 if self.scenario == "UC-03" and 65 <= time < 275 else 810
        if self.scenario == "UC-04" and crossing in ("P1", "P3") and side in "NS":
            return 660
        return 300

    def headway(self, crossing, side, time):
        # Temporary downstream bottleneck in the congestion demonstration.
        if self.scenario == "UC-03" and (crossing, side) == ("P2", "W") and 105 <= time < 275:
            return 16
        return 2

    def tick(self, time, outputs):
        for lane in self.lanes:
            lane.advance(time, outputs, self.headway)
            lane.demand += self.source_flow(lane.crossings[0], lane.side, time) / 3600
            if lane.demand >= 1:
                lane.demand -= 1
                if not lane.vehicles or lane.vehicles[-1].position >= ENTRY + GAP:
                    lane.vehicles.append(self.vehicle(ENTRY))
        self.update_readings(time)

    def update_readings(self, time):
        readings = {crossing: {} for crossing in CROSSINGS}
        for lane in self.lanes:
            outside, inside = lane.queue_lengths()
            first, second = lane.crossings
            for crossing, waiting, flow in (
                (first, outside, self.source_flow(first, lane.side, time)),
                (second, inside, len(lane.detected) * 3600 // HISTORY),
            ):
                readings[crossing][lane.side] = {
                    "queue": waiting, "flow": flow, "fill": min(1.0, round(waiting / self.capacity, 2)),
                }

        self.readings = readings

    def sample(self, crossing):
        return self.readings[crossing]

    def detector(self, crossing, time):
        if self.scenario == "UC-04" and crossing == "P3" and 80 <= time < 165:
            return None
        return self.sample(crossing)

    def vehicles(self):
        return [car for lane in self.lanes for car in lane.snapshot()]
