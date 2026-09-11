"""AI-drafted news and research, for the news desk and the participant
research desk respectively.

One job each. ``draft_news`` turns an operator's rough prompt ("Infosys
misses earnings, guidance cut") into a headline and a short body in the voice
of financial-news copy, that the news desk then edits and publishes through
the ordinary path. ``draft_research`` turns a stock's current public state
into a short opinion piece from an invented boutique research house, that a
team can buy for one symbol. Neither function publishes or charges anything
itself, and neither sees or influences prices — they draft text, nothing else.

Three things keep both bounded:

* Each call is a single, disposable request. No chat, no memory of earlier
  turns, no tool use, so there is no way for either to accumulate context
  beyond what it was explicitly given.
* ``draft_research`` is never given anything about scenario scripts, pending
  price actions, or operator intent — only the instrument's public,
  current-moment numbers. There is no scenario data in its input for it to
  leak, structurally, not just by instruction: an operator's planned "INFY
  halts and reopens 7% lower at 16:30" is never part of what the model sees,
  so it cannot appear in what the model writes.
* If the key is unset, both raise a clear, specific error rather than the
  button silently doing nothing. Nothing about the exchange depends on
  either working: a competition runs identically without them, exactly as
  it did before this file existed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from .config import get_settings

_SYSTEM_PROMPT = """You write short financial-news items for a simulated stock \
market exchange run at a college competition. Given a rough prompt from an \
operator, produce ONE headline and a two-to-three sentence body in the voice \
of a wire report (Reuters/Bloomberg style): concrete, factual in tone, no \
hedging, no exclamation marks, no emoji.

Rules:
- Headline: under 20 words, no trailing punctuation.
- Body: 2-3 sentences, plausible financial detail (numbers, named roles like \
"the CFO" or "analysts"), never a real company outside the ones named in the \
prompt.
- Never mention that this is a simulation, a game, a competition, an AI, or a \
draft. Write it exactly as the operator would publish it.
- Output ONLY the headline on the first line, then a blank line, then the \
body. No labels, no markdown, no quotation marks around the headline."""


class AiNewsUnavailable(Exception):
    """Raised when there is no key configured. The caller turns this into a
    plain-language 400 rather than a stack trace."""


class AiNewsError(Exception):
    """The API call itself failed (rate limit, network, bad response)."""


@dataclass(frozen=True)
class NewsDraft:
    headline: str
    body: str


def draft_news(prompt: str, *, symbols: list[str] | None = None) -> NewsDraft:
    """Ask Claude for a headline and body. Synchronous; called from a thread."""
    settings = get_settings()
    key = settings.resolved_anthropic_key
    if not key:
        raise AiNewsUnavailable(
            "AI drafting is not set up. Ask whoever runs the deployment to set "
            "EXCHANGE_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY)."
        )

    import anthropic

    user_prompt = prompt.strip()
    if symbols:
        user_prompt += f"\n\n(Tag these stocks if relevant: {', '.join(symbols)})"

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=settings.ai_news_model,
            max_tokens=300,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as exc:
        raise AiNewsError(f"The AI draft failed: {exc}") from exc

    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise AiNewsError("The AI returned an empty draft. Try rephrasing the prompt.")

    headline, _, body = text.partition("\n\n")
    headline = headline.strip().strip('"')
    body = body.strip()
    if not headline:
        raise AiNewsError("Could not parse a headline from the AI's response.")

    # A headline field has a hard length cap; a runaway response should not
    # crash the request, it should just get trimmed.
    return NewsDraft(headline=headline[:240], body=body[:2000])


# --------------------------------------------------------------- research desk

_RATINGS = ("BUY", "ACCUMULATE", "HOLD", "REDUCE", "SELL")

# Invented boutique names. Deliberately nothing close to a real Indian
# brokerage or research house - see app.py's note on why: these are fictional
# reports about real, publicly-traded companies, and attaching a real firm's
# name to a fabricated recommendation is not a line worth going near, game
# context or not.
_HOUSE_NAMES = (
    "Meridian Street Research",
    "Northbridge Capital Advisory",
    "Fathom Securities",
    "Greywood Analytics",
    "Cardinal Point Research",
    "Long Ledger Partners",
)

_RESEARCH_SYSTEM_PROMPT = """You write a short equity research note for a \
simulated stock market at a college competition. You are given one stock's
current public price and today's move, nothing else. Write from the point of
view of an equity analyst at a boutique research house.

Rules:
- One rating from exactly this list: BUY, ACCUMULATE, HOLD, REDUCE, SELL.
- One target price: a plausible number within about 15% of the current
  price, in the direction the rating implies.
- A headline under 15 words stating the call, e.g. "Initiates coverage with
  a BUY, target 1,720".
- A body of 3-4 sentences: valuation or technical-flavoured reasoning
  (P/E-style language, momentum, sector positioning, support/resistance),
  written as opinion, not fact.
- You were given only today's price and change. You have no information
  about the company's actual fundamentals, no knowledge of any pending
  announcement, and must not invent specific undisclosed events (deals,
  results, management changes). Write general market-technical opinion only.
- Never mention that this is a simulation, a competition, an AI, or a draft.
- Output exactly four lines, nothing else:
  RATING: <one word from the list>
  TARGET: <a number, no currency symbol, no commas>
  HEADLINE: <the headline>
  BODY: <the body, one paragraph>"""


@dataclass(frozen=True)
class ResearchDraft:
    house_name: str
    rating: str
    target_price: Decimal | None
    headline: str
    body: str


def draft_research(
    symbol: str, name: str, sector: str, last_price: Decimal, change_pct: Decimal
) -> ResearchDraft:
    """A fictional research house's take on one stock, from its public price
    alone. Synchronous; called from a thread."""
    settings = get_settings()
    key = settings.resolved_anthropic_key
    if not key:
        raise AiNewsUnavailable(
            "The research desk is not set up. Ask whoever runs the deployment to "
            "set EXCHANGE_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY)."
        )

    import anthropic

    house_name = random.choice(_HOUSE_NAMES)
    user_prompt = (
        f"Stock: {symbol} ({name}), sector: {sector}. "
        f"Current price: {last_price}. Today's move: {change_pct}%. "
        f"You are writing as an analyst at {house_name}."
    )

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=settings.ai_news_model,
            max_tokens=350,
            system=_RESEARCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as exc:
        raise AiNewsError(f"The research desk failed: {exc}") from exc

    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise AiNewsError("The research desk returned nothing. Try again.")

    fields: dict[str, str] = {}
    for line in text.splitlines():
        key_part, sep, value = line.partition(":")
        if sep:
            fields[key_part.strip().upper()] = value.strip()

    rating = fields.get("RATING", "").upper()
    if rating not in _RATINGS:
        rating = "HOLD"

    target_price: Decimal | None = None
    try:
        raw_target = fields.get("TARGET", "").replace(",", "")
        if raw_target:
            target_price = Decimal(raw_target)
    except Exception:
        target_price = None

    headline = fields.get("HEADLINE", "").strip() or f"{house_name} rates {symbol} {rating}"
    body = fields.get("BODY", "").strip() or text[:500]

    return ResearchDraft(
        house_name=house_name,
        rating=rating,
        target_price=target_price,
        headline=headline[:240],
        body=body[:2000],
    )
