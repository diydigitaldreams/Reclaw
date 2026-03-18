"""Tests for the ReClaw recovery engine."""

import json
from pathlib import Path

import pytest

from reclaw.config import discover_workspace
from reclaw.scanner import scan_workspace, Severity
from reclaw.reindexer import reindex_workspace
from reclaw.snapshot import (
    SnapshotError,
    create_snapshot,
    list_snapshots,
    restore_snapshot,
)


@pytest.fixture
def mock_workspace(tmp_path):
    """Create a minimal valid OpenClaw workspace (flat layout)."""
    config = {
        "ai": {"model": "claude-sonnet-4-20250514", "provider": "anthropic"},
        "channels": {"telegram": {"enabled": True}},
        "workspace": str(tmp_path / "workspace"),
    }
    (tmp_path / "openclaw.json").write_text(json.dumps(config, indent=2))

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# Agents\nDefault agent config.")
    (ws / "SOUL.md").write_text("# Soul\nYou are a helpful assistant.")
    (ws / "USER.md").write_text("# User\nJP lives in Puerto Rico.")

    sessions = ws / "sessions"
    sessions.mkdir()
    session = {
        "id": "session-001",
        "model": "claude-sonnet-4-20250514",
        "channel": "telegram",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hey there"},
        ],
    }
    (sessions / "session-001.json").write_text(json.dumps(session))

    skills = ws / "skills"
    skills.mkdir()
    skill = skills / "web-search"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Web Search\nSearches the web.")
    (skill / "search.py").write_text("def search(q): pass")

    return tmp_path


@pytest.fixture
def openclaw_workspace(tmp_path):
    """Create a workspace matching real OpenClaw layout (agents/*/sessions/, JSONL)."""
    config = {
        "channels": {"telegram": {"enabled": True}},
        "gateway": {"port": 18789},
        "tools": {},
        "commands": {},
    }
    (tmp_path / "openclaw.json").write_text(json.dumps(config, indent=2))

    # Agent-based session layout
    agents = tmp_path / "agents" / "main"
    agents.mkdir(parents=True)
    sessions = agents / "sessions"
    sessions.mkdir()

    # JSONL-format session file (one JSON object per line)
    lines = [
        json.dumps({"role": "user", "content": "hello"}),
        json.dumps({"role": "assistant", "content": "hey there"}),
    ]
    (sessions / "abc123.json").write_text("\n".join(lines))

    # Proper .jsonl log files
    logs = tmp_path / "logs"
    logs.mkdir()
    log_lines = [
        json.dumps({"ts": "2026-03-18", "event": "boot"}),
        json.dumps({"ts": "2026-03-18", "event": "ready"}),
    ]
    (logs / "config-audit.jsonl").write_text("\n".join(log_lines))

    # Cron runs
    cron = tmp_path / "cron" / "runs"
    cron.mkdir(parents=True)
    (cron / "job.jsonl").write_text("\n".join([
        json.dumps({"ts": "2026-03-18", "status": "ok"}),
    ]))

    # Empty patch file (intentional)
    (tmp_path / "config.patch.json").write_text("")

    return tmp_path


@pytest.fixture
def mock_layout(mock_workspace):
    return discover_workspace(mock_workspace)


@pytest.fixture
def openclaw_layout(openclaw_workspace):
    return discover_workspace(openclaw_workspace)


# === Scanner: Core ===

class TestScannerCore:
    def test_clean_workspace_is_bootable(self, mock_layout):
        report = scan_workspace(mock_layout)
        assert report.is_bootable
        assert report.critical_count == 0

    def test_missing_config_is_critical(self, tmp_path):
        layout = discover_workspace(tmp_path)
        report = scan_workspace(layout)
        assert not report.is_bootable
        assert report.critical_count > 0

    def test_corrupt_json_detected(self, mock_workspace, mock_layout):
        sessions = mock_workspace / "workspace" / "sessions"
        (sessions / "bad.json").write_text('{"id": "broken"')
        report = scan_workspace(mock_layout)
        hits = [f for f in report.findings if "JSON" in f.message or "truncated" in f.message]
        assert len(hits) > 0

    def test_null_bytes_detected(self, mock_workspace, mock_layout):
        sessions = mock_workspace / "workspace" / "sessions"
        (sessions / "corrupted.json").write_bytes(b'{"id": "test"\x00\x00\x00}')
        report = scan_workspace(mock_layout)
        hits = [f for f in report.findings if "corruption" in f.message.lower()]
        assert len(hits) > 0

    def test_empty_identity_file_warned(self, mock_workspace, mock_layout):
        (mock_workspace / "workspace" / "SOUL.md").write_text("")
        report = scan_workspace(mock_layout)
        hits = [f for f in report.findings if "empty" in f.message.lower()]
        assert len(hits) > 0

    def test_broken_workspace_path_is_critical(self, mock_workspace):
        config = json.loads((mock_workspace / "openclaw.json").read_text())
        config["workspace"] = "/nonexistent/path/that/doesnt/exist"
        (mock_workspace / "openclaw.json").write_text(json.dumps(config))
        layout = discover_workspace(mock_workspace)
        report = scan_workspace(layout)
        hits = [
            f for f in report.findings
            if "workspace path" in f.message.lower() or "does not exist" in f.message.lower()
        ]
        assert len(hits) > 0


# === Scanner: JSONL & OpenClaw layout ===

