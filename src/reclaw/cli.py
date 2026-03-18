"""
ReClaw CLI — out-of-band recovery engine for OpenClaw workspaces.

Usage:
    reclaw scan [--path PATH]
    reclaw reindex [--path PATH] [--output FILE]
    reclaw snapshot [--path PATH] [--note TEXT] [--force]
    reclaw snapshots [--path PATH]
    reclaw restore [--path PATH] [--snapshot-id ID] [--dry-run]
    reclaw status [--path PATH]
"""

from __future__ import annotations

import re as _re
from pathlib import Path

import click

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from . import __version__
from .config import discover_workspace
from .reindexer import reindex_workspace
from .scanner import Severity, scan_workspace
from .snapshot import (
    SnapshotError,
    create_snapshot,
    list_snapshots,
    restore_snapshot,
)
from .watcher import WatchConfig, watch_workspace


# --- Output helpers that degrade gracefully without rich ---

def _strip_markup(text: str) -> str:
    return _re.sub(r"\[/?[^\]]*\]", "", str(text))


class _FallbackConsole:
    def print(self, text="", **kwargs):
        print(_strip_markup(text))


console = Console() if HAS_RICH else _FallbackConsole()


def _print_panel(content: str, title: str = "", border_style: str = "") -> None:
    if HAS_RICH:
        console.print(Panel(content, title=title, border_style=border_style))
    else:
        sep = "=" * 50
        console.print(f"\n{sep}")
        if title:
            console.print(f"  {title}")
            console.print(sep)
        console.print(content)
        console.print(sep)


def _print_findings_table(findings, layout_root: Path, verbose: bool) -> None:
    if HAS_RICH:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Severity", width=10)
        table.add_column("File", style="dim")
        table.add_column("Issue")
        for finding in findings:
            if not verbose and finding.severity == Severity.INFO:
                continue
            severity_style = {
                Severity.CRITICAL: "bold red",
                Severity.WARNING: "yellow",
                Severity.INFO: "dim",
            }[finding.severity]
            file_display = _short_path(finding.file, layout_root)
            if finding.line:
                file_display += f":{finding.line}"
            message = finding.message
            if finding.detail and verbose:
                message += f"\n[dim]{finding.detail}[/dim]"
            table.add_row(
                f"[{severity_style}]{finding.severity.value.upper()}[/]",
                file_display,
                message,
            )
        console.print(table)
    else:
        for finding in findings:
            if not verbose and finding.severity == Severity.INFO:
                continue
            file_display = _short_path(finding.file, layout_root)
            if finding.line:
                file_display += f":{finding.line}"
            print(f"  {finding.severity.value.upper():10s} {file_display}")
            print(f"             {finding.message}")
            if finding.detail and verbose:
                print(f"             {finding.detail}")


def _print_snapshots_table(snaps) -> None:
    if HAS_RICH:
        table = Table(show_header=True, header_style="bold")
        table.add_column("ID", style="cyan")
        table.add_column("Created")
        table.add_column("Files", justify="right")
        table.add_column("Clean")
        table.add_column("Note")
        for snap in snaps:
            clean = "[green]✓[/]" if snap.scan_was_clean else "[yellow]✗[/]"
            table.add_row(
                snap.snapshot_id,
                snap.created_at.strftime("%Y-%m-%d %H:%M UTC"),
                str(len(snap.files)),
                clean,
                snap.note or "",
            )
        console.print(table)
    else:
        fmt = "  {:<20s} {:<22s} {:>5s} {:>5s} {}"
        print(fmt.format("ID", "Created", "Files", "Clean", "Note"))
        print("  " + "-" * 70)
        for snap in snaps:
            clean = "yes" if snap.scan_was_clean else "no"
            print(fmt.format(
                snap.snapshot_id,
                snap.created_at.strftime("%Y-%m-%d %H:%M UTC"),
                str(len(snap.files)),
                clean,
                snap.note or "",
            ))


# --- CLI commands ---

@click.group()
@click.version_option(version=__version__, prog_name="reclaw")
def main():
    """ReClaw — out-of-band recovery engine for OpenClaw workspaces. 🦞🔧"""
    pass


@main.command()
@click.option("--path", "-p", type=click.Path(exists=False), default=None,
              help="Path to OpenClaw workspace (default: auto-discover)")
@click.option("--verbose", "-v", is_flag=True, help="Show all findings including info-level")
def scan(path: str | None, verbose: bool):
    """Scan the workspace for corruption, broken configs, and logic errors."""
    layout = discover_workspace(Path(path) if path else None)
    if not layout.is_valid:
        console.print(f"[bold red]✗[/] Could not find a valid OpenClaw workspace at {layout.root}")
        raise SystemExit(1)

    console.print(f"[bold]Scanning workspace:[/] {layout.root}\n")
    report = scan_workspace(layout)

    if report.findings:
        _print_findings_table(report.findings, layout.root, verbose)
        console.print()

    if report.is_bootable:
        _print_panel(
            f"[bold green]✓ BOOTABLE[/]\n{report.summary()}",
            title="Scan Result", border_style="green",
        )
    else:
        _print_panel(
            f"[bold red]✗ BROKEN[/]\n{report.summary()}\n\n"
            "Run [bold]reclaw restore[/] to recover from the last good snapshot.",
            title="Scan Result", border_style="red",
        )


