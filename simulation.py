"""Deterministic demonstration inputs and simulation clock."""

import asyncio
import os

from agents import CROSSINGS, Coordinator, Emergency, Light, Sensor
from traffic import World
from transport import Broker, Journal

AMBULANCE_START, AMBULANCE_SPEED = 5, 50
AMBULANCE_ROUTE = (("P1", 430), ("P2", 930))
AMQP_URL = "amqp://traffic:traffic-demo@127.0.0.1:5672/"
SCENARIOS = {
    "UC-01": ("Адаптация фаз", "Очередь меняет длительность зелёного на П1.", 108),
    "UC-02": ("Зелёная волна", "П1 и П2 включают зелёный вдоль улицы со сдвигом 22 с.", 285),
    "UC-03": ("Разгрузка затора", "П2 разгружает очередь, а П1 ограничивает приток.", 465),
    "UC-04": ("Отказ датчика", "Датчик П3 отказывает. Светофор продолжает цикл и восстанавливает адаптацию.", 245),
    "UC-05": ("Проезд скорой", "Скорая движется по маршруту П1 → П2. Светофоры готовят зелёный коридор.", 104),
}


class Simulation:
    def __init__(self, scenario, speed):
        self.scenario, self.speed, self.time = scenario, speed, 0
        self.world = World(scenario)
        self.journal = Journal()
        self.bus = Broker(self.journal)
        self.lights = {cross: Light(cross, self.bus) for cross in CROSSINGS}
        self.sensors = {cross: Sensor(cross, self.bus) for cross in CROSSINGS}
        self.coordinator = Coordinator(self.bus)
        self.emergency = Emergency(self.bus)
        self.duration = SCENARIOS[scenario][2]
        self.status, self.error = "starting", None
        self.outputs = self.signal_outputs()
        self.published = self.snapshot()

    def signal_outputs(self):
        return {cross: light.snapshot() for cross, light in self.lights.items()}

    def snapshot(self):
        crossings = {}
        for cross, output in self.outputs.items():
            crossings[cross] = {
                **output, "approaches": self.world.sample(cross),
            }
        ambulance = None
        if self.scenario == "UC-05" and AMBULANCE_START <= self.time <= 95:
            x = (self.ambulance_distance() - AMBULANCE_ROUTE[0][1]) / 500
            ambulance = {"x": x}
        return {
            "scenario": self.scenario, "time": self.time, "duration": self.duration, "speed": self.speed,
            "status": self.status, "error": self.error, "crossings": crossings,
            "ambulance": ambulance, "vehicles": self.world.vehicles(), "event_count": len(self.journal.events),
        }

    def ambulance_distance(self):
        return (self.time - AMBULANCE_START) * (AMBULANCE_SPEED / 3.6)

    async def run(self):
        agents = {agent.name: agent for agent in (
            *self.sensors.values(), *self.lights.values(), self.coordinator, self.emergency,
        )}
        opened = False
        try:
            await self.bus.open(os.getenv("AMQP_URL", AMQP_URL), agents)
            opened, self.status = True, "running"
            self.journal.add(0, "simulator", "scenario_started", scenario=self.scenario,
                             broker="RabbitMQ", agents=len(agents))
            for second in range(self.duration + 1):
                self.time = second
                if second:
                    self.world.tick(second, self.outputs)
                if second % 5 == 0:
                    for cross, sensor in self.sensors.items():
                        await sensor.measure(second, self.world.detector(cross, second))
                    await self.bus.settle()
                if self.scenario == "UC-05" and second >= AMBULANCE_START:
                    distance = self.ambulance_distance()
                    if second == AMBULANCE_START:
                        await self.emergency.start(second, "ambulance-1", AMBULANCE_ROUTE,
                                                   distance, AMBULANCE_SPEED)
                    else:
                        self.emergency.update_position(second, "ambulance-1", distance, AMBULANCE_SPEED)
                await self.emergency.tick(second)
                await self.bus.settle()
                await self.coordinator.tick(second)
                await self.bus.settle()
                for light in self.lights.values():
                    await light.tick(second)
                await self.bus.settle()
                self.outputs = self.signal_outputs()
                self.published = self.snapshot()
                if self.speed and second < self.duration:
                    await asyncio.sleep(1 / self.speed)
            self.status = "finished"
            self.journal.add(self.time, "simulator", "scenario_finished", scenario=self.scenario)
        except asyncio.CancelledError:
            self.status = "stopped"
            raise
        except Exception as error:
            self.status, self.error = "error", str(error)
            self.journal.add(self.time, "simulator", "run_error", reason=self.error)
        finally:
            if opened:
                await self.bus.close()
            self.published = self.snapshot()
