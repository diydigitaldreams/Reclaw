# ReClaw 🦞🔧

**Out-of-band recovery engine for [OpenClaw](https://github.com/openclaw/openclaw) workspaces.**

When your AI agent's brain gets corrupted — broken JSON configs, truncated session files, mangled identity docs — ReClaw is the search-and-rescue team that goes in, finds the survivors, and rebuilds the infrastructure so your assistant can wake up again.

## Why ReClaw?

OpenClaw workspaces accumulate state: session histories, tool-output logs, skill configs, identity files. If a process crashes mid-write or a model hallucinates a breaking change to its own config, the system can enter a death spiral where the agent can't even boot up to fix itself.

ReClaw operates **out-of-band** — it's a standalone Python tool with zero dependency on OpenClaw's Node.js runtime. If OpenClaw is down, ReClaw still runs.

## Install

```bash
pip install -e .
# or when published:
# pip install reclaw

# For real-time file monitoring (recommended):
pip install -e ".[watch]"
```

## Commands

### `reclaw status`
Quick health check — one command to see if your workspace is bootable.

```bash
reclaw status
reclaw status --path /path/to/.openclaw
```

### `reclaw scan`
Deep surgical scan of every JSON, JSONL, Markdown, and config file. Checks for:
- JSON parse errors and truncated writes
- JSONL (JSON Lines) validation — including `.json` session files written in JSONL format
- Null bytes / binary corruption in text files
- Empty identity files (AGENTS.md, SOUL.md, USER.md)
- Orphaned references between config and filesystem
- Oversized session files
- Broken workspace path references

Understands both flat (`sessions/`) and agent-based (`agents/*/sessions/`) OpenClaw layouts.

```bash
reclaw scan
reclaw scan --verbose     # include info-level findings
```

### `reclaw reindex`
Rebuild the workspace structural map from raw files on disk. Ignores all existing pointers — discovers everything fresh from the filesystem.

```bash
reclaw reindex
reclaw reindex --output index.json   # save the map to a file
```

### `reclaw snapshot`
Create a known-good checkpoint. By default, refuses to snapshot a broken workspace (so you don't overwrite a good backup with a bad one).

```bash
reclaw snapshot
reclaw snapshot --note "before upgrading openclaw"
reclaw snapshot --force   # snapshot even if scan finds issues
```

### `reclaw snapshots`
List all available recovery snapshots.

```bash
reclaw snapshots
```

### `reclaw restore`
Restore workspace files from a snapshot. Defaults to the most recent clean snapshot.

```bash
reclaw restore --dry-run          # preview what would be restored
reclaw restore                    # restore from latest clean snapshot
reclaw restore --snapshot-id 20260318_142530   # restore specific snapshot
```

### `reclaw watch`
Background monitor that auto-snapshots when the workspace is healthy. Detects file changes, waits for them to settle, scans for corruption, and creates a recovery checkpoint if everything is clean. Alerts immediately if critical issues are detected.

```bash
reclaw watch                          # start with defaults
reclaw watch --cooldown 60            # wait 60s after last change before scanning
reclaw watch --interval 600           # minimum 10 min between snapshots
reclaw watch --max-interval 1800      # force a check every 30 min
reclaw watch --no-events              # force polling mode (no watchdog)
```

With `watchdog` installed (`pip install reclaw[watch]`), it uses real-time filesystem events. Without it, falls back to polling every 10 seconds — still works, just slightly less responsive.

## How It Works

**Scanner** — Walks the workspace and validates every file. JSON files are parse-tested. JSONL files (including `.json` session files written in JSON Lines format) are validated line-by-line. Text files are checked for binary corruption. Config files are validated against known schema patterns. Cross-references between config and filesystem are verified.

**Re-indexer** — Rebuilds the complete structural map from scratch. Discovers sessions, skills, identity files, and config without trusting any existing index. Produces a manifest that shows exactly what's on disk.

**Snapshot Engine** — Maintains timestamped backups of all critical files in `~/.openclaw/.reclaw/snapshots/`. Automatically prunes old snapshots. Restore copies files back into place from the backup.

**Watcher** — Background daemon that monitors the workspace for changes. After changes settle (configurable cooldown), runs a health scan and auto-snapshots if clean. Uses watchdog for real-time filesystem events when available, falls back to polling otherwise.

## Project Structure

```
src/reclaw/
├── cli.py          # Click CLI entry point
├── config.py       # Workspace discovery and path resolution
├── scanner.py      # File validation and corruption detection
├── reindexer.py    # Filesystem-based structural mapping
├── snapshot.py     # Checkpoint creation and restore
└── watcher.py      # Background filesystem monitor
```

## Development

```bash
pip install -e ".[dev]"
pytest
pytest --cov=reclaw
```

## License

MIT — DIY Digital Dreams
