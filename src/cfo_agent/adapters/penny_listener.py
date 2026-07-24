"""Penny's live listener (Socket Mode). Connects to Slack with the app token,
and when a pal DMs Penny, processes the reply end-to-end: applies their
categorization/billable/project + recategorizations, downloads & files any
receipt, and replies with a confirm-back — all autonomously.
"""
from __future__ import annotations

import calendar
import re
import sys
import threading
import time
import traceback
from datetime import date
from pathlib import Path
from threading import Event

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from ..config import RUNS_LOCAL, env, load_client
from ..engine import continuous_close as cc_mod
from ..engine import digest as digest_mod
from ..engine import kv, ledger, penny_catchup, reimburse_flow, reply_flow
from .slack_client import PennySlack

# Daily digest posts around this hour UTC (~9am ET). The scheduler checks a few
# times an hour and posts once/day during the first week (guarded in the DB).
_DIGEST_HOUR_UTC = 13

# Admin command: "start the 2026-07 close" / "switch to July close". Kept tight
# (must end with the word "close") so a normal expense reply is never hijacked.
_MONTH_SWITCH = re.compile(
    r"^\s*(?:penny[,:\s]+)?(?:start|switch\s+to|set)\s+(?:the\s+)?(.+?)\s+close\s*[.!]*\s*$",
    re.I)


def _parse_month(s: str):
    """'2026-07', 'July', or 'July 2026' -> 'YYYY-MM' (None if unparseable).
    A bare month name assumes the current year, rolling to next year when the
    named month is more than one month in the past (year boundary)."""
    s = s.strip().lower()
    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m:
        return s if 1 <= int(m.group(2)) <= 12 else None
    months = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
    parts = s.split()
    if not parts or parts[0] not in months:
        return None
    mo = months[parts[0]]
    if len(parts) == 2 and parts[1].isdigit():
        return f"{int(parts[1]):04d}-{mo:02d}"
    if len(parts) > 1:
        return None
    today = date.today()
    yr = today.year + (1 if mo < today.month - 1 else 0)
    return f"{yr:04d}-{mo:02d}"


def _log(msg):
    print(msg, flush=True)


