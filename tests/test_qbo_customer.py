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


# --- parent resolution + sub-customer provisioning (solutions ②③) -----------
PPFA_CUSTS = [
    {"Id": "P", "DisplayName": "Planned Parenthood Federation of America",
     "FullyQualifiedName": "Planned Parenthood Federation of America"},
    {"Id": "S1", "DisplayName": "OOP Org Design and Change Management",
     "FullyQualifiedName": "Planned Parenthood Federation of America:OOP Org Design and Change Management"},
]
ALIASES = {"PPFA": "Planned Parenthood Federation of America"}


def test_resolve_parent_uses_alias():
    # HubSpot acronym "PPFA" -> QBO legal-name parent.
    assert w.resolve_parent(None, "PPFA", customers=PPFA_CUSTS, aliases=ALIASES)[0] == "P"


def test_ensure_subcustomer_missing_without_create():
    # The FY27 engagement isn't in QBO yet and create=False -> missing.
    cid, action = w.ensure_subcustomer(
        None, "P", "Planned Parenthood Federation of America",
        "OOP FY27 Advisory, Design and Facilitation", PPFA_CUSTS, create=False)
    assert cid is None and action == "missing"


def test_ensure_subcustomer_creates_when_allowed(monkeypatch):
    monkeypatch.setattr(w, "create_customer", lambda q, name, parent_id=None: {"Id": "NEW"})
    cid, action = w.ensure_subcustomer(
        None, "P", "Planned Parenthood Federation of America",
        "OOP FY27 Advisory, Design and Facilitation", PPFA_CUSTS, create=True)
    assert cid == "NEW" and action == "created"


def test_ensure_subcustomer_matches_existing():
    cid, action = w.ensure_subcustomer(
        None, "P", "Planned Parenthood Federation of America",
        "OOP Org Design and Change Management", PPFA_CUSTS, create=False)
    assert cid == "S1" and action == "matched"


def test_proj_norm():
    assert w.proj_norm("PPFA OOP FY27 Advisory, Design & Facilitation") == \
        "ppfa oop fy27 advisory design facilitation"
