"""Telegram notifications via the Bot API (stdlib urllib only)."""

from __future__ import annotations

import json
import logging
import time
import urllib.request

from ..errors import ConfigError
from ..settings import TelegramConfig

API = "https://api.telegram.org/bot{token}/sendMessage"
GETUPDATES = "https://api.telegram.org/bot{token}/getUpdates"
log = logging.getLogger("aa.telegram")


def _http_get(url: str, timeout: int = 20):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def send_message(text: str, cfg: TelegramConfig, timeout: int = 20) -> bool:
    """Send a plain-text message. Returns True on success; never raises."""
    token = (cfg.bot_token or "").strip()
    chat_id = (cfg.chat_id or "").strip()
    if not cfg.enabled:
        log.info("telegram disabled — skipping notification")
        return False
    if not token or not chat_id:
        raise ConfigError(
            "Telegram enabled but bot_token/chat_id are empty. "
            "Fill config/telegram.yaml or run `aa test-telegram`."
        )
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        API.format(token=token),
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read() or b"{}")
        ok = bool(body.get("ok"))
        if not ok:
            log.warning("telegram returned not-ok: %s", body.get("description"))
        return ok
    except Exception as exc:  # network / bot API hiccups must not fail the run
        log.warning("telegram send failed: %s", exc)
        return False


def test_telegram(cfg: TelegramConfig) -> bool:
    """Send a probe message; used by `aa test-telegram`."""
    return send_message(
        "AutoApply: Telegram notification channel works \u2705", cfg
    )


def ask_and_wait(cfg: TelegramConfig, question: str, timeout: int = 480) -> str | None:
    """Send `question` to the chat and block until the user replies.

    Polls getUpdates for any new message from the configured chat. Returns the
    reply text (normalized) or None if the timeout lapses or notifications are
    disabled (a disabled channel means "no interactive answers").
    """
    token = (cfg.bot_token or "").strip()
    chat_id = (cfg.chat_id or "").strip()
    if not cfg.enabled:
        log.info("telegram disabled — interactive apply prompt skipped")
        return None
    if not token or not chat_id:
        raise ConfigError(
            "Telegram enabled but bot_token/chat_id are empty for interactive apply."
        )
    last_id = _fetch_max_update_id(token)
    if not send_message(question, cfg):
        return None
    deadline = time.monotonic() + timeout
    log.info("telegram: asked question; waiting up to %ss for a reply", timeout)
    while time.monotonic() < deadline:
        try:
            body = _http_get(GETUPDATES.format(token=token), timeout=15)
        except Exception as exc:
            log.warning("telegram getUpdates failed: %s", exc)
            time.sleep(3)
            continue
        for upd in (body.get("result") or []):
            if int(upd.get("update_id", 0)) <= last_id:
                continue
            msg = upd.get("message") or upd.get("edited_message") or {}
            if str(msg.get("chat", {}).get("id", "")) == str(chat_id):
                text = (msg.get("text") or "").strip()
                if text:
                    return text
        time.sleep(3)
    log.info("telegram: no reply within %ss for question", timeout)
    return None


def _fetch_max_update_id(token: str) -> int:
    try:
        body = _http_get(GETUPDATES.format(token=token), timeout=15)
        ids = [int(u.get("update_id", 0)) for u in (body.get("result") or [])]
        return max(ids) if ids else 0
    except Exception:
        return 0