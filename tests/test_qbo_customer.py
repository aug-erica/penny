"""Parent-scoped QBO customer matching — HubSpot deal names drift from the QBO
sub-customer names, so we pin the parent (client) then match the sub within it."""
from cfo_agent.adapters.books_out import qbo_writer as w


def _c(cid, parent, sub):
    return {"Id": cid, "DisplayName": sub, "FullyQualifiedName": f"{parent}:{sub}"}


CUSTS = [
    _c("1", "PPFA", "OOP Advisory Retainer FY27"),       # drifted from the HubSpot name
    _c("2", "PPFA", "Cultural Transformation Roadmap"),
    _c("3", "Genentech", "L&SD Sprint"),
    _c("4", "Genentech", "External Affairs Leadership Forum"),
    _c("5", "Waymo", "People Development & Way of Hiring Support"),
]


def test_containment_when_sub_is_deal_minus_prefix():
    # QBO sub == HubSpot deal name minus the "Genentech " client prefix.
    assert w.resolve_customer(None, "Genentech L&SD Sprint", customers=CUSTS) == "3"


def test_parent_scoped_fuzzy_across_drift():
    # HubSpot deal "PPFA OOP FY27 Advisory, Design and Facilitation" vs QBO
    # "OOP Advisory Retainer FY27" — resolves within the PPFA parent, not to
    # PPFA's Cultural Transformation.
    got = w.resolve_customer(None, "PPFA OOP FY27 Advisory, Design and Facilitation",
                             customers=CUSTS)
    assert got == "1"


def test_scopes_to_the_right_client():
    # A Genentech project never resolves to a PPFA sub-customer.
    assert w.resolve_customer(None, "Genentech External Affairs Leadership Forum",
                              customers=CUSTS) == "4"


def test_no_confident_match_returns_none():
    assert w.resolve_customer(None, "Some Unrelated Vendor LLC", customers=CUSTS) is None


def test_explicit_client_hint_pins_parent():
    # When we DO know the client (e.g. from HubSpot's associated company), pass it.
    assert w.resolve_customer(None, "OOP FY27 Advisory", customers=CUSTS,
                              client="PPFA") == "1"
