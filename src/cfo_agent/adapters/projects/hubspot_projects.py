"""Active-project source: HubSpot deals with confirmed revenue in the close month.

Feeds the billable-tagging list a pal picks from (they tag a billable expense to
a PROJECT, not just a client). This is the upstream source the finance dashboard's
"Confirmed revenue" tab is itself built from — chosen because it's fully
automatable, whereas the dashboard tab can't be read by gid.

Definition of an active project for month M:
  dealstage == "Closed Won"  AND  service_start_date <= end(M)  AND  service_end_date >= start(M)

GOTCHA (verified): HubSpot's Accounts pipeline gives its "Procurement (Gain
Approval)" stage the *internal id* `closedwon`. True Closed Won is the stage
whose id is `1da62dec-16bb-491b-bd08-162539738ba4`.

BILLABLE stages (Erica, Aug 2026): a pal may tag a billable expense to a project
that's confirmed OR still in "Procurement / Gain Approval" — engagements in that
stage are already being delivered (pals incur billable travel for them), so Penny
accepts BOTH stages as billable projects. `BILLABLE_STAGE_IDS` is the set to use
for billable-tagging; `CLOSED_WON_STAGE_ID` alone is still the definition of
CONFIRMED REVENUE (don't use the broader set where you mean confirmed revenue).

Headless use needs a HubSpot private-app token (HUBSPOT_TOKEN) with crm.objects.
deals.read. In Cowork the list is refreshed via the connected HubSpot MCP and
cached to clients/<client>/active_projects.yaml, which the DM builder reads.
"""
from __future__ import annotations

CLOSED_WON_STAGE_ID = "1da62dec-16bb-491b-bd08-162539738ba4"
# "Procurement / Gain Approval" (Accounts pipeline) — reports internal id
# `closedwon` (the string), NOT true Closed Won.
GAIN_APPROVAL_STAGE_ID = "closedwon"
# Stages a billable expense may be tagged to (confirmed OR in gain-approval).
BILLABLE_STAGE_IDS = (CLOSED_WON_STAGE_ID, GAIN_APPROVAL_STAGE_ID)

# The query the recurring pull runs (SQL form used via the HubSpot connector):
QUERY_TEMPLATE = (
    "SELECT dealname, COMPANY.name, dealstage, service_start_date, service_end_date "
    "FROM DEAL WHERE service_start_date <= '{month_end}' "
    "AND service_end_date >= '{month_start}'"
)


def active_projects(rows: list, month_start: str, month_end: str) -> list:
    """Filter raw deal rows to confirmed projects active in the window, deduped
    by deal id (a deal associated to two companies appears twice)."""
    seen, out = set(), []
    for r in rows:
        if r.get("dealstage_id") not in BILLABLE_STAGE_IDS:
            continue
        did = r.get("deal_id")
        if did in seen:
            continue
        seen.add(did)
        out.append({"project": r["dealname"].strip(), "client": r.get("company", "")})
    return sorted(out, key=lambda p: (p["client"], p["project"]))
