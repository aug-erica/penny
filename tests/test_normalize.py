"""Slack mrkdwn normalization — formatting must not break Penny's parsing
(Levi's italicised message that silently didn't record)."""
from cfo_agent.adapters.penny_listener import normalize_slack_text as n


def test_strips_italic_and_bold():
    assert n("_reimburse $50 for team lunch_") == "reimburse $50 for team lunch"
    assert n("*reimburse* $40 lunch") == "reimburse $40 lunch"
    assert n("~old~ `note`") == "old note"


def test_unwraps_links_and_mentions():
    assert n("reimburse $42 <https://x.com/r|receipt>") == "reimburse $42 receipt"
    assert n("bill it to <@U123|Sam>") == "bill it to Sam"
    assert n("see <https://x.com>") == "see https://x.com"


def test_unescapes_html_entities():
    # Slack sends & as &amp; — unescape so category names like "Telephone & Internet" match.
    assert n("reimburse $50 home internet &amp; phone") == \
        "reimburse $50 home internet & phone"


def test_plain_text_untouched():
    assert n("reimburse $42 for lunch with the McCain team") == \
        "reimburse $42 for lunch with the McCain team"
