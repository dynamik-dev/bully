"""Anthropic SDK helpers: token counting + real `messages.create` round-trips.

Stdlib-first: the optional `anthropic` import is gated behind API-key
presence. When unavailable, `count_tokens` falls back to a char-count proxy
and `full_dispatch` falls back to count_tokens for input + output_tokens=0.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BENCH_MODEL = "claude-sonnet-4-6"

# USD per million tokens (Sonnet 4.6). Used to render the --full summary's
# cost column. Bump alongside any BENCH_MODEL change.
BENCH_INPUT_PRICE_PER_MTOK = 3.0
BENCH_OUTPUT_PRICE_PER_MTOK = 15.0


def repo_root() -> Path:
    """Return the project root (directory holding `agents/`).

    Walks upward from this file until the marker directory appears. Replaces
    the previous `parent.parent` heuristic that broke under src layout.
    """
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "agents").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    return here.parents[3]  # fallback: src/bully/bench/dispatch.py -> repo root


def import_anthropic():
    """Import and return the anthropic module, or None if unavailable."""
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return None
    return anthropic


def load_evaluator_system_prompt() -> str:
    """Load the bully-evaluator system prompt from agents/bully-evaluator.md.

    Strips the YAML frontmatter (everything between the first `---` pair).
    """
    path = repo_root() / "agents" / "bully-evaluator.md"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        rest = text[3:]
        end = rest.find("\n---")
        if end != -1:
            text = rest[end + 4 :]
    return text.lstrip("\n")


def count_tokens(payload: str | dict, *, system: str = "", use_api: bool = True) -> tuple[int, str]:
    """Count input tokens for the given bully-evaluator payload.

    `payload` may be a pre-formatted string (the new `_evaluator_input`
    shape) or a dict (legacy callers). Strings are sent as-is; dicts
    are JSON-serialized.

    Returns (token_count, method) where method is 'count_tokens' or 'proxy'.

    Uses the Anthropic `messages/count_tokens` endpoint when
    ANTHROPIC_API_KEY is set AND the anthropic SDK is importable AND
    use_api is True. Falls back to `len(content) + len(system)`.
    """
    content = payload if isinstance(payload, str) else json.dumps(payload)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    anthropic = import_anthropic() if use_api else None
    if use_api and api_key and anthropic is not None:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.count_tokens(
                model=BENCH_MODEL,
                system=system,
                messages=[{"role": "user", "content": content}],
            )
            return int(resp.input_tokens), "count_tokens"
        except Exception:
            # Any API failure -> proxy. Bench must not crash on transient errors.
            pass
    return len(content) + len(system), "proxy"


def full_dispatch(
    payload: str | dict, *, system: str, max_tokens: int = 1024
) -> tuple[int, int, str]:
    """Make a real `messages.create` call to capture input + output tokens.

    Returns (input_tokens, output_tokens, method) where method is 'full' on
    a real round-trip. Falls back to count_tokens for input and
    `output_tokens=0` if the API call can't be made.

    Real model cost is paid each call -- use sparingly (e.g. one bench run
    for calibration, not per-edit).
    """
    content = payload if isinstance(payload, str) else json.dumps(payload)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    anthropic = import_anthropic()
    if api_key and anthropic is not None:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=BENCH_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
            )
            return int(resp.usage.input_tokens), int(resp.usage.output_tokens), "full"
        except Exception:
            pass
    tokens, method = count_tokens(payload, system=system, use_api=True)
    return tokens, 0, method


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Rough USD cost using BENCH_*_PRICE_PER_MTOK constants."""
    return (
        input_tokens * BENCH_INPUT_PRICE_PER_MTOK / 1_000_000
        + output_tokens * BENCH_OUTPUT_PRICE_PER_MTOK / 1_000_000
    )
