"""WebSocket fan-out.

Five hundred phones on venue Wi-Fi means some of them will be slow, and a slow
client must never slow down the engine. Each connection owns a bounded queue and
its own writer task. When a queue fills, the oldest quote is dropped rather than
the connection: a phone that missed three price updates wants the newest one, not
a backlog. Anything that must not be dropped (a fill, a margin call) is marked
durable and will evict quotes to make room, and only closes the connection if it
still cannot fit, at which point that client reconnects and resyncs over REST.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("exchange.ws")

PUBLIC = "public"
OPS = "ops"


def team_channel(team_id: int) -> str:
    return f"team:{team_id}"


def _default(value: Any):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):  # enums
        return value.value
    raise TypeError(f"cannot serialise {type(value).__name__}")


def encode(event: str, data: Any) -> str:
    return json.dumps({"event": event, "data": data}, default=_default, separators=(",", ":"))


# eq=False keeps identity hashing and equality. Connections live in sets and
# dicts keyed by the object itself, and two different sockets are never "equal"
# just because their fields happen to match.
@dataclass(eq=False)
class Connection:
    websocket: WebSocket
    channels: set[str]
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    label: str = "anon"
    dropped: int = 0

    async def writer(self) -> None:
        try:
            while True:
                message = await self.queue.get()
                await self.websocket.send_text(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def offer(self, message: str, durable: bool) -> bool:
        """Queue a message. Returns False when the connection should be closed."""
        try:
            self.queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            if not durable:
                self.dropped += 1
                return True
            # Make room by discarding the oldest queued update.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(message)
                self.dropped += 1
                return True
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                return False


class Hub:
    def __init__(self) -> None:
        self._by_channel: dict[str, set[Connection]] = defaultdict(set)
        self._connections: set[Connection] = set()
        self._tasks: dict[Connection, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, channels: set[str], label: str = "anon") -> Connection:
        await websocket.accept()
        connection = Connection(websocket=websocket, channels=set(channels), label=label)
        async with self._lock:
            self._connections.add(connection)
            for channel in connection.channels:
                self._by_channel[channel].add(connection)
            self._tasks[connection] = asyncio.create_task(connection.writer())
        return connection

    async def disconnect(self, connection: Connection) -> None:
        async with self._lock:
            self._connections.discard(connection)
            for channel in connection.channels:
                self._by_channel[channel].discard(connection)
            task = self._tasks.pop(connection, None)
        if task is not None:
            task.cancel()

    async def publish(self, channel: str, event: str, data: Any, *, durable: bool = False) -> None:
        message = encode(event, data)
        async with self._lock:
            targets = list(self._by_channel.get(channel, ()))
        casualties = [c for c in targets if not c.offer(message, durable)]
        for connection in casualties:
            log.warning("dropping slow websocket %s on %s", connection.label, channel)
            await self.disconnect(connection)
            try:
                await connection.websocket.close(code=1013)  # try again later
            except Exception:
                pass

    async def publish_many(self, channels: list[str], event: str, data: Any, *, durable: bool = False) -> None:
        for channel in channels:
            await self.publish(channel, event, data, durable=durable)

    async def broadcast_public(self, event: str, data: Any, *, durable: bool = False) -> None:
        await self.publish(PUBLIC, event, data, durable=durable)
        await self.publish(OPS, event, data, durable=durable)

    async def to_team(self, team_id: int, event: str, data: Any) -> None:
        # Anything addressed to one team is something that team must not miss.
        await self.publish(team_channel(team_id), event, data, durable=True)

    async def to_ops(self, event: str, data: Any, *, durable: bool = False) -> None:
        await self.publish(OPS, event, data, durable=durable)

    @property
    def stats(self) -> dict:
        return {
            "connections": len(self._connections),
            "public": len(self._by_channel.get(PUBLIC, ())),
            "ops": len(self._by_channel.get(OPS, ())),
            "teams": len([c for c in self._by_channel if c.startswith("team:")]),
            "dropped_messages": sum(c.dropped for c in self._connections),
        }

    async def close_all(self) -> None:
        for connection in list(self._connections):
            await self.disconnect(connection)
            try:
                await connection.websocket.close()
            except Exception:
                pass


hub = Hub()
