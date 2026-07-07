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
from ..engine import digest as digest_mod
from ..engine import kv, ledger, penny_catchup, reply_flow
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
        """Resolve the working close month PER EVENT (not once at startup):
        the DB value (set by an admin DM) wins; --month is the fallback."""
        return kv.active_month(cfg.client, month)

    def resolve_cardholder(sender_uid, text):
        """Sender's own DM = their charges. But an admin naming another pal in the
        message files on that pal's behalf."""
        sender = slack_users.get(sender_uid)
        if sender_uid in admins and text:
            tl = text.lower()
            named = {c for c in slack_users.values() if c != sender
                     and (c.split()[0].lower() in tl or c.split()[-1].lower() in tl)}
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

        cardholder = resolve_cardholder(uid, text)
        if not cardholder:
            _log(f"[skip] DM from unmapped user {uid}")
            return
        file_ids = [f["id"] for f in e.get("files", []) if f.get("id")]
        on_behalf = " (on behalf, by admin)" if slack_users.get(uid) != cardholder else ""
        _log(f"[reply] {cardholder}{on_behalf}: {text[:70]!r} + {len(file_ids)} file(s)")
        try:
            conn = ledger.open_db(db_path)   # fresh connection on this worker thread
            res = reply_flow.process_pal_reply(conn, cfg, cardholder, text, active_month(),
                                               slack=penny, file_ids=file_ids)
            penny.send_dm(uid, res["confirm_back"], thread_ts=e.get("ts"))
            ledger.mark_dm_processed(conn, e.get("ts"), cardholder)
            _log(f"[done] {cardholder}: {res['decisions']} billable/project, "
                 f"{res['recats']} recat, {res['receipts']} receipt(s); confirm-back sent")
        except Exception:
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

    sm.socket_mode_request_listeners.append(handle)
    sm.connect()
    _log(f"Penny listening (Socket Mode) for {client_name}, close month "
         f"{active_month()} — bot {me}. Ctrl-C to stop.")
    Event().wait()
