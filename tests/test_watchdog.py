import datetime as dt
import json
from pathlib import Path

import yaml

import scripts.watchdog_check as wc

ROOT = Path(__file__).parents[1]
WATCHDOG = ROOT / ".github/workflows/watchdog.yml"


def _runs(rows: list[dict], tmp_path: Path) -> Path:
    path = tmp_path / "runs.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def test_watchdog_workflow_is_scheduled_and_read_only():
    raw = WATCHDOG.read_text(encoding="utf-8")
    assert "schedule" in raw
    assert "*/15 * * * *" in raw
    workflow = yaml.safe_load(raw)
    permissions = workflow["permissions"]
    assert permissions.get("issues") == "write"
    assert permissions.get("actions") == "write"
    assert permissions.get("contents") == "read"
    assert "watchdog_check.py" in raw
    assert "cancel" in raw or "--stuck-minutes" in raw


def _stuck_run(id_: int, minutes_ago: int) -> dict:
    updated = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)).isoformat()
    return {"id": id_, "status": "in_progress", "updated_at": updated, "head_sha": "a" * 40}


def test_stuck_threshold_boundary(monkeypatch, tmp_path):
    """At 59 min idle (below 60) no alert; at 61 min (above) alert fires."""
    calls = {"cancel": 0, "alert": 0}

    def fake_gh(*args, **kwargs):
        if len(args) >= 2 and args[0] == "run" and args[1] == "cancel":
            calls["cancel"] += 1
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(wc, "gh", fake_gh)
    monkeypatch.setattr(wc, "upsert_alert", lambda repo, fp, body: calls.__setitem__("alert", calls["alert"] + 1))

    runs = [_stuck_run(1, 59)]
    wc.check_stuck(_runs(runs, tmp_path), "o/r", 60)
    assert calls["cancel"] == 0

    runs = [_stuck_run(2, 61)]
    wc.check_stuck(_runs(runs, tmp_path), "o/r", 60)
    assert calls["cancel"] == 1
    assert calls["alert"] == 1


def test_consecutive_failures_threshold_boundary(monkeypatch, tmp_path):
    """N-1 failures do not alert; N failures alert (N=3)."""
    calls = {"alert": 0}
    monkeypatch.setattr(wc, "upsert_alert", lambda repo, fp, body: calls.__setitem__("alert", calls["alert"] + 1))

    runs = [
        {"id": 1, "status": "completed", "conclusion": "failure"},
        {"id": 2, "status": "completed", "conclusion": "failure"},
        {"id": 3, "status": "completed", "conclusion": "success"},
    ]
    wc.check_consecutive_failures(_runs(runs, tmp_path), "o/r", 3)
    assert calls["alert"] == 0

    runs = [
        {"id": 1, "status": "completed", "conclusion": "failure"},
        {"id": 2, "status": "completed", "conclusion": "failure"},
        {"id": 3, "status": "completed", "conclusion": "failure"},
    ]
    wc.check_consecutive_failures(_runs(runs, tmp_path), "o/r", 3)
    assert calls["alert"] == 1


def test_alert_title_prefix_is_stable():
    assert wc.ALERT_TITLE_PREFIX == "[vps-watchdog] "


def test_freshness_reads_batch_json(monkeypatch, tmp_path):
    """Freshness must read finished_at from data/batch.json (manifest lacks it)."""
    import urllib.request as ur

    manifest = {"batch_id": "crawl_vps_promotions:1:1", "schema_version": 4}
    batch = {"batch_id": "crawl_vps_promotions:1:1",
             "finished_at": dt.datetime.now(dt.UTC).isoformat()}

    class R:
        def __init__(self, url):
            self._url = str(url)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            payload = batch if self._url.endswith("batch.json") else manifest
            return json.dumps(payload).encode()

    monkeypatch.setattr(ur, "urlopen", lambda url, timeout: R(url))
    calls = {"alert": 0}
    monkeypatch.setattr(wc, "upsert_alert", lambda repo, fp, body: calls.__setitem__("alert", calls["alert"] + 1))
    wc.check_freshness("o/r", 26, "https://fatty911.github.io/crawl_vps_promotions/manifest.json")
    assert calls["alert"] == 0


def test_freshness_stale_alert(monkeypatch):
    import urllib.request as ur

    manifest = {"batch_id": "crawl_vps_promotions:1:1"}
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=30)).isoformat()
    batch = {"batch_id": "crawl_vps_promotions:1:1", "finished_at": old}

    class R:
        def __init__(self, url):
            self._url = str(url)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            payload = batch if self._url.endswith("batch.json") else manifest
            return json.dumps(payload).encode()

    monkeypatch.setattr(ur, "urlopen", lambda url, timeout: R(url))
    calls = {"alert": 0}
    monkeypatch.setattr(wc, "upsert_alert", lambda repo, fp, body: calls.__setitem__("alert", calls["alert"] + 1))
    wc.check_freshness("o/r", 26, "https://fatty911.github.io/crawl_vps_promotions/manifest.json")
    assert calls["alert"] == 1


def test_watchdog_repair_separation():
    """C2: repair must be a separate, dispatch-only workflow; diagnosis
    must not carry contents:write (no implicit push authorization)."""
    diag_raw = (ROOT / ".github/workflows/vps-diagnosis.yml").read_text(encoding="utf-8")
    assert "contents: write" not in diag_raw
    assert "workflow_run" in diag_raw

    repair_raw = (ROOT / ".github/workflows/vps-repair.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in repair_raw
    assert "workflow_run" not in repair_raw
    assert "auto_repair == 'true'" in repair_raw
    assert "secrets.KIMI_CODINGPLAN_API_KEY" in repair_raw
    assert "secrets.NVIDIA_NIM_API_KEY" in repair_raw
