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


def test_server_service_is_the_single_writer_and_binds_tailscale_only() -> None:
    s = _unit("gridcase-server.service")
    execs = " ; ".join(s["ExecStart"])
    assert "gridcase serve" in execs
    assert "--host 100.115.94.44" in execs
    assert "--port 8710" in execs
    assert "--no-ingest" not in execs  # the server owns ingest; no second writer process
    assert s["Restart"] == ["on-failure"]
    assert s["WorkingDirectory"][0].endswith("/gridcase")
    assert "Environment" not in s
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
    assert "Environment" not in s


def test_install_script_enables_the_collector_in_every_mode() -> None:
    text = (DEPLOY / "install.sh").read_text()
    assert "enable --now gridcase-collector.service" in text


def test_tunnel_publishes_the_server_without_mcp() -> None:
    """kanumadhok.com/marketcase reaches the VPS through a Cloudflare Tunnel (outbound only: no
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
    assert rules[1]["service"] == "http://100.115.94.44:8710"
    assert rules[-1] == {"service": "http_status:404"}  # anything else: nothing
    text = (DEPLOY / "install.sh").read_text()
    assert "cert.pem" in text and "enable --now gridcase-tunnel.service" in text
