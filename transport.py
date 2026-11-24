"""Addressed AMQP messages and a journal of a single simulation run."""

import asyncio
import json
from uuid import uuid4

import aio_pika

PERFORMATIVES = {
    "sensor_failure": "FAILURE", "apply_plan": "REQUEST",
    "commit_plan": "INFORM", "cancel_plan": "CANCEL",
    "corridor_request": "REQUEST", "corridor_update": "REQUEST",
    "corridor_ready": "CONFIRM", "corridor_cancel": "CANCEL",
    "corridor_unavailable": "FAILURE",
}


class Journal:
    def __init__(self):
        self.events = []

    def add(self, time, sender, event, *, receiver=None, conversation=None, **data):
        self.events.append({
            "time": time, "sender": sender, "receiver": receiver, "event": event,
            "conversation": conversation, "data": data,
        })

    def text(self):
        return "".join(
            f't={row["time"]:03d}s  {row["sender"]}'
            + (f' → {row["receiver"]}' if row["receiver"] else "")
            + f'  {row["event"]}'
            + (f' [{row["conversation"]}]' if row["conversation"] else "")
            + "  " + json.dumps(row["data"], ensure_ascii=False) + "\n"
            for row in self.events
        )


class Broker:
    def __init__(self, journal):
        self.journal = journal
        self.id = uuid4().hex[:10]
        self.done = asyncio.Event()
        self.done.set()
        self.pending = 0
        self.error = None

    async def open(self, url, agents):
        self.connection = await aio_pika.connect(url, timeout=5)
        try:
            self.channel = await self.connection.channel(on_return_raises=True)
            self.exchange = await self.channel.declare_exchange(
                f"traffic.{self.id}", aio_pika.ExchangeType.DIRECT, auto_delete=True
            )
            for name, agent in agents.items():
                queue = await self.channel.declare_queue(f"{self.id}.{name}", exclusive=True)
                await queue.bind(self.exchange, routing_key=name)

                lock = asyncio.Lock()

                async def receive(incoming, recipient=agent, inbox=lock):
                    try:
                        async with inbox, incoming.process():
                            message = json.loads(incoming.body)
                            await recipient.receive(message)
                    except Exception as error:
                        self.error = error
                    finally:
                        self.finish_delivery()

                await queue.consume(receive)
        except BaseException:
            await self.connection.close()
            raise

    def finish_delivery(self):
        self.pending -= 1
        if self.pending == 0:
            self.done.set()

    async def send(self, time, sender, receiver, event, conversation=None, **data):
        performative = data.get("decision") or PERFORMATIVES.get(event, "INFORM")
        message = {
            "time": time, "sender": sender, "receiver": receiver, "event": event,
            "conversation": conversation or f"{sender}:{event}:{time}",
            "performative": performative, "data": data,
        }
        self.pending += 1
        self.done.clear()
        self.journal.add(
            time, sender, event, receiver=receiver, conversation=message["conversation"],
            acl=performative, **data,
        )
        try:
            await self.exchange.publish(
                aio_pika.Message(json.dumps(message).encode(), content_type="application/json"),
                routing_key=receiver, mandatory=True, timeout=5,
            )
        except Exception:
            self.finish_delivery()
            raise

    async def settle(self):
        # The simulator advances its clock only after this round of AMQP messages.
        await asyncio.wait_for(self.done.wait(), timeout=8)
        if self.error:
            raise RuntimeError("Ошибка обработки сообщения RabbitMQ") from self.error

    async def close(self):
        await self.connection.close()