class TestScannerJSONL:
    def test_jsonl_sessions_no_false_positives(self, openclaw_layout):
        """JSONL-format .json session files should not trigger 'Extra data' warnings."""
        report = scan_workspace(openclaw_layout)
        extra = [f for f in report.findings if f.detail and "Extra data" in f.detail]
        assert len(extra) == 0, f"Got {len(extra)} false positives"

    def test_jsonl_log_files_validated(self, openclaw_layout):
        """Proper .jsonl files should be validated line-by-line and marked healthy."""
        report = scan_workspace(openclaw_layout)
        assert report.is_bootable
        assert report.files_healthy > 0

    def test_empty_patch_is_info_not_warning(self, openclaw_layout):
        """Empty config.patch.json is intentional — should be INFO, not WARNING."""
        report = scan_workspace(openclaw_layout)
        patch_findings = [f for f in report.findings if "config.patch" in str(f.file)]
        assert len(patch_findings) > 0
        assert all(f.severity == Severity.INFO for f in patch_findings)

    def test_agents_sessions_discovered(self, openclaw_layout):
        """Workspace discovery should find sessions under agents/*/sessions/."""
        assert openclaw_layout.sessions_dir is not None


# === Reindexer ===

class TestReindexer:
    def test_discovers_all_components(self, mock_layout):
        index = reindex_workspace(mock_layout)
        assert index.config_parseable
        assert len(index.identity_files) >= 3
        assert len(index.skills) >= 1
        assert len(index.sessions) >= 1

    def test_session_metadata_extracted(self, mock_layout):
        index = reindex_workspace(mock_layout)
        session = index.sessions[0]
        assert session.session_id == "session-001"
        assert session.model == "claude-sonnet-4-20250514"
        assert session.message_count == 2
        assert session.parseable

    def test_skill_metadata_extracted(self, mock_layout):
        index = reindex_workspace(mock_layout)
        skill = next(s for s in index.skills if s.name == "web-search")
        assert skill.has_readme
        assert skill.has_code

    def test_index_serializes_to_json(self, mock_layout, tmp_path):
        index = reindex_workspace(mock_layout)
        output = tmp_path / "index.json"
        index.save(output)
        assert output.exists()
        data = json.loads(output.read_text())
        assert data["config_parseable"] is True
        assert data["skills_count"] >= 1


# === Snapshot & Recovery ===

class TestSnapshot:
    def test_create_and_list(self, mock_layout):
        snap = create_snapshot(mock_layout, note="test snapshot")
        assert snap.scan_was_clean
        assert len(snap.files) > 0
        snaps = list_snapshots(mock_layout)
        assert len(snaps) == 1
        assert snaps[0].snapshot_id == snap.snapshot_id

    def test_restore_dry_run(self, mock_layout):
        create_snapshot(mock_layout)
        result = restore_snapshot(mock_layout, dry_run=True)
        assert result.success
        assert len(result.files_restored) > 0

    def test_restore_recovers_deleted_config(self, mock_layout, mock_workspace):
        create_snapshot(mock_layout)
        config_path = mock_workspace / "openclaw.json"
        config_path.unlink()
        assert not config_path.exists()
        result = restore_snapshot(mock_layout)
        assert result.success
        assert config_path.exists()

    def test_snapshot_refuses_broken_state(self, tmp_path):
        (tmp_path / "openclaw.json").write_text(
            json.dumps({"workspace": "/this/path/does/not/exist"})
        )
        layout = discover_workspace(tmp_path)
        with pytest.raises(SnapshotError):
            create_snapshot(layout, force=False)


# === Watcher ===

class TestWatcher:
    def test_watcher_imports(self):
        from reclaw.watcher import WatchConfig, WatchState, watch_workspace
        assert callable(watch_workspace)

    def test_watch_config_defaults(self):
        from reclaw.watcher import WatchConfig
        config = WatchConfig()
        assert config.cooldown == 30
        assert config.min_snapshot_interval == 300
        assert config.max_snapshot_interval == 3600
        assert config.poll_interval == 10
        assert config.use_events is True
        assert config.scan_on_start is True
        assert config.snapshot_on_start is True

    def test_build_hash_map(self, mock_layout):
        from reclaw.watcher import _build_hash_map
        hashes = _build_hash_map(mock_layout)
        assert len(hashes) > 0
        assert "openclaw.json" in hashes

    def test_hash_map_detects_changes(self, mock_layout, mock_workspace):
        from reclaw.watcher import _build_hash_map, _diff_hashes
        before = _build_hash_map(mock_layout)

        # Modify a file
        soul = mock_workspace / "workspace" / "SOUL.md"
        soul.write_text("# Soul\nUpdated content.")

        after = _build_hash_map(mock_layout)
        changed = _diff_hashes(before, after)
        assert "SOUL.md" in changed

    def test_hash_map_ignores_reclaw_dir(self, mock_layout):
        from reclaw.watcher import _build_hash_map
        # Create a snapshot first (creates .reclaw/ dir)
        create_snapshot(mock_layout, note="test")
        hashes = _build_hash_map(mock_layout)
        reclaw_files = [k for k in hashes if ".reclaw" in k]
        assert len(reclaw_files) == 0

    def test_alert_callback_fires(self):
        from reclaw.watcher import WatchConfig, WatchState, _emit_alert
        alerts = []
        config = WatchConfig(on_alert=lambda sev, msg: alerts.append((sev, msg)))
        state = WatchState()
        _emit_alert(config, state, "critical", "test alert")
        assert len(alerts) == 1
        assert alerts[0] == ("critical", "test alert")
        assert state.alerts_fired == 1

    def test_cli_watch_command_exists(self):
        from reclaw.cli import main
        # Verify 'watch' is a registered command
        assert "watch" in main.commands