@main.command()
@click.option("--path", "-p", type=click.Path(exists=False), default=None)
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Write index to this file (default: print to stdout)")
def reindex(path: str | None, output: str | None):
    """Rebuild the workspace structural map from raw files on disk."""
    layout = discover_workspace(Path(path) if path else None)
    if not layout.root.exists():
        console.print(f"[bold red]✗[/] Workspace not found at {layout.root}")
        raise SystemExit(1)

    console.print(f"[bold]Re-indexing workspace:[/] {layout.root}\n")
    index = reindex_workspace(layout)

    console.print(f"  Config parseable:  {'[green]yes[/]' if index.config_parseable else '[red]no[/]'}")
    console.print(f"  Identity files:    {len(index.identity_files)}")
    console.print(f"  Skills discovered: {len(index.skills)}")
    console.print(f"  Sessions found:    {len(index.sessions)}")
    console.print(f"  Orphaned files:    {len(index.orphaned_files)}")

    if index.skills:
        console.print("\n[bold]Skills:[/]")
        for skill in index.skills:
            status = "[green]✓[/]" if skill.has_code or skill.has_readme else "[yellow]?[/]"
            console.print(f"  {status} {skill.name}")

    if index.sessions:
        console.print("\n[bold]Sessions:[/]")
        for sess in index.sessions[:10]:
            status = "[green]✓[/]" if sess.parseable else "[red]✗[/]"
            id_display = sess.session_id or sess.file.stem
            model = sess.model or "unknown"
            console.print(f"  {status} {id_display} ({model}, {sess.message_count} msgs)")
        if len(index.sessions) > 10:
            console.print(f"  ... and {len(index.sessions) - 10} more")

    if index.orphaned_files:
        console.print("\n[bold yellow]Orphaned files:[/]")
        for f in index.orphaned_files:
            console.print(f"  [dim]{_short_path(f, layout.root)}[/]")

    if output:
        output_path = Path(output)
        index.save(output_path)
        console.print(f"\n[green]Index written to {output_path}[/]")


@main.command()
@click.option("--path", "-p", type=click.Path(exists=False), default=None)
@click.option("--note", "-n", default="", help="Annotation for this snapshot")
@click.option("--force", is_flag=True, help="Snapshot even if scan finds critical issues")
def snapshot(path: str | None, note: str, force: bool):
    """Create a known-good checkpoint of the workspace."""
    layout = discover_workspace(Path(path) if path else None)
    if not layout.is_valid:
        console.print(f"[bold red]✗[/] No valid workspace at {layout.root}")
        raise SystemExit(1)

    console.print(f"[bold]Creating snapshot of:[/] {layout.root}\n")
    try:
        snap = create_snapshot(layout, note=note, force=force)
    except SnapshotError as e:
        console.print(f"[bold red]✗[/] {e}")
        raise SystemExit(1)

    status = "[green]clean[/]" if snap.scan_was_clean else "[yellow]forced (unclean)[/]"
    _print_panel(
        f"[bold green]✓ Snapshot created[/]\n\n"
        f"  ID:      {snap.snapshot_id}\n"
        f"  Files:   {len(snap.files)}\n"
        f"  Status:  {status}\n"
        f"  Stored:  {snap.snapshot_dir}",
        title="Snapshot", border_style="green",
    )


@main.command(name="snapshots")
@click.option("--path", "-p", type=click.Path(exists=False), default=None)
def list_snaps(path: str | None):
    """List all available recovery snapshots."""
    layout = discover_workspace(Path(path) if path else None)
    snaps = list_snapshots(layout)
    if not snaps:
        console.print("[yellow]No snapshots found.[/] Run [bold]reclaw snapshot[/] to create one.")
        return
    _print_snapshots_table(snaps)


@main.command()
@click.option("--path", "-p", type=click.Path(exists=False), default=None)
@click.option("--snapshot-id", "-s", default=None, help="Restore from this specific snapshot")
@click.option("--dry-run", is_flag=True, help="Show what would be restored without writing")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def restore(path: str | None, snapshot_id: str | None, dry_run: bool, yes: bool):
    """Restore the workspace from a known-good snapshot."""
    layout = discover_workspace(Path(path) if path else None)
    if dry_run:
        console.print("[bold]DRY RUN — no files will be modified\n[/]")
    elif not yes:
        click.confirm("This will overwrite current workspace files. Continue?", abort=True)

    result = restore_snapshot(layout, snapshot_id=snapshot_id, dry_run=dry_run)

    if result.success:
        _print_panel(
            f"[bold green]✓ Restored from snapshot {result.snapshot_id}[/]\n\n"
            f"  Files restored: {len(result.files_restored)}\n"
            f"  Files skipped:  {len(result.files_skipped)}",
            title="Restore Complete" if not dry_run else "Dry Run Complete",
            border_style="green",
        )
    else:
        _print_panel(
            f"[bold red]✗ Restore failed[/]\n\n"
            + "\n".join(f"  {e}" for e in result.errors),
            title="Restore Failed", border_style="red",
        )
        raise SystemExit(1)

    if result.files_restored and dry_run:
        console.print("\n[bold]Would restore:[/]")
        for f in result.files_restored:
            console.print(f"  {f}")


