"""Penny's Slack client (dedicated bot). Sends DMs as Penny, resolves pals by
email, and — the reason we built the bot — downloads receipt files a pal sends,
so they can be retained in the repository instead of only living in Slack.

A bot can only download files from conversations it belongs to, so file download
works for receipts sent to Penny's own DMs (post-cutover), not files in someone
else's DMs.
"""
from __future__ import annotations

from pathlib import Path

import httpx

from ..config import env

API = "https://slack.com/api"


class SlackError(RuntimeError):
    pass


class PennySlack:
    def __init__(self, bot_token: str = None):
        self.token = bot_token or env("SLACK_BOT_TOKEN")
        if not self.token:
            raise SlackError("Missing SLACK_BOT_TOKEN in .env (Penny bot).")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _post(self, method: str, **payload) -> dict:
        r = httpx.post(f"{API}/{method}", headers=self._headers(), json=payload, timeout=60)
        data = r.json()
        if not data.get("ok"):
            raise SlackError(f"{method} failed: {data.get('error')}")
        return data

    def _get(self, method: str, **params) -> dict:
        r = httpx.get(f"{API}/{method}", headers=self._headers(), params=params, timeout=60)
        data = r.json()
        if not data.get("ok"):
            raise SlackError(f"{method} failed: {data.get('error')}")
        return data

    def auth_test(self) -> dict:
        return self._post("auth.test")

    def user_id_by_email(self, email: str) -> str:
        return self._get("users.lookupByEmail", email=email)["user"]["id"]

    def open_dm(self, user_id: str) -> str:
        return self._post("conversations.open", users=user_id)["channel"]["id"]

    def send_dm(self, user_id: str, text: str, thread_ts: str = None) -> dict:
        channel = self.open_dm(user_id)
        payload = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._post("chat.postMessage", **payload)

    def download_file(self, file_id: str, dest: Path) -> Path:
        """Download a Slack file (by id) to dest. Requires files:read and that
        Penny is a member of the conversation the file was shared in."""
        info = self._get("files.info", file=file_id)["file"]
        url = info.get("url_private_download") or info.get("url_private")
        if not url:
            raise SlackError(f"file {file_id} has no downloadable URL")
        r = httpx.get(url, headers=self._headers(), timeout=120, follow_redirects=True)
        if r.status_code != 200 or r.content[:9].lower().startswith(b"<!doctype"):
            # Slack returns an HTML login page (200) when the token can't access the file.
            raise SlackError(f"file {file_id} not accessible to Penny "
                             "(bot must be in the conversation)")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
