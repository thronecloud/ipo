"""
Failure/anomaly notifications for the living engine.

Zero-infrastructure by default: set NTFY_TOPIC (any unique string) and subscribe
to https://ntfy.sh/<topic> on your phone/browser — no signup needed. A generic
JSON webhook (Slack/Discord/Telegram bridge) is also supported via NOTIFY_WEBHOOK_URL.
If neither env var is set, notify() is a silent no-op (logs only).

Design rules:
- Never raise: a broken notifier must never break the engine.
- Rate-limited per (title) key to avoid alert storms from a looping job.
"""

import json
import os
import time
import urllib.request

_last_sent: dict[str, float] = {}
RATE_LIMIT_S = int(os.environ.get("NOTIFY_RATE_LIMIT_S", "900"))  # 15 min per title


def _post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def notify(title: str, message: str, priority: str = "default", tags: str = "warning") -> bool:
    """Send a notification. Returns True if at least one channel accepted it."""
    now = time.time()
    if now - _last_sent.get(title, 0) < RATE_LIMIT_S:
        return False
    _last_sent[title] = now

    sent = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            _post(
                f"https://ntfy.sh/{topic}",
                message.encode(),
                {"Title": title, "Priority": priority, "Tags": tags},
            )
            sent = True
        except Exception as e:  # never let the notifier break the engine
            print(f"[notify] ntfy failed: {e}")

    webhook = os.environ.get("NOTIFY_WEBHOOK_URL")
    if webhook:
        try:
            _post(
                webhook,
                json.dumps({"title": title, "message": message, "priority": priority}).encode(),
                {"Content-Type": "application/json"},
            )
            sent = True
        except Exception as e:
            print(f"[notify] webhook failed: {e}")

    if not topic and not webhook:
        print(f"[notify] (no channel configured) {title}: {message[:120]}")
    return sent
