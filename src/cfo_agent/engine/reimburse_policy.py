"""Reimbursement policy checks — August's documented per-category caps + the
90-day submission rule (Employment Benefits doc, Aug 2025). Pure functions: given
a reimbursement's amount / purpose / category / date, return any policy
violations. Penny uses these to PUSH BACK (warn the employee + flag for the
reviewer) — never a hard block, because the policy has legitimate exceptions
(e.g. international-travel cell costs are covered beyond $100), so a human decides.

Config-driven: the caps live in clients/<client>/client.yaml under
reimbursements.policy, so Finance can adjust them without a code change.
"""
from __future__ import annotations

from datetime import date


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _policy(cfg) -> dict:
    return cfg.section("reimbursements").get("policy", {}) or {}


def _matches(item: dict, purpose_l: str, coa: str) -> bool:
    kws = [k.lower() for k in item.get("keywords", [])]
    if kws and any(k in purpose_l for k in kws):
        return True
    c = item.get("coa")
    if c and coa and c == coa and not kws:
        return True   # coa-only rule (e.g. Professional Development)
    return False


def check(cfg, amount_cents, business_purpose, coa,
          expense_date: str = None, today: str = None) -> list:
    """Return a list of violation dicts (empty if in policy). Kinds:
      {"kind":"limit","item","limit_cents","amount_cents","period","message"}
      {"kind":"stale","age_days","max_days","message"}
    """
    pol = _policy(cfg)
    out = []
    purpose_l = (business_purpose or "").lower()

    # Amount caps — first matching limit item wins (order the config specific->general).
    for item in pol.get("limits", []) or []:
        if _matches(item, purpose_l, coa):
            lim = item.get("limit_cents")
            if lim is not None and amount_cents and amount_cents > lim:
                out.append({
                    "kind": "limit", "item": item.get("name", "this expense"),
                    "limit_cents": lim, "amount_cents": amount_cents,
                    "period": item.get("period", "month"),
                    "message": (f"August reimburses {item.get('name','this')} up to "
                                f"{_money(lim)}/{item.get('period','month')}, and this "
                                f"is {_money(amount_cents)}")})
            break   # matched a rule (over or within) — don't also match a looser one

    # Timeliness — submit within N days of purchase (accountable-plan / general policy).
    max_days = pol.get("max_age_days")
    if max_days and expense_date and today:
        try:
            age = (date.fromisoformat(today) - date.fromisoformat(expense_date)).days
            if age > int(max_days):
                out.append({
                    "kind": "stale", "age_days": age, "max_days": int(max_days),
                    "message": (f"this was {age} days ago — our policy is to submit "
                                f"within {max_days} days of purchase")})
        except ValueError:
            pass
    return out
