"""Create teams from a registration CSV and print their credential cards.

    python -m scripts.import_teams teams.csv --cards cards.html

Input CSV, header row required. ``members`` is a semicolon-separated list:

    team_name,college,contact_email,members
    Bulls of Bangalore,Christ University,a@b.com,Aarav;Diya;Rohan
    Alpha Seekers,St Josephs,c@d.com,Ishaan;Meera

Passwords are generated here, stored only as scrypt hashes, and printed once.
There is no way to recover them afterwards; the help desk issues a new one. The
optional ``--cards`` file is a print-ready page, one card per team, for cutting
up and handing out at check-in.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_rules  # noqa: E402
from app.db import create_all, dispose_engine, session_scope  # noqa: E402
from app.engine.matching import post_ledger  # noqa: E402
from app.models import LedgerKind, Member, MemberRole, Team  # noqa: E402
from app.security import generate_code, generate_password, hash_password  # noqa: E402


async def import_rows(rows: list[dict], skip_existing: bool = True) -> list[dict]:
    rules = get_rules()
    created: list[dict] = []

    async with session_scope() as session:
        for row in rows:
            name = (row.get("team_name") or "").strip()
            if not name:
                continue
            existing = (
                await session.execute(select(Team).where(Team.name == name))
            ).scalar_one_or_none()
            if existing is not None:
                if skip_existing:
                    print(f"  skipping {name}: already exists")
                    continue
                raise SystemExit(f"Team already exists: {name}")

            code = generate_code()
            team = Team(
                name=name,
                code=code,
                cash=Decimal("0"),
                college=(row.get("college") or "").strip() or None,
                contact_email=(row.get("contact_email") or "").strip() or None,
            )
            session.add(team)
            await session.flush()

            post_ledger(
                session, team, LedgerKind.OPENING, rules.starting_capital,
                note="Opening balance", day_no=0,
            )

            member_names = [m.strip() for m in (row.get("members") or "").split(";") if m.strip()]
            if not member_names:
                member_names = ["Captain"]

            members = []
            for index, member_name in enumerate(member_names[: rules.max_members_per_team]):
                password = generate_password()
                login = f"{code.lower()}-{index + 1}"
                session.add(
                    Member(
                        team_id=team.id,
                        name=member_name,
                        login=login,
                        password_hash=hash_password(password),
                        role=MemberRole.CAPTAIN if index == 0 else MemberRole.MEMBER,
                    )
                )
                members.append({"name": member_name, "login": login, "password": password})

            created.append({
                "team": name,
                "code": code,
                "college": team.college,
                "members": members,
            })

    return created


def write_cards(created: list[dict], path: Path, competition: str) -> None:
    """A print-ready page of credential cards, one per team."""
    cards = "".join(
        f"""
    <div class="card">
      <div class="head">
        <span class="comp">{html.escape(competition)}</span>
        <span class="code">{html.escape(entry['code'])}</span>
      </div>
      <h2>{html.escape(entry['team'])}</h2>
      {f'<div class="college">{html.escape(entry["college"])}</div>' if entry.get('college') else ''}
      <table>
        <tr><th>Member</th><th>Login</th><th>Password</th></tr>
        {''.join(
            f"<tr><td>{html.escape(m['name'])}</td>"
            f"<td class='mono'>{html.escape(m['login'])}</td>"
            f"<td class='mono pw'>{html.escape(m['password'])}</td></tr>"
            for m in entry['members']
        )}
      </table>
      <div class="foot">Sign in at the address on the screen. Keep this card; passwords cannot be recovered.</div>
    </div>"""
        for entry in created
    )

    path.write_text(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Credential cards</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font: 12px -apple-system, "Segoe UI", Roboto, sans-serif; color: #111; margin: 0; }}
  .sheet {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8mm; }}
  .card {{ border: 1px solid #999; border-radius: 3mm; padding: 5mm; page-break-inside: avoid; }}
  .head {{ display: flex; justify-content: space-between; align-items: baseline;
           border-bottom: 1px solid #ddd; padding-bottom: 2mm; margin-bottom: 3mm; }}
  .comp {{ font-size: 9px; letter-spacing: .08em; text-transform: uppercase; color: #666; }}
  .code {{ font: 700 15px ui-monospace, Menlo, monospace; letter-spacing: .06em; }}
  h2 {{ margin: 0 0 1mm; font-size: 15px; }}
  .college {{ color: #666; font-size: 11px; margin-bottom: 3mm; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 3mm; }}
  th {{ text-align: left; font-size: 8px; letter-spacing: .07em; text-transform: uppercase;
        color: #666; border-bottom: 1px solid #ddd; padding: 1mm 0; }}
  td {{ padding: 1.5mm 0; border-bottom: 1px dotted #ddd; font-size: 11px; }}
  .mono {{ font-family: ui-monospace, Menlo, monospace; }}
  .pw {{ font-weight: 700; letter-spacing: .04em; }}
  .foot {{ margin-top: 3mm; font-size: 8.5px; color: #666; }}
</style></head>
<body><div class="sheet">{cards}</div></body></html>
""")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Import teams from a registration CSV.")
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--cards", type=Path, help="Write a print-ready HTML page of credential cards.")
    parser.add_argument("--json", type=Path, help="Write the credentials as JSON (contains cleartext passwords).")
    args = parser.parse_args()

    if not args.csv_file.exists():
        raise SystemExit(f"No such file: {args.csv_file}")

    with args.csv_file.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("The CSV has no rows.")

    await create_all()
    created = await import_rows(rows)
    print(f"\nCreated {len(created)} teams.\n")

    for entry in created:
        print(f"{entry['team']}  [{entry['code']}]")
        for member in entry["members"]:
            print(f"    {member['name']:<24} {member['login']:<12} {member['password']}")
        print()

    if args.cards:
        write_cards(created, args.cards, get_rules().competition_name)
        print(f"Credential cards written to {args.cards}. Open it and print.")

    if args.json:
        import json

        args.json.write_text(json.dumps(created, indent=2))
        print(f"Credentials written to {args.json}. This file contains cleartext passwords.")

    print("\nThese passwords are not stored and cannot be recovered. Print them now.")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
