"""Disambiguation helpers: fuzzy near-match acceptance, numbered pick parsing,
and shortlist building — the primitives behind "reply with a number" and the
card-side "accept a near-miss project instead of dropping it"."""
from cfo_agent.engine import disambiguate as dz
from cfo_agent.engine import reply_parse

PROJECTS = [
    "McCain WOMB PACE", "McCain Jan 2026 Extension",
    "Genentech L&SD Sprint", "Marriott Gear Up Design",
]


def test_fuzzy_one_accepts_confident_nearmiss():
    # A near-miss that clearly points at one project is accepted.
    assert dz.fuzzy_one("McCain WOMB", PROJECTS) == "McCain WOMB PACE"
    assert dz.fuzzy_one("Genentech LSD Sprint", PROJECTS) == "Genentech L&SD Sprint"


def test_fuzzy_one_declines_ambiguous_or_weak():
    # "McCain" alone is ambiguous across two McCain projects -> don't guess.
    assert dz.fuzzy_one("McCain", PROJECTS) is None
    # Nothing close -> None (never invent).
    assert dz.fuzzy_one("Spotify subscription", PROJECTS) is None


def test_parse_choice():
    assert dz.parse_choice("2") == 2
    assert dz.parse_choice("#3") == 3
    assert dz.parse_choice("option 1") == 1
    assert dz.parse_choice(" 2. ") == 2
    # A normal sentence that merely contains a digit is NOT a pick.
    assert dz.parse_choice("it was the $2 coffee") is None
    assert dz.parse_choice("Groceries & Meals") is None
    assert dz.parse_choice("") is None


def test_shortlist_orders_and_floors():
    sl = dz.shortlist("McCain", PROJECTS, limit=3)
    assert sl and all("McCain" in s for s in sl)   # both McCain projects surface
    assert dz.shortlist("zzz nonsense", PROJECTS) == []


def test_interpret_reply_fuzzy_project_without_llm():
    # No API key -> interpret_reply returns [] (LLM path). This guards the import
    # wiring of disambiguate inside reply_parse rather than the LLM itself.
    assert reply_parse.interpret_reply("august", "", [], PROJECTS) == []