@main.command()
@click.option("--path", "-p", type=click.Path(exists=False), default=None)
def status(path: str | None):
    """Quick health check — scan + snapshot status in one command."""
    layout = discover_workspace(Path(path) if path else None)
    console.print(f"[bold]OpenClaw workspace:[/] {layout.root}\n")

    if not layout.is_valid:
        console.print("[bold red]✗ No valid workspace found[/]")
        raise SystemExit(1)

    console.print(f"  Config:     {'[green]found[/]' if layout.config_file else '[red]missing[/]'}")
    console.print(f"  Workspace:  {'[green]found[/]' if layout.workspace_dir else '[yellow]not set[/]'}")
    console.print(f"  Sessions:   {'[green]found[/]' if layout.sessions_dir else '[dim]none[/]'}")
    console.print(f"  Skills:     {'[green]found[/]' if layout.skills_dir else '[dim]none[/]'}")

    console.print("\n[bold]Health scan:[/]")
    report = scan_workspace(layout)
    if report.is_bootable:
        console.print(
            f"  [green]✓ Bootable[/] — {report.files_scanned} files scanned, "
            f"{report.critical_count} critical, {report.warning_count} warnings"
        )
    else:
        console.print(f"  [red]✗ Broken[/] — {report.critical_count} critical issue(s)")
        for f in report.findings:
            if f.severity == Severity.CRITICAL:
                console.print(f"    [red]•[/] {f.message} ({_short_path(f.file, layout.root)})")

    snaps = list_snapshots(layout)
    console.print(f"\n[bold]Snapshots:[/]")
    if snaps:
        latest = snaps[0]
        clean = "[green]clean[/]" if latest.scan_was_clean else "[yellow]unclean[/]"
        console.print(f"  Latest: {latest.snapshot_id} ({clean}, {len(latest.files)} files)")
        console.print(f"  Total:  {len(snaps)} snapshot(s)")
    else:
        console.print("  [yellow]None — run 'reclaw snapshot' to create a recovery point[/]")


@main.command()
@click.option("--path", "-p", type=click.Path(exists=False), default=None,
              help="Path to OpenClaw workspace (default: auto-discover)")
@click.option("--cooldown", "-c", default=30, show_default=True,
              help="Seconds to wait after last change before scanning")
@click.option("--interval", "-i", default=300, show_default=True,
              help="Minimum seconds between snapshots")
@click.option("--max-interval", default=3600, show_default=True,
              help="Maximum seconds between snapshots (even without changes)")
@click.option("--poll", default=10, show_default=True,
              help="Polling interval in seconds (when watchdog is not installed)")
@click.option("--no-events", is_flag=True,
              help="Force polling mode even if watchdog is available")
@click.option("--max-snapshots", default=20, show_default=True,
              help="Maximum number of snapshots to keep")
def watch(
    path: str | None,
    cooldown: int,
    interval: int,
    max_interval: int,
    poll: int,
    no_events: bool,
    max_snapshots: int,
):
    """Watch the workspace and auto-snapshot when healthy.

    Monitors for file changes, waits for them to settle, scans for
    corruption, and creates a recovery snapshot if everything is clean.
    Alerts immediately if critical issues are detected.

    \b
    Install watchdog for real-time event monitoring:
        pip install watchdog
    Without it, ReClaw falls back to polling.
    """
    layout = discover_workspace(Path(path) if path else None)

    if not layout.is_valid:
        console.print(f"[bold red]✗[/] No valid workspace at {layout.root}")
        raise SystemExit(1)

    def _rich_status(msg: str) -> None:
        console.print(msg)

    def _rich_alert(severity: str, msg: str) -> None:
        style = "bold red" if severity == "critical" else "yellow"
        console.print(f"\n[{style}]⚠️  ALERT [{severity.upper()}][/]: {msg}\n")

    watch_config = WatchConfig(
        cooldown=cooldown,
        min_snapshot_interval=interval,
        max_snapshot_interval=max_interval,
        poll_interval=poll,
        use_events=not no_events,
        max_snapshots=max_snapshots,
        on_status=_rich_status,
        on_alert=_rich_alert,
    )

    watch_workspace(layout, watch_config)


def _short_path(file_path: Path, root: Path) -> str:
    try:
        return str(file_path.relative_to(root))
    except ValueError:
        return str(file_path)


if __name__ == "__main__":
    main()
