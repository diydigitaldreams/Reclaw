"""
Scanner — surgical audit of every file in the workspace.

Checks for:
- JSON parse errors (truncated writes, encoding issues)
- Null bytes / binary corruption in text files
- Empty files that shouldn't be empty
- Orphaned references (sessions pointing to missing skills, etc.)
- Config schema violations (missing required keys)
- Markdown structure issues
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .config import WorkspaceLayout


class Severity(str, Enum):
    CRITICAL = "critical"   # System won't boot
    WARNING = "warning"     # Degraded but functional
    INFO = "info"           # Cosmetic or minor


@dataclass
class Finding:
    """A single issue found by the scanner."""
    severity: Severity
    file: Path
    message: str
    detail: Optional[str] = None
    line: Optional[int] = None
    recoverable: bool = True

    def __str__(self) -> str:
        loc = f"{self.file}"
        if self.line:
            loc += f":{self.line}"
        return f"[{self.severity.value.upper()}] {loc} — {self.message}"


@dataclass
class ScanReport:
    """Aggregated results from a full workspace scan."""
    workspace_root: Path
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    files_healthy: int = 0

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.WARNING)

    @property
    def is_bootable(self) -> bool:
        """Can OpenClaw likely boot with the current state?"""
        return self.critical_count == 0

    def summary(self) -> str:
        status = "HEALTHY" if self.is_bootable else "BROKEN"
        lines = [
            f"Scan complete: {status}",
            f"  Files scanned: {self.files_scanned}",
            f"  Healthy:       {self.files_healthy}",
            f"  Critical:      {self.critical_count}",
            f"  Warnings:      {self.warning_count}",
            f"  Info:          {len(self.findings) - self.critical_count - self.warning_count}",
        ]
        return "\n".join(lines)


# --- Required keys in openclaw.json (best-effort based on known schema) ---
OPENCLAW_REQUIRED_KEYS: list[str] = []  # None strictly required to parse
OPENCLAW_EXPECTED_KEYS: set[str] = {
    "ai", "channels", "workspace", "tools", "gateway",
    "sessions", "commands", "memory",
}


def scan_workspace(layout: WorkspaceLayout) -> ScanReport:
    """Run all scanners across the workspace and return a report."""
    report = ScanReport(workspace_root=layout.root)

    # 0. Check workspace exists at all
    if not layout.root.exists():
        report.findings.append(Finding(
            severity=Severity.CRITICAL,
            file=layout.root,
            message="Workspace root directory does not exist",
            recoverable=False,
        ))
        return report

    # 1. Validate main config
    if layout.config_file:
        _scan_main_config(layout.config_file, report)
    else:
        report.findings.append(Finding(
            severity=Severity.CRITICAL,
            file=layout.root / "openclaw.json",
            message="Main config file (openclaw.json) not found",
            detail="OpenClaw cannot boot without this file.",
            recoverable=False,
        ))

    # 2. Scan all JSON files (skip session files — scanned separately below)
    for json_file in layout.json_files:
        if layout.sessions_dir and _is_under(json_file, layout.sessions_dir):
            continue
        _scan_json_file(json_file, report)

    # 3. Scan markdown files
    for md_file in layout.markdown_files:
        _scan_markdown_file(md_file, report)

    # 4. Scan sessions directory
    if layout.sessions_dir:
        _scan_sessions(layout.sessions_dir, report)

    # 5. Scan skills directory
    if layout.skills_dir:
        _scan_skills(layout.skills_dir, layout, report)

    # 6. Cross-reference checks
    _scan_cross_references(layout, report)

    return report


def _scan_main_config(config_path: Path, report: ScanReport) -> None:
    """Validate the main openclaw.json."""
    report.files_scanned += 1
    data = _try_parse_json(config_path, report, critical=True)
    if data is None:
        return

    report.files_healthy += 1

    if not isinstance(data, dict):
        report.findings.append(Finding(
            severity=Severity.CRITICAL,
            file=config_path,
            message="Config root is not a JSON object",
            detail=f"Got {type(data).__name__}, expected dict",
        ))
        return

    # Check for expected top-level keys
    present_keys = set(data.keys())
    missing_expected = OPENCLAW_EXPECTED_KEYS - present_keys
    if missing_expected:
        report.findings.append(Finding(
            severity=Severity.INFO,
            file=config_path,
            message=f"Missing common config sections: {', '.join(sorted(missing_expected))}",
            detail="These may be optional depending on your setup.",
        ))

    # Check for obviously broken values
    _check_for_null_values(config_path, data, report, path_prefix="")


def _scan_json_file(file_path: Path, report: ScanReport) -> None:
    """Validate a single JSON or JSONL file."""
    report.files_scanned += 1

    # Check for binary corruption first
    corruption = _check_binary_corruption(file_path)
    if corruption:
        report.findings.append(Finding(
            severity=Severity.CRITICAL,
            file=file_path,
            message="Binary corruption detected",
            detail=corruption,
        ))
        return

    # Check for empty file
    if file_path.stat().st_size == 0:
        # Some files are intentionally empty (e.g. config.patch.json)
        severity = Severity.INFO if "patch" in file_path.name.lower() else Severity.WARNING
        report.findings.append(Finding(
            severity=severity,
            file=file_path,
            message="File is empty (0 bytes)",
            detail="May be intentional (patch/override file) or a truncated write.",
        ))
        return

    # Route JSONL files to line-by-line validation
    if file_path.suffix.lower() == ".jsonl":
        _scan_jsonl_file(file_path, report)
        return

    # Try standard JSON parse — track findings so we can roll back if it's JSONL
    findings_before = len(report.findings)
    data = _try_parse_json(file_path, report, critical=False)
    if data is not None:
        report.files_healthy += 1
        return

    # If JSON parse failed with "Extra data", this is likely JSONL
    # written with a .json extension (OpenClaw session files do this)
    new_findings = report.findings[findings_before:]
    has_extra_data = any(
        f.detail and "Extra data" in f.detail for f in new_findings
    )
    if has_extra_data:
        # Remove all findings from the failed JSON parse attempt
        del report.findings[findings_before:]
        _scan_jsonl_file(file_path, report)


def _scan_jsonl_file(file_path: Path, report: ScanReport) -> None:
    """Validate a JSONL (JSON Lines) file — one JSON object per line."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        report.findings.append(Finding(
            severity=Severity.WARNING,
            file=file_path,
            message="Cannot read JSONL file",
            detail=str(e),
        ))
        return

    lines = content.splitlines()
    bad_lines = []
    total_lines = 0

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue  # Blank lines are fine in JSONL
        total_lines += 1
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            bad_lines.append(i)

    if bad_lines:
        # Only warn if a significant portion is broken
        if len(bad_lines) == total_lines and total_lines > 0:
            report.findings.append(Finding(
                severity=Severity.WARNING,
                file=file_path,
                message=f"JSONL file has no valid lines ({total_lines} total)",
            ))
        elif len(bad_lines) <= 3:
            report.findings.append(Finding(
                severity=Severity.INFO,
                file=file_path,
                message=f"JSONL file has {len(bad_lines)} unparseable line(s) of {total_lines}",
                detail=f"Bad lines: {bad_lines[:5]}",
            ))
        else:
            report.findings.append(Finding(
                severity=Severity.WARNING,
                file=file_path,
                message=f"JSONL file has {len(bad_lines)} unparseable lines of {total_lines}",
                detail=f"First bad lines: {bad_lines[:5]}",
            ))
    else:
        report.files_healthy += 1


