"""Load test: hold N WebSocket connections and fire M orders a second.

    python -m scripts.loadtest --clients 1500 --orders-per-second 200 --seconds 120

Run this against a staging server, never against the live one. It needs teams to
exist; seed with ``--demo-teams`` first, or point it at the practice database.

What to look for. The gates from the design document:

  order p95 latency  under 300 ms
  tick latency p95   under 100 ms   (read it from /healthz on the server)
  dropped sockets    zero
  rejected orders    only for real reasons: funds, rate limits, market state

If p95 order latency climbs while tick latency stays flat, the bottleneck is the
team lock rather than the engine, which means one team is being hammered rather
than the field being busy. That is a test artefact, not a production risk; give
the test more teams.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class Results:
    def __init__(self) -> None:
        self.latencies: list[float] = []
        self.filled = 0
        self.rejected: dict[str, int] = {}
        self.errors = 0
        self.messages = 0
        self.sockets_open = 0
        self.sockets_lost = 0

    def record_reject(self, reason: str) -> None:
        key = (reason or "unknown")[:60]
        self.rejected[key] = self.rejected.get(key, 0) + 1

    def report(self, seconds: float) -> None:
        print("\n" + "=" * 62)
        print(f"{'Load test results':<40}{seconds:>10.1f}s")
        print("=" * 62)
        print(f"{'WebSocket clients held':<40}{self.sockets_open:>10}")
        print(f"{'WebSocket clients lost':<40}{self.sockets_lost:>10}")
        print(f"{'Messages received':<40}{self.messages:>10}")
        print(f"{'Orders filled or accepted':<40}{self.filled:>10}")
        print(f"{'Orders rejected':<40}{sum(self.rejected.values()):>10}")
        print(f"{'Transport errors':<40}{self.errors:>10}")

        if self.latencies:
            ordered = sorted(self.latencies)
            p = lambda q: ordered[min(len(ordered) - 1, int(len(ordered) * q))] * 1000  # noqa: E731
            print()
            print(f"{'Order latency mean':<40}{statistics.mean(ordered) * 1000:>9.1f}ms")
            print(f"{'Order latency p50':<40}{p(0.50):>9.1f}ms")
            print(f"{'Order latency p95':<40}{p(0.95):>9.1f}ms")
            print(f"{'Order latency p99':<40}{p(0.99):>9.1f}ms")
            print(f"{'Orders per second':<40}{len(ordered) / seconds:>10.1f}")
            verdict = "PASS" if p(0.95) < 300 else "FAIL"
            print(f"\n{'p95 under the 300ms gate':<40}{verdict:>10}")

        if self.rejected:
            print("\nRejections by reason:")
            for reason, count in sorted(self.rejected.items(), key=lambda kv: -kv[1]):
                print(f"  {count:>6}  {reason}")
        print()


async def sign_in(client: httpx.AsyncClient, base: str, login: str, password: str) -> str | None:
    try:
        response = await client.post(f"{base}/api/auth/login", json={"login": login, "password": password})
        if response.status_code == 200:
            return response.json()["access_token"]
    except Exception:
        pass
    return None


async def hold_socket(base: str, token: str, results: Results, stop: asyncio.Event) -> None:
    """One idle client, exactly like a phone sitting on a desk."""
    url = base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?token={token}"
    try:
        async with websockets.connect(url, open_timeout=20, close_timeout=5) as socket:
            results.sockets_open += 1
            while not stop.is_set():
                try:
                    await asyncio.wait_for(socket.recv(), timeout=1.0)
                    results.messages += 1
                except asyncio.TimeoutError:
                    continue
    except Exception:
        results.sockets_lost += 1


async def trade_loop(
    client: httpx.AsyncClient,
    base: str,
    token: str,
    symbols: list[str],
    results: Results,
    stop: asyncio.Event,
    interval: float,
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    rng = random.Random()
    while not stop.is_set():
        await asyncio.sleep(interval * (0.5 + rng.random()))
        body = {
            "symbol": rng.choice(symbols),
            "side": rng.choice(["BUY", "SELL"]),
            "order_type": "MARKET",
            "qty": rng.randint(1, 40),
            "client_order_id": f"lt-{time.time_ns()}-{rng.randint(0, 1 << 30)}",
        }
        started = time.perf_counter()
        try:
            response = await client.post(f"{base}/api/orders", json=body, headers=headers, timeout=20)
            results.latencies.append(time.perf_counter() - started)
            if response.status_code == 429:
                results.record_reject("rate limited (429)")
            elif response.status_code >= 400:
                results.record_reject(f"http {response.status_code}")
            else:
                payload = response.json()
                if payload.get("status") == "REJECTED":
                    results.record_reject(payload.get("reason", "rejected"))
                else:
                    results.filled += 1
        except Exception as exc:
            results.errors += 1
            results.record_reject(type(exc).__name__)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Load test the mock exchange.")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--clients", type=int, default=500, help="WebSocket connections to hold open.")
    parser.add_argument("--orders-per-second", type=float, default=50.0)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--credentials", type=Path, help="JSON from import_teams --json.")
    parser.add_argument("--password", default=None, help="Shared password if all logins use one.")
    parser.add_argument("--logins", nargs="*", help="Explicit logins, used with --password.")
    args = parser.parse_args()

    results = Results()
    stop = asyncio.Event()

    async with httpx.AsyncClient(timeout=30) as client:
        market = (await client.get(f"{args.base}/api/market")).json()
        print(f"Market is {market['state']}, day {market['day_no']}.")
        if market["state"] != "OPEN":
            print("Warning: the market is not open, so every order will be rejected.")

        instruments = (await client.get(f"{args.base}/api/instruments")).json()["instruments"]
        symbols = [i["symbol"] for i in instruments]
        print(f"{len(symbols)} instruments listed.")

        pairs: list[tuple[str, str]] = []
        if args.credentials and args.credentials.exists():
            for entry in json.loads(args.credentials.read_text()):
                for member in entry["members"]:
                    pairs.append((member["login"], member["password"]))
        elif args.logins and args.password:
            pairs = [(login, args.password) for login in args.logins]
        else:
            raise SystemExit(
                "Give either --credentials (from import_teams --json) or "
                "--logins with --password."
            )

        print(f"Signing in {len(pairs)} accounts...")
        tokens = [t for t in await asyncio.gather(*[sign_in(client, args.base, u, p) for u, p in pairs]) if t]
        if not tokens:
            raise SystemExit("No account signed in. Check the credentials.")
        print(f"{len(tokens)} signed in.")

        # Hold sockets: cycle through the tokens if fewer accounts than clients.
        socket_tokens = [tokens[i % len(tokens)] for i in range(args.clients)]
        socket_tasks = [asyncio.create_task(hold_socket(args.base, t, results, stop)) for t in socket_tokens]

        print(f"Opening {args.clients} sockets...")
        await asyncio.sleep(min(20, 2 + args.clients / 100))
        print(f"{results.sockets_open} sockets open, {results.sockets_lost} lost.")

        interval = len(tokens) / max(args.orders_per_second, 0.001)
        trade_tasks = [
            asyncio.create_task(trade_loop(client, args.base, t, symbols, results, stop, interval))
            for t in tokens
        ]

        print(f"Trading for {args.seconds}s at about {args.orders_per_second} orders/second...")
        started = time.perf_counter()
        await asyncio.sleep(args.seconds)
        stop.set()
        elapsed = time.perf_counter() - started

        for task in trade_tasks + socket_tasks:
            task.cancel()
        await asyncio.gather(*trade_tasks, *socket_tasks, return_exceptions=True)

        health = (await client.get(f"{args.base}/healthz")).json()
        engine = health.get("engine", {})
        print(f"\nServer-side tick latency: avg {engine.get('tick_ms_avg')}ms, "
              f"p95 {engine.get('tick_ms_p95')}ms, errors {engine.get('errors')}")
        print(f"Server-side sockets: {engine.get('websockets', {})}")

    results.report(elapsed)


if __name__ == "__main__":
    asyncio.run(main())
