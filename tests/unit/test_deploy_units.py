"""The systemd user units under deploy/ say what CLAUDE.md's Deployment section requires."""

from __future__ import annotations

import uuid
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


def test_server_service_is_the_single_writer_and_binds_tailscale_only() -> None:
    s = _unit("gridcase-server.service")
    execs = " ; ".join(s["ExecStart"])
    assert "gridcase serve" in execs
    assert "--host 100.125.186.6" in execs
    assert "--port 8710" in execs
    assert "--no-ingest" not in execs  # the server owns ingest; no second writer process
    assert s["Restart"] == ["on-failure"]
    assert s["WorkingDirectory"][0].endswith("/gridcase")
    assert s["Environment"] == ["TZ=UTC"]  # DuckDB uses the host's zone; times are shown as UTC
    assert not any("KEY" in v or "TOKEN" in v for vals in s.values() for v in vals)


def test_install_script_enables_the_server_and_retires_the_grid_ingest_timer() -> None:
    text = (DEPLOY / "install.sh").read_text()
    assert "enable --now gridcase-server.service" in text
    assert "disable --now gridcase-ingest.timer" in text  # left over on hosts from the grid era
    assert not (DEPLOY / "gridcase-ingest.timer").exists()


def test_collector_runs_forever_and_restarts_on_failure() -> None:
    s = _unit("gridcase-collector.service")
    assert "gridcase collect" in " ; ".join(s["ExecStart"])
    assert s["Restart"] == ["always"]
    assert s["WorkingDirectory"][0].endswith("/gridcase")
    assert s["Environment"] == ["TZ=UTC"]


def test_install_script_enables_the_collector_in_every_mode() -> None:
    text = (DEPLOY / "install.sh").read_text()
    assert "enable --now gridcase-collector.service" in text


def test_tunnel_publishes_the_server_without_mcp() -> None:
    """kanumadhok.com/marketcase reaches kayak through a Cloudflare Tunnel (outbound only: no
    port opened). The tunnel refuses MCP at Cloudflare's edge, before the app's own check."""
    import yaml

    s = _unit("gridcase-tunnel.service")
    execs = " ; ".join(s["ExecStart"])
    assert "cloudflared" in execs and "tunnel" in execs and "run" in execs
    assert "--no-autoupdate" in execs  # updates are a deploy decision, not a surprise
    assert s["Restart"] == ["always"]
    cfg = yaml.safe_load((DEPLOY / "cloudflared-marketcase.yml").read_text())
    rules = cfg["ingress"]
    assert rules[0]["service"] == "http_status:404" and "mcp" in rules[0]["path"]
    assert rules[1]["hostname"] == "marketcase.kanumadhok.com"
    assert rules[1]["service"] == "http://100.125.186.6:8710"
    assert rules[-1] == {"service": "http_status:404"}  # anything else: nothing
    # By id, not name: the host needs only that tunnel's credentials file, not the account cert.
    uuid.UUID(cfg["tunnel"])
    text = (DEPLOY / "install.sh").read_text()
    assert "cert.pem" in text and "enable --now gridcase-tunnel.service" in text


def test_airflow_schedules_the_warehouse_and_binds_tailscale_only() -> None:
    s = _unit("gridcase-airflow.service")
    root = "%h/Documents/Claude/Projects/gridcase"
    assert s["Type"] == ["simple"]
    assert s["Restart"] == ["on-failure"]
    assert s["MemoryMax"] == ["2600M"]  # Airflow idles near 1 GB; dbt peaks at 1.3 GB
    assert s["Nice"] == ["10"]
    assert s["WorkingDirectory"] == [root]
    assert s["ExecStart"] == [f"{root}/orchestration/airflow/.venv/bin/airflow standalone"]
    env = dict(value.split("=", 1) for value in s["Environment"])
    assert env["AIRFLOW_HOME"] == "%h/.local/share/marketcase-airflow"
    assert env["AIRFLOW__CORE__DAGS_FOLDER"] == f"{root}/orchestration/airflow/dags"
    assert env["AIRFLOW__CORE__LOAD_EXAMPLES"] == "False"
    assert env["AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION"] == "False"  # else nothing ever runs
    assert env["AIRFLOW__API__HOST"] == "100.125.186.6"
    assert env["AIRFLOW__API__PORT"] == "8711"
    assert env["AIRFLOW__CORE__EXECUTION_API_SERVER_URL"] == "http://100.125.186.6:8711/execution/"
    assert env["GRIDCASE_ROOT"] == root
    assert env["TZ"] == "UTC"
    assert env["UV"] == "%h/.local/bin/uv"
    # standalone starts its scheduler, API server and DAG processor as `airflow ...` children.
    assert env["PATH"].startswith(f"{root}/orchestration/airflow/.venv/bin:")
    assert env["AIRFLOW__API__WORKERS"] == "1"
    assert env["AIRFLOW__CORE__PARALLELISM"] == "2"  # one chain; default 32 idle workers OOM
    assert not any("KEY" in v or "TOKEN" in v for vals in s.values() for v in vals)


def test_install_script_replaces_the_dbt_timer_with_airflow() -> None:
    text = (DEPLOY / "install.sh").read_text()
    assert 'if [ ! -d "$root/orchestration/airflow/.venv" ]; then' in text
    assert 'bash "$root/orchestration/airflow/setup.sh"' in text
    assert "enable --now gridcase-airflow.service" in text
    assert "disable --now gridcase-dbt.timer" in text
    assert 'rm -f "$dest/gridcase-dbt.timer" "$dest/gridcase-dbt.service"' in text
    install_loop = text.split("for f in ", 1)[1].split("; do", 1)[0]
    assert "gridcase-airflow.service" in install_loop
    assert "gridcase-dbt" not in install_loop
    assert "enable --now gridcase-dbt.timer" not in text
    assert not (DEPLOY / "gridcase-dbt.service").exists()
    assert not (DEPLOY / "gridcase-dbt.timer").exists()


def test_the_collectors_interval_is_the_one_the_site_explains() -> None:
    from gridcase.ingest.polymarket import COLLECT_EVERY_S

    exec_start = _unit("gridcase-collector.service")["ExecStart"][0]
    assert (
        f"--interval {COLLECT_EVERY_S:g}" in exec_start
    )  # "How is this calculated?" says 4 minutes
