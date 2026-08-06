"""Penny's live listener (Socket Mode). Connects to Slack with the app token,
and when a pal DMs Penny, processes the reply end-to-end: applies their
categorization/billable/project + recategorizations, downloads & files any
receipt, and replies with a confirm-back — all autonomously.
"""
from __future__ import annotations

import calendar
import json
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
from ..engine import (intent, kv, ledger, penny_catchup, receipts, reimburse_dm,
                      reimburse_flow, reply_flow)
from .slack_client import PennySlack


def _load_pending(key: str, now_ts: str):
    """The pal's stashed pre-clarifier message (text + file ids), or None if there
    isn't one or it's stale (>1h). `now_ts`/stored ts are Slack epoch-second ts."""
    raw = kv.get(key)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not d or not d.get("ts"):
        return None
    try:
        if float(now_ts) - float(d["ts"]) > 3600:
            return None
    except (TypeError, ValueError):
        pass
    return d


def normalize_slack_text(text: str) -> str:
    """Strip Slack mrkdwn so formatting never breaks Penny's parsing. A pal who
    italicises their message sends '_reimburse $50_' — the leading '_' defeated the
    trigger/keyword checks and Penny silently dropped it (Levi's bug). Unwrap
    links/mentions to their labels and remove the *_~` wrappers."""
    import html
    t = html.unescape(text or "")                        # &amp;->&  &lt;-><  &gt;->>
    t = re.sub(r"<([@#!][^>|]+)\|([^>]+)>", r"\2", t)   # <@U123|name>/<#C|name> -> name
    t = re.sub(r"<[@#!][^>]+>", " ", t)                  # bare <@U123> mention -> space
    t = re.sub(r"<([^>|]+)\|([^>]+)>", r"\2", t)          # <url|label> -> label
    t = re.sub(r"<([^>]+)>", r"\1", t)                    # <url> -> url
    t = re.sub(r"[*_~`]", "", t)                          # bold/italic/strike/code wrappers
    return re.sub(r"[ \t]{2,}", " ", t).strip()


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
        # Normalize Slack formatting up front so italic/bold/code never breaks the
        # trigger, intent, amount, or category parsing downstream.
        text = normalize_slack_text(e.get("text", "") or "")

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

        # Reimbursement vs card routing. Penny initiates ALL card-charge
        # conversations (the continuous-close poller DMs pals about QBO-feed
        # charges), so a pal who STARTS a conversation is almost always filing an
        # out-of-pocket reimbursement. So the "reimburse" trigger word is no longer
        # required: a pal-initiated top-level DM defaults to a reimbursement (an
        # explicit trigger still forces one), thread replies route by their thread,
        # and a genuinely ambiguous message gets a one-line clarifier whose answer
        # we resolve against the stashed original (no resending / re-uploading).
        rc = cfg.section("reimbursements")
        enabled = bool(rc.get("enabled"))
        trigger = (rc.get("trigger") or "reimburse").lower()
        starts_trigger = enabled and text.strip().lower().startswith(trigger)
        employee = slack_users.get(uid)
        # Open to everyone (open_to_all) = any mapped teammate can submit; else the
        # explicit participants allow-list.
        participants = (set(slack_users.values()) if rc.get("open_to_all")
                        else set(rc.get("participants", [])))
        can_reimburse = enabled and bool(employee) and employee in participants
        thread_ts = e.get("thread_ts")
        msg_ts = e.get("ts")
        file_ids = [f["id"] for f in e.get("files", []) if f.get("id")]
        pending_key = f"pending_intent:{cfg.client}:{uid}"

        # Someone we can't map/authorize tried the trigger word — we need an
        # employee identity (for the vendor/booking), so ask them to get added.
        if starts_trigger and not can_reimburse:
            conn = ledger.open_db(db_path)
            msg = ("I don't have you mapped to an employee yet, so I can't file a "
                   "reimbursement for you — ping Erica to get set up." if not employee
                   else "Reimbursements through Penny aren't open to you yet — ping "
                   "Erica to join.")
            penny.send_dm(uid, msg, thread_ts=msg_ts)
            ledger.mark_dm_processed(conn, msg_ts, employee)
            return

        if can_reimburse:
            conn = ledger.open_db(db_path)

            def _intake(itext, anchor_ts, ifiles):
                """File a reimbursement from `itext`+`ifiles`; anchor its thread/id
                to `anchor_ts` (the pal's original message, even when resolved later
                via a clarifier). Dedup on the CURRENT message (msg_ts)."""
                if not ledger.claim_dm(conn, msg_ts, employee):
                    _log(f"[skip] {employee}: reimbursement msg already claimed (dup)")
                    return
                try:
                    _log(f"[reimburse] {employee} intake: {itext[:60]!r} "
                         f"+ {len(ifiles)} file(s)")
                    res = reimburse_flow.process_intake(
                        conn, cfg, employee, uid, itext, active_month(),
                        slack=penny, file_ids=ifiles, source_dm_ts=anchor_ts)
                    penny.send_dm(uid, res["confirm_back"], thread_ts=anchor_ts)
                    _log(f"[reimburse] {employee}: {res['status']}")
                except Exception:
                    ledger.unclaim_dm(conn, msg_ts)
                    _log(f"[error] reimbursement {employee}: {traceback.format_exc()}")

            def _card(ctext, cfiles):
                """Route a message to the card-charge reply flow (used when a
                clarifier answer says 'card')."""
                if not ledger.claim_dm(conn, msg_ts, employee):
                    _log(f"[skip] {employee}: msg already claimed (dup)")
                    return
                try:
                    res = reply_flow.process_pal_reply(
                        conn, cfg, employee, ctext, active_month(),
                        slack=penny, file_ids=cfiles)
                    penny.send_dm(uid, res["confirm_back"], thread_ts=msg_ts)
                    _log(f"[done] {employee}: card reply (via clarifier)")
                except Exception:
                    ledger.unclaim_dm(conn, msg_ts)
                    _log(f"[error] card {employee}: {traceback.format_exc()}")

            # 1) Reply inside an existing reimbursement's thread -> follow-up (thread
            #    wins, even if the person re-typed "reimburse" — never duplicates).
            followup_ext = f"reimb-{thread_ts}" if thread_ts else None
            if followup_ext and ledger.reimbursement_by_external_id(
                    conn, cfg.client, followup_ext):
                if not ledger.claim_dm(conn, msg_ts, employee):
                    _log(f"[skip] {employee}: reimbursement msg already claimed (dup)")
                    return
                try:
                    _log(f"[reimburse] {employee} follow-up in {followup_ext}: "
                         f"{text[:50]!r} + {len(file_ids)} file(s)")
                    res = reimburse_flow.process_followup(
                        conn, cfg, employee, uid, text, active_month(),
                        slack=penny, file_ids=file_ids, thread_ts=thread_ts)
                    penny.send_dm(uid, res["confirm_back"], thread_ts=thread_ts)
                    _log(f"[reimburse] {employee}: {res['status']}")
                except Exception:
                    ledger.unclaim_dm(conn, msg_ts)
                    _log(f"[error] reimbursement {employee}: {traceback.format_exc()}")
                return

            # 2) Explicit trigger word (top-level) -> intake (backward compatible).
            if starts_trigger and not thread_ts:
                _intake(text, msg_ts, file_ids)
                return

            # 3) A clarifier is pending -> read the answer and resolve the STASHED
            #    original message, so the pal never re-types or re-uploads.
            if not thread_ts:
                pend = _load_pending(pending_key, msg_ts)
                if pend is not None:
                    kv.set(pending_key, "")   # one-shot; always clear
                    ans = intent.interpret_answer(text)
                    if ans == "reimbursement":
                        _intake(pend["text"], pend["ts"], pend.get("file_ids") or [])
                        return
                    if ans == "card":
                        _card(pend["text"], pend.get("file_ids") or [])
                        return
                    # Couldn't read the answer -> treat THIS message fresh (below).

            # 4) Wordless, pal-initiated top-level DM -> classify intent.
            if not thread_ts:
                month = active_month()
                pal_charges = reply_flow._pal_charges(conn, cfg.client, month, employee)
                projects = reply_flow._projects(cfg, month)
                awaiting = bool([c for c in receipts.receipt_needed(pal_charges)
                                 if c.get("receipt_status") != "stored"])
                amts = [c["amount_cents"] for c in pal_charges if c.get("amount_cents")]
                kind = intent.classify(cfg.client, text, bool(file_ids),
                                       awaiting, projects, charge_amounts=amts)
                if kind == "reimbursement":
                    _intake(text, msg_ts, file_ids)
                    return
                if kind == "ambiguous":
                    if not ledger.claim_dm(conn, msg_ts, employee):
                        _log(f"[skip] {employee}: msg already claimed (dup)")
                        return
                    kv.set(pending_key, json.dumps(
                        {"ts": msg_ts, "text": text, "file_ids": file_ids}))
                    penny.send_dm(uid, reimburse_dm.clarify_intent(employee.split()[0]),
                                  thread_ts=msg_ts)
                    _log(f"[intent] {employee}: ambiguous -> asked clarifier")
                    return
                # kind == "card" -> fall through to the card path below.

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

    # The daily #finance digest thread was removed (Aug 2026). It keyed off the
    # current calendar month, so once the close month rolled over it reported an
    # empty in-flight month — a green all-clear while the prior close was still
    # open. The digest itself still exists and is now on-demand only:
    #   cfo-agent notify digest --client august --month 2026-07 --post
    # Per-pal DMs (notify send) are unaffected.

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
                    now = _dt.datetime.now(_dt.timezone.utc)
                    cur_month = now.strftime("%Y-%m")
                    py, pm = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
                    prev_month = f"{py:04d}-{pm:02d}"
                    conn = ledger.open_db(db_path)
                    # Sweep the CURRENT and PREVIOUS month: a charge that posts to
                    # Credit Card Pending in the last days of a month (or lands after
                    # the month rolls over) would otherwise never be fetched by the
                    # new month's poll — that's how the July 29–31 charges got stranded.
                    for mo in (cur_month, prev_month):
                        res = cc_mod.run_once(conn, cfg, penny, mo, post=True, log=_log)
                        if res.get("new"):
                            _log(f"[continuous] {mo}: {res['new']} new charge(s) "
                                 f"booked + DM'd: {res.get('pals')}")
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
