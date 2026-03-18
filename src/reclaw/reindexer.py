"""
Re-indexer — rebuild the structural map of an OpenClaw workspace.

Ignores all existing pointers and indexes. Discovers everything
from the raw filesystem, then produces a clean manifest that
OpenClaw (or the operator) can use to restore coherence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import WorkspaceLayout


@dataclass
class SkillEntry:
    """A discovered skill in the workspace."""
    name: str
    path: Path
    has_readme: bool = False
    has_code: bool = False
    entry_files: list[str] = field(default_factory=list)


@dataclass
class SessionEntry:
    """A discovered session file."""
    file: Path
    session_id: Optional[str] = None
    model: Optional[str] = None
    channel: Optional[str] = None
    message_count: int = 0
    last_modified: Optional[datetime] = None
    size_bytes: int = 0
    parseable: bool = True


@dataclass
class WorkspaceIndex:
    """Complete structural map rebuilt from the filesystem."""
    root: Path
    generated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    config: Optional[dict[str, Any]] = None
    config_parseable: bool = False
    workspace_dir: Optional[Path] = None
    skills: list[SkillEntry] = field(default_factory=list)
    sessions: list[SessionEntry] = field(default_factory=list)
    identity_files: dict[str, Path] = field(default_factory=dict)
    orphaned_files: list[Path] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary."""
        return {
            "generated_at": self.generated_at.isoformat(),
            "root": str(self.root),
            "config_parseable": self.config_parseable,
            "workspace_dir": str(self.workspace_dir) if self.workspace_dir else None,
            "skills_count": len(self.skills),
            "skills": [
                {
                    "name": s.name,
                    "path": str(s.path),
                    "has_readme": s.has_readme,
                    "has_code": s.has_code,
                    "entry_files": s.entry_files,
                }
                for s in self.skills
            ],
            "sessions_count": len(self.sessions),
            "sessions": [
                {
                    "file": str(s.file),
                    "session_id": s.session_id,
                    "model": s.model,
                    "channel": s.channel,
                    "message_count": s.message_count,
                    "last_modified": (
                        s.last_modified.isoformat() if s.last_modified else None
                    ),
                    "size_bytes": s.size_bytes,
                    "parseable": s.parseable,
                }
                for s in self.sessions
            ],
            "identity_files": {k: str(v) for k, v in self.identity_files.items()},
            "orphaned_files": [str(f) for f in self.orphaned_files],
        }

    def save(self, output_path: Path) -> None:
        """Write the index to a JSON file."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)


def reindex_workspace(layout: WorkspaceLayout) -> WorkspaceIndex:
    """
    Rebuild the complete workspace index from raw filesystem state.

    Does NOT trust any existing index or pointer. Everything is
    discovered fresh from disk.
    """
    index = WorkspaceIndex(root=layout.root)

    # 1. Parse main config (if possible)
    index.config, index.config_parseable = _load_config(layout)
    index.workspace_dir = layout.workspace_dir

    # 2. Discover identity files (AGENTS.md, SOUL.md, USER.md)
    index.identity_files = _discover_identity_files(layout)

    # 3. Discover skills
    index.skills = _discover_skills(layout)

    # 4. Discover sessions
    index.sessions = _discover_sessions(layout)

    # 5. Find orphaned files
    index.orphaned_files = _find_orphaned_files(layout, index)

    return index


def _load_config(
    layout: WorkspaceLayout,
) -> tuple[Optional[dict[str, Any]], bool]:
    """Attempt to load the main config, returning (data, success)."""
    if not layout.config_file or not layout.config_file.is_file():
        return None, False
    try:
        with open(layout.config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data, True
        return None, False
    except (json.JSONDecodeError, OSError):
        return None, False


def _discover_identity_files(layout: WorkspaceLayout) -> dict[str, Path]:
    """Find identity markdown files in the workspace."""
    identity = {}
    search_dirs = [layout.root]
    if layout.workspace_dir and layout.workspace_dir != layout.root:
        search_dirs.insert(0, layout.workspace_dir)

    target_names = {"AGENTS.md", "SOUL.md", "USER.md", "CLAUDE.md", "README.md"}

    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        for name in target_names:
            candidate = search_dir / name
            if candidate.is_file() and name not in identity:
                identity[name] = candidate

    return identity


def _discover_skills(layout: WorkspaceLayout) -> list[SkillEntry]:
    """Discover all skills from the filesystem."""
    skills = []

    skills_dirs = []
    if layout.skills_dir and layout.skills_dir.is_dir():
        skills_dirs.append(layout.skills_dir)

    # Also check workspace root for a skills/ dir
    if layout.workspace_dir:
        alt_skills = layout.workspace_dir / "skills"
        if alt_skills.is_dir() and alt_skills not in skills_dirs:
            skills_dirs.append(alt_skills)

    for skills_dir in skills_dirs:
        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue

            skill = SkillEntry(name=entry.name, path=entry)

            # Check for documentation
            for readme_name in ["README.md", "SKILL.md", "skill.md", "readme.md"]:
                if (entry / readme_name).is_file():
                    skill.has_readme = True
                    skill.entry_files.append(readme_name)
                    break

            # Check for code files
            py_files = list(entry.glob("*.py"))
            js_files = list(entry.glob("*.js"))
            sh_files = list(entry.glob("*.sh"))
            if py_files or js_files or sh_files:
                skill.has_code = True
                skill.entry_files.extend(
                    f.name for f in (py_files + js_files + sh_files)
                )

            skills.append(skill)

    return skills


def _discover_sessions(layout: WorkspaceLayout) -> list[SessionEntry]:
    """Discover and inspect all session files."""
    sessions = []

    search_dirs = []
    if layout.sessions_dir and layout.sessions_dir.is_dir():
        search_dirs.append(layout.sessions_dir)

    # Also check for sessions at workspace root
    if layout.workspace_dir:
        alt = layout.workspace_dir / "sessions"
        if alt.is_dir() and alt not in search_dirs:
            search_dirs.append(alt)

    for sessions_dir in search_dirs:
        for sf in sorted(sessions_dir.rglob("*.json")):
            stat = sf.stat()
            entry = SessionEntry(
                file=sf,
                size_bytes=stat.st_size,
                last_modified=datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ),
            )

            try:
                with open(sf, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, dict):
                    entry.session_id = data.get("id") or data.get("sessionId")
                    entry.model = data.get("model")
                    entry.channel = data.get("channel") or data.get("platform")

                    # Try to count messages
                    messages = data.get("messages") or data.get("history") or []
                    if isinstance(messages, list):
                        entry.message_count = len(messages)

            except (json.JSONDecodeError, OSError):
                entry.parseable = False

            sessions.append(entry)

    return sessions


def _find_orphaned_files(
    layout: WorkspaceLayout, index: WorkspaceIndex
) -> list[Path]:
    """
    Find files that exist on disk but aren't referenced by anything.

    This is a heuristic — we flag JSON files outside known directories
    that don't belong to sessions, skills, or config.
    """
    orphaned = []

    known_paths: set[Path] = set()
    if layout.config_file:
        known_paths.add(layout.config_file)
    for p in index.identity_files.values():
        known_paths.add(p)
    for s in index.sessions:
        known_paths.add(s.file)
    for sk in index.skills:
        known_paths.add(sk.path)

    # Check JSON files at the root level that aren't the main config
    scan_root = layout.workspace_dir or layout.root
    for json_file in layout.json_files:
        if json_file in known_paths:
            continue
        # If it's inside sessions/ or skills/, it's accounted for
        try:
            json_file.relative_to(layout.sessions_dir or Path("/nonexistent"))
            continue
        except (ValueError, TypeError):
            pass
        try:
            json_file.relative_to(layout.skills_dir or Path("/nonexistent"))
            continue
        except (ValueError, TypeError):
            pass

        orphaned.append(json_file)

    return orphaned
