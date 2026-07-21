"""
Dead-man's switch — the heartbeat that lives OFF the box.

The alerter must not live on the corpse: the scheduler pings an external monitor,
and the monitor alerts when the pings stop. This covers the on-box half:
  - fires a GET to SCHED_HEARTBEAT_URL (with a bounded timeout) when set,
  - is a no-op when unset,
  - never raises into the scheduler even when the ping fails,
  - logs only on a state change, so a persistent outage doesn't spam the log.
"""

from unittest import mock

import pytest

import engine.scheduler as scheduler


class _Resp:
    def __init__(self, boom=False):
        self._boom = boom

    def raise_for_status(self):
        if self._boom:
            raise RuntimeError("500 Server Error")


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    # Every test starts from an unknown heartbeat state and a known URL/timeout.
    monkeypatch.setattr(scheduler, "_heartbeat_state", {"ok": None})
    monkeypatch.setattr(scheduler, "HEARTBEAT_URL", "https://hc-ping.com/abc")
    monkeypatch.setattr(scheduler, "HEARTBEAT_TIMEOUT_S", 5)


def test_heartbeat_fires_get_with_timeout():
    with mock.patch.object(scheduler.requests, "get", return_value=_Resp()) as get:
        scheduler.job_heartbeat()
    get.assert_called_once_with("https://hc-ping.com/abc", timeout=5)
    assert scheduler._heartbeat_state["ok"] is True


def test_heartbeat_skips_when_unset(monkeypatch):
    monkeypatch.setattr(scheduler, "HEARTBEAT_URL", "")
    with mock.patch.object(scheduler.requests, "get") as get:
        scheduler.job_heartbeat()
    get.assert_not_called()


def test_heartbeat_never_raises_on_failure():
    with mock.patch.object(scheduler.requests, "get",
                           side_effect=OSError("connection refused")):
        scheduler.job_heartbeat()   # must not raise
    assert scheduler._heartbeat_state["ok"] is False


def test_heartbeat_logs_once_per_state_change(capsys):
    # Two consecutive failures log FAILED exactly once (no per-tick spam).
    with mock.patch.object(scheduler.requests, "get",
                           side_effect=OSError("down")):
        scheduler.job_heartbeat()
        scheduler.job_heartbeat()
    out = capsys.readouterr().out
    assert out.count("heartbeat FAILED") == 1
    assert "heartbeat OK" not in out

    # Recovery logs OK exactly once.
    with mock.patch.object(scheduler.requests, "get", return_value=_Resp()):
        scheduler.job_heartbeat()
        scheduler.job_heartbeat()
    out2 = capsys.readouterr().out
    assert out2.count("heartbeat OK") == 1


def test_heartbeat_treats_http_error_as_failure():
    with mock.patch.object(scheduler.requests, "get", return_value=_Resp(boom=True)):
        scheduler.job_heartbeat()
    assert scheduler._heartbeat_state["ok"] is False


def test_heartbeat_is_registered_untracked():
    # It appears in the manifest and is honestly marked untracked (writes no job_run).
    from engine.scheduler import schedule_manifest
    entry = next(e for e in schedule_manifest() if e.id == "heartbeat")
    assert entry.job_type is None
    assert entry.cadence == "every 5m"
