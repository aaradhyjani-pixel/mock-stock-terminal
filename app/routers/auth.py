"""Sign in and sign out, for participants and operators.

Participants and operators authenticate through separate endpoints, carry
separate cookies and are checked by separate dependencies. A participant token
is never accepted by an admin route, and the reverse, which is the kind of thing
that is much easier to guarantee when the two paths never share code.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import AuditLog, Member, Operator, Team, utcnow
from ..schemas import LoginRequest
from ..security import (
    MemberIdentity,
    OperatorIdentity,
    clear_auth_cookies,
    current_member,
    current_operator,
    issue_member_tokens,
    issue_operator_tokens,
    login_ip_limiter,
    login_limiter,
    read_token,
    set_auth_cookies,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

BAD_CREDENTIALS = "That login and password do not match. Check the card handed to your team."


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
):
    """Participant sign-in with a member login and password."""
    ip = _client_ip(request)
    if not login_ip_limiter.check(f"ip:{ip}") or not login_limiter.check(
        f"login:{payload.login.lower()}"
    ):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Wait five minutes, or ask the help desk to reset your password.",
        )

    member = (
        await session.execute(select(Member).where(Member.login == payload.login.strip().lower()))
    ).scalar_one_or_none()
    if member is None or not member.active or not verify_password(payload.password, member.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, BAD_CREDENTIALS)

    team = (await session.execute(select(Team).where(Team.id == member.team_id))).scalar_one()
    member.last_seen_at = utcnow()
    login_limiter.reset(f"login:{payload.login.lower()}")

    access, refresh = issue_member_tokens(member)
    set_auth_cookies(response, access, refresh)
    session.add(
        AuditLog(
            actor_type="member",
            actor_id=member.id,
            actor_name=member.name,
            action="login",
            target=f"team:{team.code}",
            ip=ip,
        )
    )
    return {
        "access_token": access,
        "member": {"id": member.id, "name": member.name, "role": member.role.value},
        "team": {"id": team.id, "name": team.name, "code": team.code, "status": team.status.value},
    }


@router.post("/refresh")
async def refresh_session(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
):
    """Trade a refresh cookie for a new access token."""
    token = request.cookies.get("refresh_token")
    payload = read_token(token) if token else None
    if payload is None or payload.get("kind") != "refresh" or payload.get("typ") != "member":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session expired. Sign in again.")

    member = (
        await session.execute(select(Member).where(Member.id == payload["sub"]))
    ).scalar_one_or_none()
    if member is None or not member.active or member.token_version != payload.get("v"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session is no longer valid. Sign in again.")

    access, new_refresh = issue_member_tokens(member)
    set_auth_cookies(response, access, new_refresh)
    return {"access_token": access}


@router.post("/logout")
async def logout(response: Response):
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me")
async def whoami(identity: MemberIdentity = Depends(current_member)):
    return {
        "member": {
            "id": identity.member.id,
            "name": identity.member.name,
            "login": identity.member.login,
            "role": identity.member.role.value,
        },
        "team": {
            "id": identity.team.id,
            "name": identity.team.name,
            "code": identity.team.code,
            "status": identity.team.status.value,
        },
    }


# ------------------------------------------------------------------ operators


@router.post("/ops/login")
async def operator_login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)
    if not login_ip_limiter.check(f"ops-ip:{ip}") or not login_limiter.check(
        f"ops:{payload.login.lower()}"
    ):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Wait five minutes.")

    operator = (
        await session.execute(select(Operator).where(Operator.login == payload.login.strip().lower()))
    ).scalar_one_or_none()
    if operator is None or not operator.active or not verify_password(payload.password, operator.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That login and password do not match.")

    login_limiter.reset(f"ops:{payload.login.lower()}")
    access, refresh = issue_operator_tokens(operator)
    set_auth_cookies(response, access, refresh, operator=True)
    session.add(
        AuditLog(
            actor_type="operator",
            actor_id=operator.id,
            actor_name=operator.name,
            action="ops_login",
            target=operator.role.value,
            ip=ip,
        )
    )
    return {
        "access_token": access,
        "operator": {"id": operator.id, "name": operator.name, "role": operator.role.value},
    }


@router.post("/ops/refresh")
async def operator_refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
):
    token = request.cookies.get("ops_refresh")
    payload = read_token(token) if token else None
    if payload is None or payload.get("kind") != "refresh" or payload.get("typ") != "operator":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session expired. Sign in again.")
    operator = (
        await session.execute(select(Operator).where(Operator.id == payload["sub"]))
    ).scalar_one_or_none()
    if operator is None or not operator.active or operator.token_version != payload.get("v"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session is no longer valid.")
    access, new_refresh = issue_operator_tokens(operator)
    set_auth_cookies(response, access, new_refresh, operator=True)
    return {"access_token": access}


@router.post("/ops/logout")
async def operator_logout(response: Response):
    clear_auth_cookies(response, operator=True)
    return {"ok": True}


@router.get("/ops/me")
async def operator_whoami(identity: OperatorIdentity = Depends(current_operator)):
    return {
        "operator": {
            "id": identity.operator.id,
            "name": identity.operator.name,
            "role": identity.role.value,
        }
    }
