"""Monkey-patch for twscrape 0.17.0 to handle the new x.com HTML bundle layout.

Background
----------
`twscrape.xclid.get_scripts_list` parses the inline webpack chunk map out of
`https://x.com/tesla` to find the `ondemand.s.<hash>a.js` script, which is
required to compute the `x-client-transaction-id` header.

As of April 2026 the HTML no longer contains the old token
``e=>e+"."+{...}[e]+"a.js"``. Instead the bundler now emits a *name* map
followed by a *hash* map, roughly::

    ...{<id>:"<chunk-name>",...}[e]||e)+"."+{<id>:"<hash>",...}[e]+"a.js"

Additionally some numeric keys use JavaScript exponent notation (``88e3:``)
which plain ``json.loads`` rejects, so we rewrite them to integer strings
before parsing.

This module replaces ``twscrape.xclid.get_scripts_list`` with a parser that
understands both formats and falls back to the legacy behaviour when the
name map is absent.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("JobSpy:Twitter")

_PATCHED = False

_NUM_KEY_RE = re.compile(r"(\{|,)(\d+(?:e\d+)?):")


def _quote_numeric_keys(js_object_literal: str) -> str:
    def _sub(m: re.Match[str]) -> str:
        prefix, key = m.group(1), m.group(2)
        try:
            return f'{prefix}"{int(float(key))}":'
        except ValueError:
            return m.group(0)

    return _NUM_KEY_RE.sub(_sub, js_object_literal)


def _extract_balanced_object(text: str, close_idx: int) -> str | None:
    """Walk back from ``close_idx`` (a ``}`` position) to its matching ``{`` and
    return the inclusive substring, or ``None`` if braces don't balance."""
    if close_idx < 0 or close_idx >= len(text) or text[close_idx] != "}":
        return None
    depth = 1
    i = close_idx - 1
    while i >= 0:
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                return text[i : close_idx + 1]
        i -= 1
    return None


def _patched_get_scripts_list(text: str):
    from twscrape.xclid import script_url  # local import to avoid circular

    tail_marker = '[e]+"a.js"'
    tail_idx = text.find(tail_marker)
    if tail_idx < 0:
        raise Exception("Failed to parse scripts: missing '[e]+\"a.js\"' marker")

    head = text[:tail_idx]
    last_plus = head.rfind('+"."+')
    if last_plus < 0:
        raise Exception("Failed to parse scripts: missing '+\".\"+' marker")
    hash_map_literal = head[last_plus + len('+"."+') :]

    try:
        hash_obj = json.loads(_quote_numeric_keys(hash_map_literal))
    except json.JSONDecodeError as e:
        raise Exception("Failed to parse scripts hash map") from e

    # Optional chunk-id -> name map (present in the 2026+ format).
    name_obj: dict[str, str] | None = None
    name_marker = '}[e]||e)+"."+'
    name_idx = text.find(name_marker)
    if name_idx >= 0:
        name_literal = _extract_balanced_object(text, name_idx)
        if name_literal is not None:
            try:
                name_obj = json.loads(_quote_numeric_keys(name_literal))
            except json.JSONDecodeError:
                name_obj = None

    for chunk_id, chunk_hash in hash_obj.items():
        name = name_obj.get(chunk_id, chunk_id) if name_obj else chunk_id
        yield script_url(name, f"{chunk_hash}a")


def apply() -> None:
    """Install the patched ``get_scripts_list`` into ``twscrape.xclid``.

    Safe to call multiple times; subsequent calls are no-ops.
    """
    global _PATCHED
    if _PATCHED:
        return

    try:
        from twscrape import xclid as _xclid  # type: ignore
    except Exception as e:  # pragma: no cover - twscrape not installed
        log.warning("twscrape not installed; patch skipped (%s)", e)
        return

    _xclid.get_scripts_list = _patched_get_scripts_list  # type: ignore[assignment]
    _PATCHED = True
    log.debug("Applied twscrape.xclid.get_scripts_list patch")
