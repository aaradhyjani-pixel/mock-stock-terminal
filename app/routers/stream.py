"""WebSocket endpoints.

A participant socket subscribes to two channels: the public tape and its own
team's private channel. There is no way to subscribe to another team's channel,
because the channel set is derived from the token rather than sent by the
client.

The first message on a fresh connection is a full snapshot, so a phone that
reconnects after a lift or a dead spot is immediately correct rather than
waiting for the next tick to repaint.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from ..config import get_rules
from ..db import session_scope
from ..engine.market import get_engine
from ..engine.matching import load_marks, valuate_team
from ..models import Instrument, Team
from ..schemas import instrument_row, position_row
from ..security import read_token
from ..ws import OPS, PUBLIC, hub, team_channel

router = APIRouter(tags=["stream"])


async def _snapshot(team_id: int | None) -> dict:
    async with session_scope() as session:
        engine = get_engine()
        state = await engine.get_state(session)
        instruments = list(
            (
                await session.execute(
                    select(Instrument)
                    .where(Instrument.listed.is_(True))
                    .order_by(Instrument.display_order, Instrument.symbol)
                )
            ).scalars()
        )
        payload = {
            "market": engine.state_payload(state),
            "instruments": [instrument_row(i) for i in instruments],
            "rules": {
                "competition": get_rules().competition_name,
                "max_leverage": str(get_rules().margin.max_leverage),
            },
        }
        if team_id is not None:
            team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
            if team is not None:
                marks = await load_marks(session)
                valuation, positions = await valuate_team(session, team, marks)
                payload["portfolio"] = {
                    "funds": valuation.as_dict(),
                    "positions": [
                        position_row(p, marks.get(p.symbol)) for p in positions if p.qty != 0
                    ],
                }
        return payload


@router.websocket("/ws")
async def participant_stream(websocket: WebSocket, token: str = Query(default="")):
    """Live tape and private team events.

    The token comes as a query parameter because browsers cannot set headers on
    a WebSocket handshake. It is the same short-lived access token the REST API
    uses, and it is verified before the socket is accepted.
    """
    payload = read_token(token) if token else None
    if payload is None or payload.get("typ") != "member" or payload.get("kind") != "access":
        await websocket.close(code=4401)
        return

    team_id = payload.get("tid")
    channels = {PUBLIC, team_channel(team_id)}
    connection = await hub.connect(websocket, channels, label=f"team:{team_id}")
    try:
        await websocket.send_text(_encode_snapshot(await _snapshot(team_id)))
        while True:
            # The client sends nothing but keepalives; reading is how we notice
            # the socket has gone away.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.disconnect(connection)


@router.websocket("/ws/ops")
async def operator_stream(websocket: WebSocket, token: str = Query(default="")):
    payload = read_token(token) if token else None
    if payload is None or payload.get("typ") != "operator" or payload.get("kind") != "access":
        await websocket.close(code=4401)
        return

    connection = await hub.connect(
        websocket, {PUBLIC, OPS}, label=f"ops:{payload.get('role', '?')}"
    )
    try:
        await websocket.send_text(_encode_snapshot(await _snapshot(None)))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.disconnect(connection)


def _encode_snapshot(data: dict) -> str:
    from ..ws import encode

    return encode("snapshot", data)


async def heartbeat() -> None:
    """Keep sockets warm through venue Wi-Fi that drops idle connections."""
    while True:
        await asyncio.sleep(20)
        with contextlib.suppress(Exception):
            await hub.broadcast_public("ping", {})
