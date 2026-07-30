"""Behavioural tests for vps_monitor – config validation, publish guard, workflow gates."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "vps-monitor.yml"

sys.path.insert(0, str(ROOT))

from vps_monitor.monitor import load_config, validate_records_for_publish


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "providers.yaml"
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return p


VALID_PROVIDER = {
    "id": "test-1",
    "name": "Test Provider",
    "url": "https://example.com/store",
}


# ---------------------------------------------------------------------------
# a) load_config validation
# ---------------------------------------------------------------------------

class TestLoadConfigValidation:
    def test_empty_providers_raises(self, tmp_path):
        p = _write_yaml(tmp_path, {"providers": []})
        with pytest.raises(ValueError, match="(?i)provider"):
            load_config(p)

    def test_missing_providers_key_raises(self, tmp_path):
        p = _write_yaml(tmp_path, {"other": 1})
        with pytest.raises(ValueError, match="(?i)provider"):
            load_config(p)

    def test_missing_id_raises(self, tmp_path):
        prov = {"name": "X", "url": "https://a.com"}
        p = _write_yaml(tmp_path, {"providers": [prov]})
        with pytest.raises(ValueError, match="(?i)id"):
            load_config(p)

    def test_missing_name_raises(self, tmp_path):
        prov = {"id": "x", "url": "https://a.com"}
        p = _write_yaml(tmp_path, {"providers": [prov]})
        with pytest.raises(ValueError, match="(?i)name"):
            load_config(p)

    def test_missing_url_raises(self, tmp_path):
        prov = {"id": "x", "name": "X"}
        p = _write_yaml(tmp_path, {"providers": [prov]})
        with pytest.raises(ValueError, match="(?i)url"):
            load_config(p)

    def test_duplicate_id_raises(self, tmp_path):
        provs = [
            {"id": "dup", "name": "A", "url": "https://a.com"},
            {"id": "dup", "name": "B", "url": "https://b.com"},
        ]
        p = _write_yaml(tmp_path, {"providers": provs})
        with pytest.raises(ValueError, match="(?i)duplicate.*id"):
            load_config(p)

    def test_duplicate_url_raises(self, tmp_path):
        provs = [
            {"id": "a", "name": "A", "url": "https://same.com"},
            {"id": "b", "name": "B", "url": "https://same.com"},
        ]
        p = _write_yaml(tmp_path, {"providers": provs})
        with pytest.raises(ValueError, match="(?i)duplicate.*url"):
            load_config(p)

    def test_non_http_url_raises(self, tmp_path):
        prov = {"id": "x", "name": "X", "url": "ftp://bad.com"}
        p = _write_yaml(tmp_path, {"providers": [prov]})
        with pytest.raises(ValueError, match="(?i)url"):
            load_config(p)

    def test_valid_config_returns_dict(self, tmp_path):
        p = _write_yaml(tmp_path, {"providers": [VALID_PROVIDER]})
        cfg = load_config(p)
        assert isinstance(cfg, dict)
        assert cfg["providers"][0]["id"] == "test-1"


# ---------------------------------------------------------------------------
# b) validate_records_for_publish
# ---------------------------------------------------------------------------

class TestValidateRecordsForPublish:
    def test_empty_records_raises(self):
        with pytest.raises(RuntimeError):
            validate_records_for_publish([])

    def test_all_failed_raises(self):
        records = [
            {"id": "a", "status": "抓取失败"},
            {"id": "b", "status": "抓取失败"},
        ]
        with pytest.raises(RuntimeError):
            validate_records_for_publish(records)

    def test_one_success_passes(self):
        records = [
            {"id": "a", "status": "抓取失败"},
            {"id": "b", "status": "可能有货"},
        ]
        # Should not raise
        validate_records_for_publish(records)

    def test_all_success_passes(self):
        records = [{"id": "a", "status": "可能有货"}]
        validate_records_for_publish(records)


# ---------------------------------------------------------------------------
# c) workflow gates
# ---------------------------------------------------------------------------

class TestWorkflowGates:
    @pytest.fixture(autouse=True)
    def _load_workflow(self):
        self.wf = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.job = self.wf["jobs"]["monitor"]

    def test_job_has_timeout(self):
        assert "timeout-minutes" in self.job, "monitor job must set timeout-minutes"
        assert self.job["timeout-minutes"] <= 45

    def test_pytest_before_build(self):
        steps = self.job["steps"]
        names = [s.get("name", "") for s in steps]
        run_cmds = [s.get("run", "") for s in steps]
        # Find pytest step index
        pytest_idx = None
        build_idx = None
        for i, (name, run) in enumerate(zip(names, run_cmds)):
            if "pytest" in run:
                pytest_idx = i
            if "Build" in name or ("--output" in run and "monitor" in run):
                build_idx = i
        assert pytest_idx is not None, "workflow must have a pytest step"
        assert build_idx is not None, "workflow must have a Build step"
        assert pytest_idx < build_idx, "pytest must run before Build"

    def test_cron_preserved(self):
        on = self.wf.get("on") or self.wf.get(True)
        schedules = on["schedule"]
        crons = [s["cron"] for s in schedules]
        assert "20 22 * * *" in crons

    def test_workflow_dispatch_preserved(self):
        on = self.wf.get("on") or self.wf.get(True)
        assert "workflow_dispatch" in on

    def test_pages_path_site(self):
        steps = self.job["steps"]
        upload_step = next(s for s in steps if "upload-pages-artifact" in s.get("uses", ""))
        assert upload_step["with"]["path"] == "site"

    def test_install_step_includes_pytest(self):
        steps = self.job["steps"]
        install_step = next(
            (s for s in steps if s.get("name") == "Install dependencies"), None
        )
        assert install_step is not None, "must have Install dependencies step"
        run_text = install_step.get("run", "")
        assert "pytest" in run_text, (
            "Install dependencies run must explicitly install pytest"
        )
