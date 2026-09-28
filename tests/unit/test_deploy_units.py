"""The systemd user units under deploy/ say what CLAUDE.md's Deployment section requires."""

from __future__ import annotations

from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _unit(name: str) -> dict[str, list[str]]:
    """Minimal systemd unit parser: section-agnostic key -> list of values."""
    out: dict[str, list[str]] = {}
    for line in (DEPLOY / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")):
            continue
        k, _, v = line.partition("=")
        out.setdefault(k.strip(), []).append(v.strip())
    return out


def test_ingest_timer_runs_hourly_with_small_jitter() -> None:
    t = _unit("gridcase-ingest.timer")
    assert t["OnCalendar"] == ["hourly"]
    assert int(t["RandomizedDelaySec"][0]) <= 600
    assert t["Persistent"] == ["true"]
    assert "timers.target" in t["WantedBy"][0]


def test_ingest_service_runs_live_ingest_for_eia_and_openmeteo() -> None:
    s = _unit("gridcase-ingest.service")
    execs = " ; ".join(s["ExecStart"])
    assert "gridcase ingest --source eia --live" in execs
    assert "gridcase ingest --source openmeteo --live" in execs
    assert s["Type"] == ["oneshot"]
    assert s["WorkingDirectory"][0].endswith("/gridcase")
    # .env is read by python-dotenv from the working directory; no secrets in the unit
    assert "Environment" not in s
    assert not any("KEY" in v for vals in s.values() for v in vals)


def test_ingest_service_does_not_fight_the_server_for_the_db_lock() -> None:
    s = _unit("gridcase-ingest.service")
    # A failed hourly run must not retry in a tight loop while the server holds the file.
    assert s.get("Restart", ["no"]) == ["no"]
