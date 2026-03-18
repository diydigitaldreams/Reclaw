"""
Snapshot & Recovery — create known-good checkpoints and restore from them.

Snapshots are stored outside the OpenClaw workspace in ~/.openclaw/.reclaw/snapshots/
so they survive workspace corruption. Each snapshot is a timestamped directory
containing copies of critical files and a manifest.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    WORKSPACE_MARKDOWN_FILES,
    WorkspaceLayout,
)
from .scanner import scan_workspace


MANIFEST_FILENAME = "manifest.json"


@dataclass
class Snapshot:
    """A point-in-time backup of critical workspace files."""
    snapshot_id: str
    snapshot_dir: Path
    created_at: datetime
    files: list[str] = field(default_factory=list)
    scan_was_clean: bool = False
    note: str = ""

    @property
    def manifest_path(self) -> Path:
        return self.snapshot_dir / MANIFEST_FILENAME

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at.isoformat(),
            "files": self.files,
            "scan_was_clean": self.scan_was_clean,
            "note": self.note,
        }


@dataclass
class RestoreResult:
    """Outcome of a restore operation."""
    success: bool
    snapshot_id: str
    files_restored: list[str] = field(default_factory=list)
    files_skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def create_snapshot(
    layout: WorkspaceLayout,
    note: str = "",
    force: bool = False,
    max_snapshots: int = 10,
) -> Snapshot:
    """
    Create a snapshot of all critical workspace files.

    By default, runs a scan first and refuses to snapshot a broken state
    (so you don't overwrite a good snapshot with a bad one).
    Use force=True to snapshot regardless.
    """
    # Run scan unless forced
    scan_clean = True
    if not force:
        report = scan_workspace(layout)
        scan_clean = report.is_bootable
        if not scan_clean:
            raise SnapshotError(
                f"Workspace has {report.critical_count} critical issue(s). "
                "Use --force to snapshot anyway, but this will save a broken state."
            )

    # Create snapshot directory
    now = datetime.now(timezone.utc)
    snapshot_id = now.strftime("%Y%m%d_%H%M%S")
    snapshots_root = _ensure_snapshots_dir(layout)
    snapshot_dir = snapshots_root / snapshot_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    snapshot = Snapshot(
        snapshot_id=snapshot_id,
        snapshot_dir=snapshot_dir,
        created_at=now,
        scan_was_clean=scan_clean,
        note=note,
    )

    # Copy critical files
    files_to_backup = _collect_critical_files(layout)
    for src_path, rel_path in files_to_backup:
        dest = snapshot_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest)
        snapshot.files.append(rel_path)

    # Write manifest
    with open(snapshot.manifest_path, "w", encoding="utf-8") as f:
        json.dump(snapshot.to_dict(), f, indent=2)

    # Prune old snapshots
    _prune_snapshots(snapshots_root, keep=max_snapshots)

    return snapshot


def list_snapshots(layout: WorkspaceLayout) -> list[Snapshot]:
    """List all available snapshots, newest first."""
    snapshots_root = _get_snapshots_dir(layout)
    if not snapshots_root or not snapshots_root.is_dir():
        return []

    snapshots = []
    for entry in sorted(snapshots_root.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        manifest_path = entry / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            snapshots.append(Snapshot(
                snapshot_id=data["snapshot_id"],
                snapshot_dir=entry,
                created_at=datetime.fromisoformat(data["created_at"]),
                files=data.get("files", []),
                scan_was_clean=data.get("scan_was_clean", False),
                note=data.get("note", ""),
            ))
        except (json.JSONDecodeError, KeyError, OSError):
            continue

    return snapshots


def restore_snapshot(
    layout: WorkspaceLayout,
    snapshot_id: str | None = None,
    dry_run: bool = False,
) -> RestoreResult:
    """
    Restore workspace files from a snapshot.

    If no snapshot_id is given, uses the most recent clean snapshot.
    In dry_run mode, reports what would be restored without writing anything.
    """
    snapshots = list_snapshots(layout)
    if not snapshots:
        return RestoreResult(
            success=False,
            snapshot_id="",
            errors=["No snapshots available. Run 'reclaw snapshot' first."],
        )

    # Find the target snapshot
    target: Snapshot | None = None
    if snapshot_id:
        target = next(
            (s for s in snapshots if s.snapshot_id == snapshot_id), None
        )
        if not target:
            return RestoreResult(
                success=False,
                snapshot_id=snapshot_id,
                errors=[f"Snapshot '{snapshot_id}' not found."],
            )
    else:
        # Prefer the most recent clean snapshot
        target = next((s for s in snapshots if s.scan_was_clean), None)
        if not target:
            # Fall back to most recent regardless
            target = snapshots[0]

    result = RestoreResult(success=True, snapshot_id=target.snapshot_id)

    for rel_path in target.files:
        src = target.snapshot_dir / rel_path
        dest = layout.root / rel_path

        if not src.is_file():
            result.files_skipped.append(rel_path)
            result.errors.append(f"Snapshot file missing: {rel_path}")
            continue

        if dry_run:
            action = "OVERWRITE" if dest.exists() else "CREATE"
            result.files_restored.append(f"[{action}] {rel_path}")
            continue

        try:
            # Safety: reject symlinks to prevent path traversal
            if src.is_symlink():
                result.files_skipped.append(rel_path)
                result.errors.append(f"Skipped symlink: {rel_path}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            result.files_restored.append(rel_path)
        except OSError as e:
            result.errors.append(f"Failed to restore {rel_path}: {e}")
            result.success = False

    return result


# --- Internals ---

def _collect_critical_files(
    layout: WorkspaceLayout,
) -> list[tuple[Path, str]]:
    """
    Collect files that should be snapshotted.

    Returns (absolute_path, relative_path_from_root) pairs.
    """
    files: list[tuple[Path, str]] = []

    # Main config
    if layout.config_file and layout.config_file.is_file():
        rel = _rel_path(layout.config_file, layout.root)
        files.append((layout.config_file, rel))

    # Identity markdown files
    scan_root = layout.workspace_dir or layout.root
    for name in WORKSPACE_MARKDOWN_FILES:
        candidate = scan_root / name
        if candidate.is_file():
            rel = _rel_path(candidate, layout.root)
            files.append((candidate, rel))

    # Also grab CLAUDE.md if it exists
    claude_md = scan_root / "CLAUDE.md"
    if claude_md.is_file():
        rel = _rel_path(claude_md, layout.root)
        files.append((claude_md, rel))

    # Session files (they're the conversation history)
    if layout.sessions_dir and layout.sessions_dir.is_dir():
        for sf in layout.sessions_dir.rglob("*.json"):
            rel = _rel_path(sf, layout.root)
            files.append((sf, rel))
        for sf in layout.sessions_dir.rglob("*.jsonl"):
            rel = _rel_path(sf, layout.root)
            files.append((sf, rel))

    # Skill configs (but not skill code — that can be re-pulled from ClawHub)
    if layout.skills_dir and layout.skills_dir.is_dir():
        for skill_json in layout.skills_dir.rglob("*.json"):
            rel = _rel_path(skill_json, layout.root)
            files.append((skill_json, rel))

    return files


def _rel_path(file: Path, root: Path) -> str:
    """Get a safe relative path string."""
    try:
        return str(file.relative_to(root))
    except ValueError:
        return str(file.name)


def _ensure_snapshots_dir(layout: WorkspaceLayout) -> Path:
    """Create and return the snapshots directory."""
    snapshots_dir = layout.snapshots_dir or (layout.root / ".reclaw" / "snapshots")
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    return snapshots_dir


def _get_snapshots_dir(layout: WorkspaceLayout) -> Path | None:
    """Return snapshots directory if it exists."""
    snapshots_dir = layout.snapshots_dir or (layout.root / ".reclaw" / "snapshots")
    if snapshots_dir.is_dir():
        return snapshots_dir
    return None


def _prune_snapshots(snapshots_dir: Path, keep: int = 10) -> None:
    """Remove oldest snapshots beyond the keep limit."""
    dirs = sorted(
        [d for d in snapshots_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    for old_dir in dirs[keep:]:
        shutil.rmtree(old_dir, ignore_errors=True)


class SnapshotError(Exception):
    """Raised when a snapshot operation fails."""
    pass