def _scan_markdown_file(file_path: Path, report: ScanReport) -> None:
    """Validate a markdown file for corruption or structural issues."""
    report.files_scanned += 1

    corruption = _check_binary_corruption(file_path)
    if corruption:
        report.findings.append(Finding(
            severity=Severity.WARNING,
            file=file_path,
            message="Binary corruption in markdown file",
            detail=corruption,
        ))
        return

    if file_path.stat().st_size == 0:
        # Core identity files being empty is a warning
        from .config import WORKSPACE_MARKDOWN_FILES
        severity = (
            Severity.WARNING
            if file_path.name in WORKSPACE_MARKDOWN_FILES
            else Severity.INFO
        )
        report.findings.append(Finding(
            severity=severity,
            file=file_path,
            message="Markdown file is empty",
            detail=f"{file_path.name.upper()} has no content — agent identity/behavior may be undefined.",
        ))
        return

    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        report.findings.append(Finding(
            severity=Severity.WARNING,
            file=file_path,
            message="Encoding error in markdown file",
            detail=str(e),
        ))
        return

    # Check for truncation indicators
    if content.rstrip().endswith("\\"):
        report.findings.append(Finding(
            severity=Severity.WARNING,
            file=file_path,
            message="File appears truncated (ends with backslash)",
        ))

    # Check for suspiciously long lines (possible paste corruption)
    for i, line in enumerate(content.splitlines(), 1):
        if len(line) > 10_000:
            report.findings.append(Finding(
                severity=Severity.INFO,
                file=file_path,
                message=f"Extremely long line ({len(line)} chars)",
                line=i,
                detail="May indicate paste corruption or binary data in text file.",
            ))
            break  # One warning is enough

    report.files_healthy += 1


