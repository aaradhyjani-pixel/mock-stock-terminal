"""Passwords, tokens, and the guards that sit in front of every endpoint.

Deliberately dependency-free. Password hashing is ``hashlib.scrypt`` and tokens
are HMAC-SHA256 over a JSON payload, both from the standard library. For a
system that has to be stood up on a rented VPS the week of an event, every
dependency that is not carrying weight is a thing that can fail to install at
the wrong moment.

Tokens carry a ``token_version`` copied from the account. Bumping that column
invalidates every token already issued to that account, which is how the help
desk removes someone instantly without any server-side session store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import string
import time
from dataclasses import dataclass
from typing import Literal

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_db
from .models import Member, Operator, OperatorRole, Team

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"


# --------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        key = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(key_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key, bytes.fromhex(key_hex))


def generate_password(length: int = 10) -> str:
    """A readable one-off password. No O/0 or I/1 confusion on a printed card."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def generate_code(length: int = 6) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


# ------------------------------------------------------------------ tokens


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(payload: dict, ttl_seconds: int) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_seconds
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    secret = get_settings().secret_key.encode()
    signature = hmac.new(secret, raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def read_token(token: str) -> dict | None:
    try:
        body_b64, signature_b64 = token.split(".")
        raw = _unb64(body_b64)
        signature = _unb64(signature_b64)
    except (ValueError, TypeError):
        return None
    secret = get_settings().secret_key.encode()
    expected = hmac.new(secret, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# ------------------------------------------------------------------ identity


@dataclass
class MemberIdentity:
    member: Member
    team: Team

    @property
    def team_id(self) -> int:
        return self.team.id

    @property
    def is_captain(self) -> bool:
        return self.member.role.value == "CAPTAIN"


@dataclass
class OperatorIdentity:
    operator: Operator

    @property
    def role(self) -> OperatorRole:
        return self.operator.role


def issue_member_tokens(member: Member) -> tuple[str, str]:
    settings = get_settings()
    base = {"sub": member.id, "typ": "member", "tid": member.team_id, "v": member.token_version}
    access = make_token({**base, "kind": "access"}, settings.access_token_minutes * 60)
    refresh = make_token({**base, "kind": "refresh"}, settings.refresh_token_hours * 3600)
    return access, refresh


def issue_operator_tokens(operator: Operator) -> tuple[str, str]:
    settings = get_settings()
    base = {"sub": operator.id, "typ": "operator", "role": operator.role.value, "v": operator.token_version}
    access = make_token({**base, "kind": "access"}, settings.access_token_minutes * 60)
    refresh = make_token({**base, "kind": "refresh"}, settings.refresh_token_hours * 3600)
    return access, refresh


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


async def _payload_from_request(
    request: Request,
    access_token: str | None,
    expect: Literal["member", "operator"],
) -> dict:
    token = _bearer(request) or access_token
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue.")
    payload = read_token(token)
    if payload is None or payload.get("kind") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session expired. Sign in again.")
    if payload.get("typ") != expect:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This area is not available to your account.")
    return payload


async def current_member(
    request: Request,
    session: AsyncSession = Depends(get_db),
    access_token: str | None = Cookie(default=None),
) -> MemberIdentity:
    payload = await _payload_from_request(request, access_token, "member")
    member = (
        await session.execute(select(Member).where(Member.id == payload["sub"]))
    ).scalar_one_or_none()
    if member is None or not member.active or member.token_version != payload.get("v"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session is no longer valid. Sign in again.")
    team = (await session.execute(select(Team).where(Team.id == member.team_id))).scalar_one_or_none()
    if team is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your team no longer exists.")
    return MemberIdentity(member=member, team=team)


async def current_operator(
    request: Request,
    session: AsyncSession = Depends(get_db),
    ops_token: str | None = Cookie(default=None),
) -> OperatorIdentity:
    payload = await _payload_from_request(request, ops_token, "operator")
    operator = (
        await session.execute(select(Operator).where(Operator.id == payload["sub"]))
    ).scalar_one_or_none()
    if operator is None or not operator.active or operator.token_version != payload.get("v"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session is no longer valid. Sign in again.")
    return OperatorIdentity(operator=operator)


def require_roles(*roles: OperatorRole):
    """Dependency factory: allow only these operator roles.

    SUPER_ADMIN passes every check, so the two people running the event are
    never locked out of their own console mid-competition.
    """
    allowed = set(roles) | {OperatorRole.SUPER_ADMIN}

    async def guard(identity: OperatorIdentity = Depends(current_operator)) -> OperatorIdentity:
        if identity.role not in allowed:
            names = ", ".join(sorted(r.value.replace("_", " ").lower() for r in allowed))
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"You are signed in as {identity.role.value.replace('_', ' ').lower()}. "
                f"This action needs: {names}. Sign in with that account instead.",
            )
        return identity

    return guard


def set_auth_cookies(response, access: str, refresh: str, *, operator: bool = False) -> None:
    settings = get_settings()
    access_name = "ops_token" if operator else "access_token"
    refresh_name = "ops_refresh" if operator else "refresh_token"
    same_site = settings.cookie_samesite
    # A browser silently drops SameSite=None unless the cookie is also Secure,
    # and the failure looks like "it logs me out after half an hour" rather
    # than anything obviously wrong. Force the pair together.
    secure = settings.secure_cookies or same_site == "none"
    common = {
        "httponly": True,
        "secure": secure,
        "samesite": same_site,
        "path": "/",
    }
    response.set_cookie(access_name, access, max_age=settings.access_token_minutes * 60, **common)
    response.set_cookie(refresh_name, refresh, max_age=settings.refresh_token_hours * 3600, **common)


def clear_auth_cookies(response, *, operator: bool = False) -> None:
    names = ("ops_token", "ops_refresh") if operator else ("access_token", "refresh_token")
    for name in names:
        response.delete_cookie(name, path="/")


# -------------------------------------------------------------- rate limits


class SlidingWindow:
    """A per-key sliding window counter, in memory.

    Used for order submission and login attempts. In a single-process design
    this is exact; the moment there are two processes it becomes approximate,
    which is one more reason the architecture keeps to one.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


# Ten attempts per account in five minutes. This is the real protection: it
# stops someone guessing at one team's password.
login_limiter = SlidingWindow(limit=10, window_seconds=300)

# The per-address limit has to be generous. At the venue every participant is
# behind one NAT, so 500 people share a single public IP; a strict per-IP limit
# would have the hall locking itself out within seconds of the doors opening.
# This is a flood guard against a script, not an authentication control.
login_ip_limiter = SlidingWindow(limit=600, window_seconds=300)
