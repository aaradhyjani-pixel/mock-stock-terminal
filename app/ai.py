"""AI-drafted news, for the operator console's news desk.

One job: turn an operator's rough prompt ("Infosys misses earnings, guidance
cut") into a headline and a short body in the voice of financial-news copy,
that the news desk then edits and publishes through the ordinary path. This
module never publishes anything itself and never sees or influences prices —
it drafts text, nothing else.

Two things keep this bounded:

* The prompt only ever asks for copy shaped like a wire headline plus two or
  three sentences. There is no chat, no memory of earlier turns, no tool use.
  Each call is a single, disposable request with no way to accumulate context
  the operator did not type themselves.
* If the key is unset, ``draft_news`` raises a clear, specific error rather
  than the button silently doing nothing. Nothing about the exchange depends
  on this working: a competition can run entirely without it, exactly as it
  did before this file existed.
"""

from __future__ import annotations

from dataclasses import dataclass

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