def _scan_sessions(sessions_dir: Path, report: ScanReport) -> None:
    """Scan session files for health."""
    session_files = sorted(sessions_dir.glob("**/*.json"))
    jsonl_files = sorted(sessions_dir.glob("**/*.jsonl"))
    all_files = session_files + jsonl_files

    if not all_files:
        report.findings.append(Finding(
            severity=Severity.INFO,
            file=sessions_dir,
            message="No session files found",
            detail="This is fine for a fresh install.",
        ))
        return

    for sf in all_files:
        report.files_scanned += 1

        # Check for binary corruption first
        corruption = _check_binary_corruption(sf)
        if corruption:
            report.findings.append(Finding(
                severity=Severity.CRITICAL,
                file=sf,
                message="Binary corruption detected in session file",
                detail=corruption,
            ))
            continue

        # Try standard JSON first
        findings_before = len(report.findings)
        data = _try_parse_json(sf, report, critical=False)

        if data is not None:
            report.files_healthy += 1
            # Session-specific checks
            if isinstance(data, dict):
                size_mb = sf.stat().st_size / (1024 * 1024)
                if size_mb > 50:
                    report.findings.append(Finding(
                        severity=Severity.WARNING,
                        file=sf,
                        message=f"Session file is very large ({size_mb:.1f} MB)",
                        detail="May cause slow loading or memory issues.",
                    ))
            continue

        # If parse failed with "Extra data", treat as JSONL
        new_findings = report.findings[findings_before:]
        has_extra_data = any(
            f.detail and "Extra data" in f.detail for f in new_findings
        )
        if has_extra_data or sf.suffix.lower() == ".jsonl":
            del report.findings[findings_before:]
            _scan_jsonl_file(sf, report)


def _scan_skills(
    skills_dir: Path, layout: WorkspaceLayout, report: ScanReport
) -> None:
    """Scan installed skills for structural issues."""
    if not skills_dir.is_dir():
        return

    skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
    for skill_dir in sorted(skill_dirs):
        # Each skill should have at least a recognizable entry point
        has_readme = any(
            (skill_dir / name).exists()
            for name in ["README.md", "SKILL.md", "skill.md", "readme.md"]
        )
        has_code = any(skill_dir.glob("*.py")) or any(skill_dir.glob("*.js"))
        has_json = any(skill_dir.glob("*.json"))

        if not (has_readme or has_code or has_json):
            report.findings.append(Finding(
                severity=Severity.INFO,
                file=skill_dir,
                message=f"Skill directory '{skill_dir.name}' has no recognizable entry point",
            ))


