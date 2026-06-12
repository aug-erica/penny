"""Apply the per-client ruleset (rules.yaml). Exact merchant rules beat
substring/keyword rules beat cardholder defaults."""
from __future__ import annotations

from typing import Optional

from ...models import Proposal


def propose(rules: dict, line: dict) -> Optional[Proposal]:
    merchant = line["merchant_norm"]
    for r in rules.get("merchant_rules", []) or []:
        match, kind = r["match"].upper(), r.get("kind", "exact")
        hit = merchant == match if kind == "exact" else match in merchant
        if hit:
            return Proposal(
                coa_line=r["coa_line"], proposed_by="rule",
                confidence="high" if kind == "exact" else "medium",
                rationale=f"{kind} rule: {r['match']} -> {r['coa_line']}",
                billable=r.get("billable"),
            )
    for r in rules.get("keyword_rules", []) or []:
        if r["match"].upper() in line["merchant_raw"].upper():
            return Proposal(
                coa_line=r["coa_line"], proposed_by="rule", confidence="medium",
                rationale=f"keyword rule: {r['match']} -> {r['coa_line']}",
                billable=r.get("billable"),
            )
    default = (rules.get("cardholder_defaults") or {}).get(line.get("cardholder") or "")
    if default:
        return Proposal(coa_line=default, proposed_by="rule", confidence="low",
                        rationale=f"cardholder default for {line.get('cardholder')}")
    return None
