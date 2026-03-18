"""
OpenClaw workspace discovery and path resolution.

ReClaw must find the workspace without relying on OpenClaw being alive.
We discover paths from the filesystem directly.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


# Known OpenClaw directory layouts
DEFAULT_OPENCLAW_DIR = Path.home() / ".openclaw"
CONFIG_FILENAME = "openclaw.json"

WORKSPACE_MARKDOWN_FILES = [
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
]

# JSON files we expect to be parseable
JSON_EXTENSIONS = {".json", ".jsonl"}
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
CONFIG_EXTENSIONS = JSON_EXTENSIONS | {".yaml", ".yml", ".toml"}


@dataclass
class WorkspaceLayout:
    """Resolved paths for an OpenClaw workspace."""

    root: Path
    config_file: Path | None = None
    workspace_dir: Path | None = None
    sessions_dir: Path | None = None
    skills_dir: Path | None = None
    snapshots_dir: Path | None = None  # ReClaw's own snapshot storage
    markdown_files: list[Path] = field(default_factory=list)
    json_files: list[Path] = field(default_factory=list)
    all_config_files: list[Path] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.root.exists() and self.config_file is not None


def discover_workspace(search_path: Path | None = None) -> WorkspaceLayout:
    """
    Discover an OpenClaw workspace by scanning the filesystem.

    Search order:
    1. Explicit path argument
    2. OPENCLAW_HOME environment variable
    3. ~/.openclaw (default)
    """
    candidates = []

    if search_path:
        candidates.append(Path(search_path))

    env_home = os.environ.get("OPENCLAW_HOME")
    if env_home:
        candidates.append(Path(env_home))

    candidates.append(DEFAULT_OPENCLAW_DIR)

    for candidate in candidates:
        candidate = candidate.expanduser().resolve()
        if not candidate.exists():
            continue

        layout = _scan_directory(candidate)
        if layout.config_file is not None:
            return layout

    # Return an empty layout pointing to the best candidate
    best = candidates[0] if candidates else DEFAULT_OPENCLAW_DIR
    return WorkspaceLayout(root=best.expanduser().resolve())


def _scan_directory(root: Path) -> WorkspaceLayout:
    """Scan a directory tree and map out the workspace structure."""
    layout = WorkspaceLayout(root=root)

    # Find the main config
    config_path = root / CONFIG_FILENAME
    if config_path.is_file():
        layout.config_file = config_path

    # Try to resolve workspace dir from config
    workspace_dir = _resolve_workspace_from_config(root, layout.config_file)
    if workspace_dir and workspace_dir.is_dir():
        layout.workspace_dir = workspace_dir
    else:
        layout.workspace_dir = root

    # Map known subdirectories
    scan_root = layout.workspace_dir or root

    # Sessions: check both flat (sessions/) and agent-based (agents/*/sessions/)
    sessions_dir = scan_root / "sessions"
    if sessions_dir.is_dir():
        layout.sessions_dir = sessions_dir
    else:
        # OpenClaw often stores sessions under agents/<name>/sessions/
        agents_dir = scan_root / "agents"
        if agents_dir.is_dir():
            for agent_dir in sorted(agents_dir.iterdir()):
                if agent_dir.is_dir():
                    agent_sessions = agent_dir / "sessions"
                    if agent_sessions.is_dir():
                        layout.sessions_dir = agent_sessions
                        break

    # Skills: check both flat and agent-based layouts
    skills_dir = scan_root / "skills"
    if skills_dir.is_dir():
        layout.skills_dir = skills_dir
    else:
        agents_dir = scan_root / "agents"
        if agents_dir.is_dir():
            for agent_dir in sorted(agents_dir.iterdir()):
                if agent_dir.is_dir():
                    agent_skills = agent_dir / "skills"
                    if agent_skills.is_dir():
                        layout.skills_dir = agent_skills
                        break

    # ReClaw's own snapshot directory (created on first snapshot)
    layout.snapshots_dir = root / ".reclaw" / "snapshots"

    # Collect all relevant files
    layout.markdown_files = _collect_files(scan_root, MARKDOWN_EXTENSIONS, max_depth=3)
    layout.json_files = _collect_files(scan_root, JSON_EXTENSIONS, max_depth=5)
    layout.all_config_files = _collect_files(
        scan_root, CONFIG_EXTENSIONS, max_depth=5
    )

    # Also scan the root if workspace_dir is different
    if scan_root != root:
        layout.json_files.extend(_collect_files(root, JSON_EXTENSIONS, max_depth=1))
        layout.all_config_files.extend(
            _collect_files(root, CONFIG_EXTENSIONS, max_depth=1)
        )

    return layout


def _resolve_workspace_from_config(
    root: Path, config_file: Path | None
) -> Path | None:
    """Try to extract the workspace path from openclaw.json."""
    if not config_file or not config_file.is_file():
        return None
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        ws = data.get("workspace") or data.get("workspaceDir")
        if ws:
            ws_path = Path(ws).expanduser()
            if not ws_path.is_absolute():
                ws_path = root / ws_path
            return ws_path.resolve()
    except (json.JSONDecodeError, OSError, TypeError):
        pass
    return None


def _collect_files(
    root: Path, extensions: set[str], max_depth: int = 5
) -> list[Path]:
    """Recursively collect files matching given extensions, with depth limit."""
    results = []
    _walk(root, extensions, results, current_depth=0, max_depth=max_depth)
    return sorted(results)


def _walk(
    directory: Path,
    extensions: set[str],
    results: list[Path],
    current_depth: int,
    max_depth: int,
) -> None:
    if current_depth > max_depth:
        return
    try:
        for entry in sorted(directory.iterdir()):
            # Skip hidden dirs and node_modules
            if entry.name.startswith(".") or entry.name == "node_modules":
                continue
            if entry.is_file() and entry.suffix.lower() in extensions:
                results.append(entry)
            elif entry.is_dir():
                _walk(entry, extensions, results, current_depth + 1, max_depth)
    except PermissionError:
        pass
