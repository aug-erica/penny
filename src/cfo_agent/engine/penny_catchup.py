"""Catch up on DMs Penny missed while offline.

Socket Mode does NOT redeliver events that arrived while the app was
disconnected, so a listener that was down (crash, restart, weekend) silently
loses every message sent in the gap. This scans each pal's DM and processes any
message that arrived AFTER Penny's last reply — making a restart self-healing.

Idempotency: "after Penny's last reply in that DM" is the guard. Anything Penny
already answered sits before her reply timestamp and is skipped, so re-running is
safe and won't double-file receipts.
"""
from __future__ import annotations

from . import ledger, reply_flow


def _pending_for_channel(penny, channel: str, me: str, seen: set) -> list:
    """Pal messages in this DM that Penny hasn't already processed.

    Dedupe is by stored message ts (not "newer than Penny's last message"):
    Penny's confirm-backs are threaded, so they don't appear in history and can't
    be used as a watermark.
    """
    r = penny._get("conversations.history", channel=channel, limit=100)
    msgs = list(reversed(r.get("messages", [])))          # oldest -> newest
    return [m for m in msgs
            if not m.get("bot_id") and m.get("user") != me
            and m.get("ts") not in seen
            and (not m.get("subtype") or m.get("subtype") == "file_share")]


def catch_up(cfg, month: str, penny, db_path, post: bool = True, log=print) -> list:
    """Process every pal's un-answered DMs. Returns [(cardholder, n_msgs), ...]."""
    slack_users = cfg.raw.get("bot", {}).get("slack_users", {})
    me = penny.auth_test()["user_id"]
    conn = ledger.open_db(db_path)
    seen = ledger.processed_dm_ts(conn)
    handled = []
    for uid, cardholder in slack_users.items():
        if uid == me:
            continue
        try:
            channel = penny.open_dm(uid)
        except Exception:
            continue
        pending = _pending_for_channel(penny, channel, me, seen)
        if not pending:
            continue
        log(f"[catchup] {cardholder}: {len(pending)} unanswered message(s)")
        handled.append((cardholder, len(pending)))
        if not post:
            continue   # preview only — no ledger writes, no receipt downloads
        res, last_ts = None, None
        for m in pending:
            fids = [f["id"] for f in m.get("files", []) if f.get("id")]
            res = reply_flow.process_pal_reply(
                conn, cfg, cardholder, m.get("text", "") or "", month,
                slack=penny, file_ids=fids)
            last_ts = m.get("ts")
            ledger.mark_dm_processed(conn, m.get("ts"), cardholder)
        # One confirm-back per pal, threaded under their most recent message
        # (the confirm-back is recomputed from full ledger state each call).
        if res and last_ts:
            penny.send_dm(uid, res["confirm_back"], thread_ts=last_ts)
    return handled
