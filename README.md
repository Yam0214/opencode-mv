# opencode-mv

Move a project directory and update the opencode database to maintain session continuity.

When you `mv` a project directory manually, opencode loses track of it — sessions point to the old path and become inaccessible. This tool moves the directory and patches the database in one step, with rollback support if the move fails.

## Usage

```bash
opencode-mv <old-path> <new-path>
```

## Options

- `--dry-run` — Preview changes without modifying anything
- `-f, --force` — Delete destination if it exists, then move (destructive)
- `-v, --verbose` — Show detailed output
- `-h, --help` — Show help

## Behavior

1. **Validate** that the old path exists and the database schema is compatible
2. **Guard** against destructive operations (protected paths, same-path moves, moving into a subdirectory of itself)
3. **Back up** the database before any changes
4. **Update** `project.worktree` and `session.directory` / `session.path` in the database
5. **Move** the directory from old path to new path
6. **Rollback** database changes automatically if the directory move fails

All database changes are wrapped in a transaction. If the directory move fails after the database has been updated, the tool automatically rolls back the database to its original state. A database backup is also created before any modifications for additional safety.

## Safety Features

- **Protected paths**: Refuses to move to system paths like `/`, `/usr`, `/etc`, or the home directory (even without `--force`)
- **Symlink resolution**: Resolves symbolic links before checking protected paths
- **Same-path guard**: Rejects attempts to move a directory to itself
- **Subdirectory guard**: Prevents moving a directory into a subdirectory of itself
- **Atomic-ish updates**: Database is updated before the filesystem move, with automatic rollback on failure
- **Backups**: Timestamped database backups are kept (last 7 by default)

## Installation

### uv (recommended)

```bash
uv tool install git+<repo-url>
# or from a local clone:
uv tool install .
```

### pipx

```bash
pipx install git+<repo-url>
# or from a local clone:
pipx install .
```

### pip (editable)

```bash
git clone <repo-url>
cd opencode-mv
pip install -e .
```

## Requirements

- Python 3.10+

## Uninstall

```bash
uv tool uninstall opencode-mv
# or:
pipx uninstall opencode-mv
# or:
pip uninstall opencode-mv
```

## Examples

```bash
# Move a project
opencode-mv ~/projects/old-name ~/projects/new-name

# Preview what would change
opencode-mv --dry-run ~/projects/old-name ~/projects/new-name

# Force-overwrite an existing destination
opencode-mv -f ~/projects/old-name ~/projects/new-name
```

## Recovery

If a catastrophic failure occurs (database updated but move failed and rollback also failed), a database backup is available. The backup path is printed in the error output.

## License

MIT
