"""Penny's live listener (Socket Mode). Connects to Slack with the app token,
and when a pal DMs Penny, processes the reply end-to-end: applies their
categorization/billable/project + recategorizations, downloads & files any
receipt, and replies with a confirm-back — all autonomously.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from threading import Event

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from ..config import env, load_client
from ..engine import ledger, reply_flow
from .slack_client import PennySlack

RUNS_LOCAL = Path(__file__).resolve().parents[2] / "runs"


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
        subtype = e.get("subtype")
        # Process normal DMs and file uploads (subtype 'file_share'); skip edits,
        # joins, and other subtypes, bot messages, and Penny's own messages.
        if (e.get("type") != "message" or e.get("channel_type") != "im"
                or e.get("bot_id") or e.get("user") == me
                or (subtype and subtype != "file_share")):
            return
        uid = e.get("user")
        text = e.get("text", "") or ""
        cardholder = resolve_cardholder(uid, text)
        if not cardholder:
            _log(f"[skip] DM from unmapped user {uid}")
            return
        file_ids = [f["id"] for f in e.get("files", []) if f.get("id")]
        on_behalf = " (on behalf, by admin)" if slack_users.get(uid) != cardholder else ""
        _log(f"[reply] {cardholder}{on_behalf}: {text[:70]!r} + {len(file_ids)} file(s)")
        try:
            conn = ledger.open_db(db_path)   # fresh connection on this worker thread
            res = reply_flow.process_pal_reply(conn, cfg, cardholder, text, month,
                                               slack=penny, file_ids=file_ids)
            penny.send_dm(uid, res["confirm_back"], thread_ts=e.get("ts"))
            _log(f"[done] {cardholder}: {res['decisions']} billable/project, "
                 f"{res['recats']} recat, {res['receipts']} receipt(s); confirm-back sent")
        except Exception:
            _log(f"[error] processing {cardholder}: {traceback.format_exc()}")

    sm.socket_mode_request_listeners.append(handle)
    sm.connect()
    _log(f"Penny listening (Socket Mode) for {client_name} {month} — bot {me}. Ctrl-C to stop.")
    Event().wait()
