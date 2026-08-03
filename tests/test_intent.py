"""Intent routing: a pal-initiated DM is a reimbursement by default, card-charge
answers are detected, and truly ambiguous messages ask (no LLM in tests)."""
import pytest

from cfo_agent.engine import intent

PROJECTS = ["McCain WOMB PACE", "Genentech L&SD Sprint", "Marriott Gear Up Design"]


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    # Without a key the LLM tiebreaker is skipped -> cheap signals only, so these
    # assertions are deterministic. (In prod the LLM breaks the remaining ties.)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_typed_amount_is_reimbursement():
    assert intent.classify("august", "$42 team lunch", False, False, PROJECTS) == "reimbursement"
    assert intent.classify("august", "reimburse me 42.50 for coffee", False, False, PROJECTS) == "reimbursement"


def test_card_answer_signals():
    assert intent.classify("august", "the Anthropic charge is not billable", False, False, PROJECTS) == "card"
    assert intent.classify("august", "that was for McCain WOMB PACE", False, False, PROJECTS) == "card"
    assert intent.classify("august", "bill it to Genentech", False, False, PROJECTS) == "card"


def test_bare_receipt_depends_on_pending():
    # Bare receipt, no pending card receipts -> a new expense's receipt.
    assert intent.classify("august", "here's the receipt", True, False, PROJECTS) == "reimbursement"
    # Bare receipt while the pal owes card receipts -> ambiguous, ask.
    assert intent.classify("august", "here's the receipt", True, True, PROJECTS) == "ambiguous"


def test_wordless_no_signal_is_ambiguous_without_llm():
    assert intent.classify("august", "thanks!", False, False, PROJECTS) == "ambiguous"


def test_reply_matching_existing_charge_is_card_not_reimbursement():
    # A reply that quotes an existing card charge's amount ("$38.39 is billable")
    # is answering Penny about THAT charge — not a new out-of-pocket expense.
    charges = [3839, 1120]  # cents of the pal's known card charges
    assert intent.classify("august", "07-28 LYFT *AIRPORT 07-29 $38.39 is billable",
                           False, False, PROJECTS, charge_amounts=charges) == "card"
    # A typed amount that does NOT match a known charge is still a reimbursement.
    assert intent.classify("august", "$42 team lunch", False, False, PROJECTS,
                           charge_amounts=charges) == "reimbursement"


def test_interpret_answer():
    assert intent.interpret_answer("reimbursement") == "reimbursement"
    assert intent.interpret_answer("out of pocket, I paid") == "reimbursement"
    assert intent.interpret_answer("it was on my card") == "card"
    assert intent.interpret_answer("card") == "card"
    assert intent.interpret_answer("not sure") is None
    assert intent.interpret_answer("") is None
