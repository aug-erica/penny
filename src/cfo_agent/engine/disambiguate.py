"""Small, dependency-light helpers for "did you mean" disambiguation.

Shared by the reimbursement flow (category) and the card-reply flow (project):
when a value doesn't map cleanly to one item in a controlled list, we'd rather
offer a short NUMBERED shortlist the person picks from ("reply 2") than make them
recall and re-type the exact contract/category name. Pure functions — no I/O — so
they're trivially testable and safe to call from the money path.
"""
from __future__ import annotations

from typing import List, Optional


def fuzzy_one(raw: str, candidates: List[str], floor: int = 88) -> Optional[str]:
    """The single best candidate for `raw` if it clears `floor` AND is clearly
    ahead of the runner-up; else None. Used to accept a near-miss ("McCain WOMB"
    -> "McCain WOMB PACE") without guessing when several are close."""
    if not raw or not candidates:
        return None
    from rapidfuzz import process, fuzz
    ranked = process.extract(raw, candidates, scorer=fuzz.WRatio, limit=2)
    if not ranked:
        return None
    best_name, best_score, _ = ranked[0]
    if best_score < floor:
        return None
    if len(ranked) > 1 and (best_score - ranked[1][1]) < 8:
        return None   # too close to call — caller should offer a shortlist instead
    return best_name


def shortlist(raw: str, candidates: List[str], limit: int = 3,
              floor: int = 55) -> List[str]:
    """Up to `limit` closest candidates to `raw`, best first, each above `floor`.
    Used to build the numbered pick-list. Empty when nothing is close."""
    if not raw or not candidates:
        return []
    from rapidfuzz import process, fuzz
    ranked = process.extract(raw, candidates, scorer=fuzz.WRatio, limit=limit)
    return [name for name, score, _ in ranked if score >= floor]


def parse_choice(text: str) -> Optional[int]:
    """A reply that is *only* a selection ("2", "#2", "option 2", "number 3.") ->
    the 1-based number; else None. Kept strict so a normal sentence that merely
    contains a digit is never mistaken for a pick."""
    import re
    m = re.fullmatch(r"\s*(?:#|option|number|no\.?|opt)?\s*(\d{1,2})\s*[.!)]*\s*",
                     (text or ""), re.I)
    if not m:
        return None
    n = int(m.group(1))
    return n if n >= 1 else None
