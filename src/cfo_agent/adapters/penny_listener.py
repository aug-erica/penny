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
    slack_users = cfg.raw.get("bot", {}).get("slack_users", {})
    penny = PennySlack()
    me = penny.auth_test()["user_id"]

    sm = SocketModeClient(app_token=env("SLACK_APP_TOKEN"),
                          web_client=WebClient(token=env("SLACK_BOT_TOKEN")))

    def handle(smc: SocketModeClient, req: SocketModeRequest):
        if req.type != "events_api":
            return
        smc.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        e = req.payload.get("event", {})
        if (e.get("type") != "message" or e.get("channel_type") != "im"
                or e.get("bot_id") or e.get("subtype") or e.get("user") == me):
            return
        uid = e.get("user")
        cardholder = slack_users.get(uid)
        if not cardholder:
            _log(f"[skip] DM from unmapped user {uid}")
            return
        text = e.get("text", "") or ""
        file_ids = [f["id"] for f in e.get("files", []) if f.get("id")]
        _log(f"[reply] {cardholder}: {text[:70]!r} + {len(file_ids)} file(s)")
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
