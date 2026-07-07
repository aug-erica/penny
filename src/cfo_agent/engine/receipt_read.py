"""Read a receipt file (PDF or image) with Claude to extract merchant + total.

This is what lets Penny link *any* receipt to its charge — including phone photos
and scanned PDFs with no text layer, which plain text extraction can't read (the
gap that made Penny keep re-asking for receipts pals had already sent). On any
failure it returns {} and the caller falls back to text extraction / sole-need.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from ..config import env

MODEL = "claude-haiku-4-5-20251001"
_PROMPT = ('This is an expense receipt or invoice. Return ONLY JSON: '
           '{"merchant":"<store/vendor name, or null>","total_usd":<final total '
           'actually paid as a number, or null>}. The total is the grand total '
           'including tax/tip, not a subtotal or line item.')


def _media(raw: bytes):
    """(media_type, content-block-kind) from magic bytes, or (None, None)."""
    if raw[:4] == b"%PDF":
        return "application/pdf", "document"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "image"
    if raw[:4] == b"\x89PNG":
        return "image/png", "image"
    if raw[:4] == b"GIF8":
        return "image/gif", "image"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", "image"
    return None, None


def read_receipt(path) -> dict:
    """{"merchant": str|None, "amount_cents": int|None} — or {} if unreadable."""
    api_key = env("ANTHROPIC_API_KEY")
    if not api_key:
        return {}
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return {}
    media_type, kind = _media(raw)
    if not media_type:
        return {}  # unsupported type (e.g. HEIC) — caller falls back
    import anthropic
    block = {"type": kind, "source": {"type": "base64",
             "media_type": media_type, "data": base64.standard_b64encode(raw).decode()}}
    try:
        msg = anthropic.Anthropic(api_key=api_key).messages.create(
            model=MODEL, max_tokens=200,
            messages=[{"role": "user", "content": [block, {"type": "text", "text": _PROMPT}]}])
        text = msg.content[0].text
        d = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception:
        return {}
    total = d.get("total_usd")
    return {"merchant": d.get("merchant") or None,
            "amount_cents": round(total * 100) if isinstance(total, (int, float)) and total else None}