def run(client_name: str, month: str):
    cfg = load_client(client_name)
    db_path = RUNS_LOCAL / cfg.client / "ledger.sqlite3"
    bot = cfg.raw.get("bot", {})
    slack_users = bot.get("slack_users", {})
    admins = set(bot.get("admins", []))
    penny = PennySlack()
    me = penny.auth_test()["user_id"]

    def active_month():
        """Resolve the working month PER EVENT (not once at startup): an admin's
        explicit choice (DB) wins; otherwise the CURRENT calendar month — because
        charges post and replies come in for the month happening now (the
        continuous close), not a static CLOSE_MONTH env that goes stale."""
        import datetime as _dt
        return kv.active_month(cfg.client, _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m"))

    def _refers_to(cardholder: str, tl: str) -> bool:
        """Does this message clearly refer to `cardholder` as a person? Requires a
        STRONG signal — full name, or an explicit 'for X' / \"X's\" / '@X' / leading
        'X:' cue — so ordinary words that happen to match a surname don't count
        (e.g. 'Black' in 'Black Bull' must NOT resolve to Alexis Black)."""
        parts = [p for p in re.split(r"[^a-z]+", cardholder.lower()) if len(p) > 1]
        if not parts:
            return False
        first, last = parts[0], parts[-1]

        def w(s):   # whole-word present
            return re.search(r"\b" + re.escape(s) + r"\b", tl) is not None

        if len(parts) >= 2 and w(first) and w(last):         # full name
            return True
        for n in {first, last}:
            if len(n) < 3:
                continue
            if (f"for {n}" in tl or f"{n}'s" in tl or f"{n}’s" in tl
                    or f"on behalf of {n}" in tl or f"@{n}" in tl
                    or re.match(r"\s*" + re.escape(n) + r"\s*[:,\-—]", tl)):
                return True
        return False

    def resolve_cardholder(sender_uid, text):
        """Sender's own DM = their charges. An admin can file on another pal's
        behalf, but ONLY when the message clearly names that pal (see _refers_to);
        otherwise it's about the admin's own charges."""
        sender = slack_users.get(sender_uid)
        if sender_uid in admins and text:
            tl = text.lower()
            named = {c for c in slack_users.values()
                     if c != sender and _refers_to(c, tl)}
            if len(named) == 1:
                return named.pop()
        return sender

    sm = SocketModeClient(app_token=env("SLACK_APP_TOKEN"),
                          web_client=WebClient(token=env("SLACK_BOT_TOKEN")))

    def handle(smc: SocketModeClient, req: SocketModeRequest):
        if req.type != "events_api":
            return
        smc.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        e = req.payload.get("event", {})

        # @Penny in a channel (e.g. the #finance digest thread): don't go silent,
        # but never dump expense detail publicly — redirect the person to their DM.
        if e.get("type") == "app_mention" and e.get("user") != me:
            uid = e.get("user")
            dm_ok = uid in slack_users
            redirect = (f"<@{uid}> " + ("let's keep the details in our DM — "
                        "reply to me there to confirm categories or send receipts. "
                        "I've got your latest either way. 🙏" if dm_ok else
                        "I only handle August-card expenses in DMs for the folks with a "
                        "card. Ping Erica or Purvi if you need something here."))
            try:
                penny.send_dm  # noqa: available; use raw post to the channel/thread
                penny._post("chat.postMessage", channel=e.get("channel"),
                            thread_ts=e.get("thread_ts") or e.get("ts"), text=redirect)
                _log(f"[mention] redirected {uid} to DM")
            except Exception:
                _log(f"[error] mention reply: {traceback.format_exc()}")
            return

        subtype = e.get("subtype")
        # Process normal DMs and file uploads (subtype 'file_share'); skip edits,
        # joins, and other subtypes, bot messages, and Penny's own messages.
        if (e.get("type") != "message" or e.get("channel_type") != "im"
                or e.get("bot_id") or e.get("user") == me
                or (subtype and subtype != "file_share")):
            return
        uid = e.get("user")
        text = e.get("text", "") or ""

        # Admin month switch: "start the 2026-07 close" / "switch to July close".
        if uid in admins:
            sw = _MONTH_SWITCH.match(text)
            if sw:
                conn = ledger.open_db(db_path)
                parsed = _parse_month(sw.group(1))
                try:
                    if parsed:
                        kv.set(f"close_month:{cfg.client}", parsed)
                        penny.send_dm(uid, f"📅 Working the {parsed} close now.",
                                      thread_ts=e.get("ts"))
                        _log(f"[admin] close month -> {parsed} (by {slack_users.get(uid)})")
                    else:
                        penny.send_dm(uid, f'I couldn\'t read "{sw.group(1)}" as a month — '
                                      'try "start the 2026-07 close".', thread_ts=e.get("ts"))
                finally:
                    ledger.mark_dm_processed(conn, e.get("ts"), slack_users.get(uid))
                return

        # Reimbursements. A DM starting with the trigger word ("reimburse") is a NEW
        # out-of-pocket expense. A reply IN an existing reimbursement's thread needs
        # no trigger word — Penny remembers that charge and fills in what's new (so
        # nobody re-sends or re-uploads a receipt). The card flow stays untouched:
        # only trigger-word DMs and replies under a reimbursement thread are caught.
        rc = cfg.section("reimbursements")
        trigger = (rc.get("trigger") or "reimburse").lower()
        starts_trigger = bool(rc.get("enabled")) and text.strip().lower().startswith(trigger)
        employee = slack_users.get(uid)
        participants = set(rc.get("participants", []))
        thread_ts = e.get("thread_ts")

        # A non-participant tried the trigger word -> pilot notice (don't card-route it).
        if starts_trigger and (not employee or employee not in participants):
            conn = ledger.open_db(db_path)
            penny.send_dm(uid, "Reimbursements through Penny are in a limited pilot "
                          "right now — please keep using Expensify for now. (Ping Erica "
                          "to join the pilot.)", thread_ts=e.get("ts"))
            ledger.mark_dm_processed(conn, e.get("ts"), employee)
            return

        if rc.get("enabled") and employee in participants and (starts_trigger or thread_ts):
            conn = ledger.open_db(db_path)
            followup_ext = f"reimb-{thread_ts}" if thread_ts else None
            is_followup = bool(not starts_trigger and followup_ext
                               and ledger.reimbursement_by_external_id(
                                   conn, cfg.client, followup_ext))
            if starts_trigger or is_followup:
                if not ledger.claim_dm(conn, e.get("ts"), employee):
                    _log(f"[skip] {employee}: reimbursement msg already claimed (dup)")
                    return
                file_ids = [f["id"] for f in e.get("files", []) if f.get("id")]
                try:
                    if starts_trigger:
                        _log(f"[reimburse] {employee} intake: {text[:60]!r} "
                             f"+ {len(file_ids)} file(s)")
                        res = reimburse_flow.process_intake(
                            conn, cfg, employee, uid, text, active_month(),
                            slack=penny, file_ids=file_ids, source_dm_ts=e.get("ts"))
                        penny.send_dm(uid, res["confirm_back"], thread_ts=e.get("ts"))
                    else:
                        _log(f"[reimburse] {employee} follow-up in {followup_ext}: "
                             f"{text[:50]!r} + {len(file_ids)} file(s)")
                        res = reimburse_flow.process_followup(
                            conn, cfg, employee, uid, text, active_month(),
                            slack=penny, file_ids=file_ids, thread_ts=thread_ts)
                        penny.send_dm(uid, res["confirm_back"], thread_ts=thread_ts)
                    _log(f"[reimburse] {employee}: {res['status']}")
                except Exception:
                    ledger.unclaim_dm(conn, e.get("ts"))
                    _log(f"[error] reimbursement {employee}: {traceback.format_exc()}")
                return
            # participant replied in a NON-reimbursement thread -> card path below

        cardholder = resolve_cardholder(uid, text)
        if not cardholder:
            _log(f"[skip] DM from unmapped user {uid}")
            return
        file_ids = [f["id"] for f in e.get("files", []) if f.get("id")]
        on_behalf = " (on behalf, by admin)" if slack_users.get(uid) != cardholder else ""
        _log(f"[reply] {cardholder}{on_behalf}: {text[:70]!r} + {len(file_ids)} file(s)")
        conn = ledger.open_db(db_path)   # fresh connection on this worker thread
        if not ledger.claim_dm(conn, e.get("ts"), cardholder):
            _log(f"[skip] {cardholder}: message already claimed (dup event)")
            return
        try:
            res = reply_flow.process_pal_reply(conn, cfg, cardholder, text, active_month(),
                                               slack=penny, file_ids=file_ids)
            penny.send_dm(uid, res["confirm_back"], thread_ts=e.get("ts"))
            _log(f"[done] {cardholder}: {res['decisions']} billable/project, "
                 f"{res['recats']} recat, {res['receipts']} receipt(s); confirm-back sent")
        except Exception:
            ledger.unclaim_dm(conn, e.get("ts"))   # allow a retry
            _log(f"[error] processing {cardholder}: {traceback.format_exc()}")

    # Self-heal: process any DMs that arrived while Penny was offline (Socket Mode
    # never redelivers those). Runs before we start listening for new events.
    try:
        done = penny_catchup.catch_up(cfg, active_month(), penny, db_path, post=True, log=_log)
        if done:
            _log(f"[catchup] handled {sum(n for _, n in done)} missed message(s) "
                 f"across {len(done)} pal(s): {', '.join(p for p, _ in done)}")
        else:
            _log("[catchup] no missed messages")
    except Exception:
        _log(f"[error] catch-up on startup: {traceback.format_exc()}")

    # Daily #finance digest — a background thread posts it once/day during the
    # first week (idempotent via the DB guard, so restarts never double-post).
    def digest_loop():
        import datetime as _dt
        while True:
            try:
                if _dt.datetime.now(_dt.timezone.utc).hour >= _DIGEST_HOUR_UTC:
                    conn = ledger.open_db(db_path)
                    if digest_mod.post_if_due(conn, cfg, active_month(), penny) == "posted":
                        _log("[digest] posted daily digest to #finance")
            except Exception:
                _log(f"[digest] error: {traceback.format_exc()}")
            time.sleep(1800)   # re-check every 30 min

    threading.Thread(target=digest_loop, daemon=True).start()

    # Continuous close: poll QBO for newly-posted charges (enrolled cardholders),
    # categorize + book + DM them as they happen — a steady drip, not a month-end
    # burst. Uses the current calendar month (that's when new charges post).
    cc = cfg.raw.get("continuous_close", {})
    if cc.get("cardholders"):
        interval = int(cc.get("poll_hours", 4)) * 3600

        def continuous_loop():
            import datetime as _dt
            while True:
                try:
                    cur_month = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m")
                    conn = ledger.open_db(db_path)
                    res = cc_mod.run_once(conn, cfg, penny, cur_month, post=True, log=_log)
                    if res.get("new"):
                        _log(f"[continuous] {res['new']} new charge(s) booked + DM'd: {res.get('pals')}")
                except Exception:
                    _log(f"[continuous] error: {traceback.format_exc()}")
                time.sleep(interval)

        threading.Thread(target=continuous_loop, daemon=True).start()
        _log(f"[continuous] rolling close enabled for {cc['cardholders']} "
             f"(every {cc.get('poll_hours', 4)}h)")

    sm.socket_mode_request_listeners.append(handle)
    sm.connect()
    _log(f"Penny listening (Socket Mode) for {client_name}, close month "
         f"{active_month()} — bot {me}. Ctrl-C to stop.")
    Event().wait()