def _scan_cross_references(layout: WorkspaceLayout, report: ScanReport) -> None:
    """Check for orphaned references between config and filesystem."""
    if not layout.config_file or not layout.config_file.is_file():
        return

    try:
        with open(layout.config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return  # Already reported by earlier scan

    if not isinstance(config, dict):
        return

    # Check workspace path reference
    ws_path = config.get("workspace") or config.get("workspaceDir")
    if ws_path:
        resolved = Path(ws_path).expanduser()
        if not resolved.is_absolute():
            resolved = layout.root / resolved
        if not resolved.exists():
            report.findings.append(Finding(
                severity=Severity.CRITICAL,
                file=layout.config_file,
                message=f"Configured workspace path does not exist: {ws_path}",
                detail="OpenClaw will fail to load workspace resources.",
            ))


# --- Low-level helpers ---

def _try_parse_json(
    file_path: Path, report: ScanReport, critical: bool = False
) -> Optional[Any]:
    """Attempt to parse a JSON file, recording findings on failure."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError as e:
        report.findings.append(Finding(
            severity=Severity.CRITICAL if critical else Severity.WARNING,
            file=file_path,
            message="Cannot read file — encoding error",
            detail=str(e),
        ))
        return None
    except OSError as e:
        report.findings.append(Finding(
            severity=Severity.CRITICAL if critical else Severity.WARNING,
            file=file_path,
            message=f"Cannot read file — OS error",
            detail=str(e),
        ))
        return None

    # Detect truncated writes: file ends mid-structure
    stripped = content.rstrip()
    if stripped and stripped[-1] not in "]}\"0123456789truefalsn":
        report.findings.append(Finding(
            severity=Severity.CRITICAL if critical else Severity.WARNING,
            file=file_path,
            message="JSON appears truncated (unexpected final character)",
            detail=f"File ends with: ...{stripped[-20:]!r}",
        ))

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        report.findings.append(Finding(
            severity=Severity.CRITICAL if critical else Severity.WARNING,
            file=file_path,
            message="JSON parse error",
            detail=f"Line {e.lineno}, col {e.colno}: {e.msg}",
            line=e.lineno,
        ))
        return None


def _check_binary_corruption(file_path: Path) -> Optional[str]:
    """Check if a text file contains null bytes or other binary indicators."""
    try:
        with open(file_path, "rb") as f:
            # Read first 64KB to check for corruption
            chunk = f.read(65536)
    except OSError:
        return None

    null_count = chunk.count(b"\x00")
    if null_count > 0:
        return (
            f"Found {null_count} null byte(s) in first 64KB — "
            "likely truncated write or binary corruption"
        )

    # Check for common corruption patterns
    if b"\xff\xfe" in chunk[:4] or b"\xfe\xff" in chunk[:4]:
        # BOM markers in what should be UTF-8
        return "Unexpected BOM marker — file may have encoding issues"

    return None


def _is_under(path: Path, parent: Path) -> bool:
    """Check if path is under parent directory."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_for_null_values(
    file_path: Path,
    data: dict,
    report: ScanReport,
    path_prefix: str,
) -> None:
    """Recursively check for null values in critical config positions."""
    for key, value in data.items():
        current_path = f"{path_prefix}.{key}" if path_prefix else key
        if value is None and key in {"model", "provider", "apiKey", "workspace"}:
            report.findings.append(Finding(
                severity=Severity.WARNING,
                file=file_path,
                message=f"Config key '{current_path}' is null",
                detail="This may cause runtime errors.",
            ))
        elif isinstance(value, dict):
            _check_for_null_values(file_path, value, report, current_path)
