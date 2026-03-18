"""
Watcher — background filesystem monitor with auto-snapshot.

Monitors the OpenClaw workspace for changes and automatically creates
recovery snapshots when the workspace is healthy. Alerts when corruption
is detected.

Two modes:
  - Polling (default, no dependencies): checks for file changes on an interval
  - Event-based (if watchdog is installed): reacts to filesystem events in real time
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .config import WorkspaceLayout
from .scanner import ScanReport, Severity, scan_workspace
from .snapshot import SnapshotError, create_snapshot, list_snapshots


# Try to import watchdog for event-based watching
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent

    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False


@dataclass
class WatchConfig:
    """Configuration for the watcher."""

    # How long to wait after last change before scanning (seconds)
    cooldown: int = 30

    # Minimum time between snapshots (seconds)
    min_snapshot_interval: int = 300  # 5 minutes

    # Maximum time between snapshots even if no changes (seconds)
    max_snapshot_interval: int = 3600  # 1 hour

    # Polling interval when not using watchdog (seconds)
    poll_interval: int = 10

    # Use watchdog if available
    use_events: bool = True

    # Run a scan on startup
    scan_on_start: bool = True

    # Snapshot on startup if healthy
    snapshot_on_start: bool = True

    # Maximum snapshots to keep
    max_snapshots: int = 20

    # Alert callback (receives severity and message)
    on_alert: Optional[Callable[[str, str], None]] = None

    # Status callback (receives status message)
    on_status: Optional[Callable[[str], None]] = None


@dataclass
class WatchState:
    """Internal state for the watcher loop."""

    last_snapshot_time: float = 0.0
    last_change_time: float = 0.0
    last_scan_time: float = 0.0
    changes_detected: bool = False
    running: bool = True
    file_hashes: dict[str, str] = field(default_factory=dict)
    snapshots_created: int = 0
    scans_run: int = 0
    alerts_fired: int = 0


def watch_workspace(
    layout: WorkspaceLayout,
    config: Optional[WatchConfig] = None,
) -> None:
    """
    Main watch loop. Blocks until interrupted (Ctrl+C / SIGTERM).

    Flow:
    1. Optionally scan + snapshot on startup
    2. Monitor for file changes (polling or watchdog events)
    3. After changes settle (cooldown), run a scan
    4. If scan is clean, auto-snapshot
    5. If scan finds critical issues, fire an alert
    6. Repeat
    """
    if config is None:
        config = WatchConfig()

    state = WatchState()

    # Wire up graceful shutdown
    def _shutdown(signum, frame):
        state.running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _emit_status(config, "ReClaw watcher starting...")
    _emit_status(config, f"  Workspace: {layout.root}")
    _emit_status(config, f"  Cooldown: {config.cooldown}s")
    _emit_status(config, f"  Min snapshot interval: {config.min_snapshot_interval}s")
    _emit_status(config, f"  Mode: {'events (watchdog)' if config.use_events and HAS_WATCHDOG else 'polling'}")

    # Startup scan
    if config.scan_on_start:
        _emit_status(config, "\nRunning startup scan...")
        report = _run_scan(layout, config, state)
        if report.is_bootable:
            _emit_status(config, f"  ✓ Workspace is bootable ({report.files_scanned} files, {report.warning_count} warnings)")
            if config.snapshot_on_start:
                _try_snapshot(layout, config, state, note="watcher startup")
        else:
            _emit_alert(config, state, "critical",
                        f"Workspace has {report.critical_count} critical issue(s) at startup!")

    # Build initial file hash map for polling
    state.file_hashes = _build_hash_map(layout)

    _emit_status(config, "\nWatching for changes... (Ctrl+C to stop)\n")

    # Choose watch strategy
    if config.use_events and HAS_WATCHDOG:
        _watch_with_events(layout, config, state)
    else:
        _watch_with_polling(layout, config, state)

    # Shutdown
    _emit_status(config, f"\nWatcher stopped. {state.snapshots_created} snapshots created, {state.scans_run} scans run.")


def _watch_with_polling(
    layout: WorkspaceLayout,
    config: WatchConfig,
    state: WatchState,
) -> None:
    """Poll the filesystem for changes on an interval."""
    while state.running:
        time.sleep(config.poll_interval)

        if not state.running:
            break

        # Check for file changes
        new_hashes = _build_hash_map(layout)
        if new_hashes != state.file_hashes:
            changed = _diff_hashes(state.file_hashes, new_hashes)
            state.file_hashes = new_hashes
            state.last_change_time = time.time()
            state.changes_detected = True
            _emit_status(config, f"  [{_now_str()}] {len(changed)} file(s) changed: {', '.join(changed[:3])}{'...' if len(changed) > 3 else ''}")

        # Check if we should scan (changes settled past cooldown)
        _maybe_scan_and_snapshot(layout, config, state)


def _watch_with_events(
    layout: WorkspaceLayout,
    config: WatchConfig,
    state: WatchState,
) -> None:
    """Use watchdog for event-based filesystem monitoring."""
    if not HAS_WATCHDOG:
        _watch_with_polling(layout, config, state)
        return

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event: FileSystemEvent):
            # Skip directory events and temp files
            if event.is_directory:
                return
            src = Path(event.src_path)
            if src.name.startswith(".") or "__pycache__" in str(src):
                return
            # Skip our own snapshot directory
            if ".reclaw" in str(src):
                return

            state.last_change_time = time.time()
            state.changes_detected = True
            _emit_status(config, f"  [{_now_str()}] {event.event_type}: {src.name}")

    observer = Observer()
    observer.schedule(_Handler(), str(layout.root), recursive=True)
    observer.start()

    try:
        while state.running:
            time.sleep(config.poll_interval)
            if not state.running:
                break
            _maybe_scan_and_snapshot(layout, config, state)
    finally:
        observer.stop()
        observer.join(timeout=5)


def _maybe_scan_and_snapshot(
    layout: WorkspaceLayout,
    config: WatchConfig,
    state: WatchState,
) -> None:
    """Check if conditions are met to scan and/or snapshot."""
    now = time.time()

    # Forced periodic snapshot even without changes
    if (now - state.last_snapshot_time) > config.max_snapshot_interval:
        _emit_status(config, f"  [{_now_str()}] Periodic check (no changes for {config.max_snapshot_interval}s)...")
        report = _run_scan(layout, config, state)
        if report.is_bootable:
            _try_snapshot(layout, config, state, note="periodic")
        return

    # Changes detected and cooldown has passed
    if state.changes_detected and (now - state.last_change_time) > config.cooldown:
        state.changes_detected = False
        _emit_status(config, f"  [{_now_str()}] Changes settled, scanning...")

        report = _run_scan(layout, config, state)

        if report.is_bootable:
            # Respect minimum snapshot interval
            if (now - state.last_snapshot_time) > config.min_snapshot_interval:
                _try_snapshot(layout, config, state, note="auto")
            else:
                remaining = int(config.min_snapshot_interval - (now - state.last_snapshot_time))
                _emit_status(config, f"    Healthy, but snapshot cooldown ({remaining}s remaining)")
        else:
            _emit_alert(
                config, state, "critical",
                f"Workspace corruption detected! {report.critical_count} critical issue(s):\n"
                + "\n".join(
                    f"    • {f.message} ({f.file.name})"
                    for f in report.findings
                    if f.severity == Severity.CRITICAL
                ),
            )


def _run_scan(
    layout: WorkspaceLayout,
    config: WatchConfig,
    state: WatchState,
) -> ScanReport:
    """Run a scan and update state."""
    report = scan_workspace(layout)
    state.scans_run += 1
    state.last_scan_time = time.time()
    return report


def _try_snapshot(
    layout: WorkspaceLayout,
    config: WatchConfig,
    state: WatchState,
    note: str = "",
) -> bool:
    """Attempt to create a snapshot. Returns True on success."""
    try:
        snap = create_snapshot(
            layout, note=f"watch: {note}", force=False,
            max_snapshots=config.max_snapshots,
        )
        state.last_snapshot_time = time.time()
        state.snapshots_created += 1
        _emit_status(config, f"    ✓ Snapshot {snap.snapshot_id} ({len(snap.files)} files)")

        return True
    except SnapshotError as e:
        _emit_status(config, f"    ✗ Snapshot failed: {e}")
        return False


# --- Helpers ---

def _build_hash_map(layout: WorkspaceLayout) -> dict[str, str]:
    """Build a map of file path -> content hash for change detection."""
    hashes = {}
    scan_root = layout.workspace_dir or layout.root

    for f in _iter_watchable_files(scan_root):
        try:
            stat = f.stat()
            # Use mtime + size as a fast proxy (no need to read file content)
            key = str(f.relative_to(layout.root))
            hashes[key] = f"{stat.st_mtime}:{stat.st_size}"
        except OSError:
            continue

    # Also watch the root config
    if layout.config_file and layout.config_file.is_file():
        try:
            stat = layout.config_file.stat()
            hashes["openclaw.json"] = f"{stat.st_mtime}:{stat.st_size}"
        except OSError:
            pass

    return hashes


def _diff_hashes(
    old: dict[str, str], new: dict[str, str]
) -> list[str]:
    """Return list of changed/added/removed file names."""
    changed = []
    all_keys = set(old.keys()) | set(new.keys())
    for key in sorted(all_keys):
        if old.get(key) != new.get(key):
            name = Path(key).name
            if name not in changed:
                changed.append(name)
    return changed


def _iter_watchable_files(root: Path, max_depth: int = 5) -> list[Path]:
    """Iterate files worth watching, skipping noise."""
    results = []
    _walk_watchable(root, results, 0, max_depth)
    return results


def _walk_watchable(
    directory: Path,
    results: list[Path],
    depth: int,
    max_depth: int,
) -> None:
    if depth > max_depth:
        return
    try:
        for entry in directory.iterdir():
            # Skip hidden dirs (includes .reclaw) and node_modules
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue
            if entry.is_file():
                results.append(entry)
            elif entry.is_dir():
                _walk_watchable(entry, results, depth + 1, max_depth)
    except PermissionError:
        pass


def _emit_status(config: WatchConfig, message: str) -> None:
    """Send a status message through the configured callback or print."""
    if config.on_status:
        config.on_status(message)
    else:
        print(message, flush=True)


def _emit_alert(
    config: WatchConfig,
    state: WatchState,
    severity: str,
    message: str,
) -> None:
    """Fire an alert through the configured callback or print."""
    state.alerts_fired += 1
    if config.on_alert:
        config.on_alert(severity, message)
    else:
        print(f"\n⚠️  ALERT [{severity.upper()}]: {message}\n", flush=True)


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")
