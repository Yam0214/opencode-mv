"""opencode-mv: Move a project directory and update opencode database to maintain session continuity."""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from typing import NamedTuple

DEFAULT_DB_PATH = os.path.join(os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db")
GLOBAL_PROJECT_ID = "global"

REQUIRED_TABLES = {
    "project": ["id", "worktree"],
    "session": ["id", "project_id", "directory", "path"],
}

PROTECTED_PATHS = {
    "/",
    os.path.expanduser("~"),
    "/home",
    "/root",
    "/usr",
    "/etc",
    "/var",
    "/tmp",
    "/bin",
    "/sbin",
    "/lib",
    "/opt",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/snap",
}

if sys.platform == "darwin":
    PROTECTED_PATHS.update({
        "/Applications",
        "/System",
        "/Users",
        "/Library",
    })

BACKUP_KEEP_COUNT = 7


class Change(NamedTuple):
    """Represents a single database change to be applied."""

    change_type: str  # "project" or "session"
    entity_id: str
    old_directory: str
    new_directory: str
    old_path: str | None = None  # original session.path value (only for session changes)


def normalize_dir(path: str) -> str:
    """Normalize a directory path for consistent comparison."""
    return os.path.normpath(path).rstrip("/")


def is_protected_path(path: str) -> bool:
    """Check if a path is protected from destructive operations."""
    normalized = normalize_dir(os.path.realpath(path))
    for protected in PROTECTED_PATHS:
        if normalized == normalize_dir(os.path.realpath(protected)):
            return True
    return False


def backup_database(db_path: str, verbose: bool = False) -> str | None:
    """Create a timestamped backup of the database. Keeps last BACKUP_KEEP_COUNT backups."""
    if not os.path.exists(db_path):
        return None

    db_dir = os.path.dirname(db_path)
    db_name = os.path.basename(db_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = os.path.join(db_dir, f"{db_name}.backup.{timestamp}")

    try:
        shutil.copy2(db_path, backup_path)
        log(f"Database backed up to: {backup_path}", verbose=verbose)
    except OSError as e:
        print(f"Warning: failed to back up database: {e}", file=sys.stderr)
        return None

    # Rotate old backups, keep only the most recent BACKUP_KEEP_COUNT
    pattern = os.path.join(db_dir, f"{db_name}.backup.*")
    backups = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    # Only consider files matching the timestamp pattern to avoid deleting unrelated files
    timestamp_re = re.compile(rf"^{re.escape(db_name)}\.backup\.\d{{8}}_\d{{6}}_\d{{6}}$")
    backups = [b for b in backups if timestamp_re.match(os.path.basename(b))]
    for old_backup in backups[BACKUP_KEEP_COUNT:]:
        try:
            os.remove(old_backup)
            log(f"Removed old backup: {old_backup}", verbose=verbose)
        except OSError:
            pass  # best effort

    return backup_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move a project directory and update opencode database to maintain session continuity."
    )
    parser.add_argument("old_path", help="Current project path")
    parser.add_argument("new_path", help="New project path")
    parser.add_argument("-f", "--force", action="store_true", help="Overwrite if destination exists")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be changed without making changes")
    return parser.parse_args(argv)


def log(msg: str, verbose: bool = False) -> None:
    if not verbose:
        return
    print(msg)


def validate_schema(conn: sqlite3.Connection) -> bool:
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}

    for table, columns in REQUIRED_TABLES.items():
        if table not in tables:
            print(f"Error: database schema validation failed: table '{table}' is missing", file=sys.stderr)
            return False

        # PRAGMA does not support ? parameters; table name is from hardcoded dict, safe to interpolate
        cursor.execute(f"PRAGMA table_info('{table}')")
        existing_columns = {row[1] for row in cursor.fetchall()}

        for col in columns:
            if col not in existing_columns:
                print(f"Error: database schema validation failed: column '{table}.{col}' is missing", file=sys.stderr)
                return False

    return True


def find_project_by_worktree(conn: sqlite3.Connection, old_path: str) -> tuple[str | None, str | None]:
    cursor = conn.cursor()
    old_path_normalized = normalize_dir(old_path)

    cursor.execute('SELECT id, worktree FROM project WHERE worktree = ?', (old_path,))
    result = cursor.fetchone()
    if result:
        return result[0], result[1]

    cursor.execute('SELECT id, worktree FROM project')
    for row in cursor.fetchall():
        if normalize_dir(row[1]) == old_path_normalized:
            return row[0], row[1]

    return None, None


def find_sessions_by_project(conn: sqlite3.Connection, project_id: str) -> list[tuple]:
    cursor = conn.cursor()
    cursor.execute('SELECT id, project_id, directory, path FROM session WHERE project_id = ?', (project_id,))
    return cursor.fetchall()


def find_global_sessions_by_path(conn: sqlite3.Connection, old_path: str) -> list[tuple]:
    cursor = conn.cursor()
    old_path_normalized = normalize_dir(old_path)

    cursor.execute(
        'SELECT id, project_id, directory, path FROM session WHERE project_id = ?',
        (GLOBAL_PROJECT_ID,),
    )
    results = []
    for row in cursor.fetchall():
        dir_normalized = normalize_dir(row[2]) if row[2] else ""
        path_normalized = normalize_dir(row[3]) if row[3] else ""
        if (
            dir_normalized == old_path_normalized
            or path_normalized == old_path_normalized
        ):
            results.append(row)
    return results


def compute_new_session_path(new_path: str) -> str:
    """Compute the session.path value for a new path (leading-slash-stripped)."""
    result = os.path.relpath(new_path, "/")
    if result == ".":
        return ""
    return result


def collect_changes(
    conn: sqlite3.Connection, old_path: str, new_path: str, verbose: bool = False
) -> tuple[str | None, list[Change]]:
    project_id, current_worktree = find_project_by_worktree(conn, old_path)
    changes: list[Change] = []

    if project_id:
        log(f"Found project: {project_id} (worktree: {current_worktree})", verbose=verbose)
        sessions = find_sessions_by_project(conn, project_id)
        for s in sessions:
            changes.append(Change(
                change_type="session",
                entity_id=s[0],
                old_directory=s[2],
                new_directory=new_path,
                old_path=s[3],
            ))
        changes.append(Change(
            change_type="project",
            entity_id=project_id,
            old_directory=current_worktree or "",
            new_directory=new_path,
        ))
    else:
        sessions = find_global_sessions_by_path(conn, old_path)
        for s in sessions:
            changes.append(Change(
                change_type="session",
                entity_id=s[0],
                old_directory=s[2],
                new_directory=new_path,
                old_path=s[3],
            ))

    return project_id, changes


def apply_changes(
    conn: sqlite3.Connection, changes: list[Change], target_path: str, verbose: bool = False
) -> None:
    cursor = conn.cursor()
    new_session_path = compute_new_session_path(target_path)

    for change in changes:
        if change.change_type == "project":
            cursor.execute('UPDATE project SET worktree = ? WHERE id = ?', (change.new_directory, change.entity_id))
            log(f"Updated project.worktree -> {change.new_directory}", verbose=verbose)
        elif change.change_type == "session":
            if change.old_path is None:
                path_val = None
            elif change.old_path == "":
                path_val = ""
            else:
                path_val = new_session_path
            cursor.execute(
                'UPDATE session SET directory = ?, path = ? WHERE id = ?',
                (change.new_directory, path_val, change.entity_id),
            )
            log(f"Updated session {change.entity_id[:20]}... directory -> {change.new_directory}", verbose=verbose)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    old_path = os.path.abspath(os.path.expanduser(args.old_path))
    new_path = os.path.abspath(os.path.expanduser(args.new_path))

    if not os.path.exists(old_path):
        print(f"Error: '{old_path}' does not exist", file=sys.stderr)
        return 1

    if not os.path.isdir(old_path):
        print(f"Error: '{old_path}' is not a directory", file=sys.stderr)
        return 1

    # Prevent moving to the same path
    if normalize_dir(old_path) == normalize_dir(new_path):
        print(f"Error: old and new paths are the same", file=sys.stderr)
        return 1

    # Prevent moving a directory into itself or a subdirectory of itself (check BEFORE any destructive ops)
    if new_path.startswith(old_path + os.sep):
        print(f"Error: cannot move '{old_path}' into a subdirectory of itself ('{new_path}')", file=sys.stderr)
        return 1

    # Guard against moving to protected paths (unconditional, not just with --force)
    if is_protected_path(new_path):
        print(f"Error: refusing to move to protected path '{new_path}'", file=sys.stderr)
        return 1

    db_path = os.environ.get("OPENCODE_DB_PATH", DEFAULT_DB_PATH)
    if not os.path.exists(db_path):
        print(f"Error: opencode database not found at '{db_path}'", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    try:
        if not validate_schema(conn):
            print("Error: database schema is incompatible with this version of opencode-mv", file=sys.stderr)
            return 1

        project_id, changes = collect_changes(conn, old_path, new_path, verbose=args.verbose)
    finally:
        conn.close()

    if args.dry_run:
        print("=== Dry Run ===")
        print(f"Would move: {old_path} -> {new_path}")
        if not changes:
            print("No database updates needed.")
        else:
            print("Would update database:")
            for change in changes:
                if change.change_type == "project":
                    print(f"  project.worktree: {change.old_directory} -> {change.new_directory}")
                else:
                    print(f"  session ({change.change_type}): {change.old_directory} -> {change.new_directory}")
        return 0

    if os.path.exists(new_path):
        if args.force:
            log(f"Removing existing destination: {new_path}", verbose=args.verbose)
            if os.path.isfile(new_path):
                os.remove(new_path)
            else:
                shutil.rmtree(new_path)
        else:
            print(f"Error: '{new_path}' already exists. Use --force to overwrite.", file=sys.stderr)
            return 1

    # Back up database before modifying it
    backup_path = backup_database(db_path, verbose=args.verbose)

    # Update database first, then move directory
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            apply_changes(conn, changes, new_path, verbose=args.verbose)
        log("Database updated successfully", verbose=args.verbose)
    except sqlite3.Error as e:
        print(f"Error updating database: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    # Move directory
    log(f"Moving: {old_path} -> {new_path}", verbose=args.verbose)
    try:
        shutil.move(old_path, new_path)
        log("Directory moved successfully", verbose=args.verbose)
    except OSError as e:
        print(f"Error moving directory: {e}", file=sys.stderr)
        print(f"Warning: database updated but directory move failed. Attempting to roll back database...", file=sys.stderr)

        # Roll back database changes
        rollback_changes = [
            Change(
                change_type=change.change_type,
                entity_id=change.entity_id,
                old_directory=change.new_directory,
                new_directory=change.old_directory,
                old_path=change.old_path,
            )
            for change in changes
        ]

        conn = sqlite3.connect(db_path)
        try:
            with conn:
                apply_changes(conn, rollback_changes, old_path, verbose=args.verbose)
            print(f"Database rolled back successfully", file=sys.stderr)
        except sqlite3.Error as rollback_err:
            print(f"Critical: database rollback failed: {rollback_err}", file=sys.stderr)
            print(f"Database now points to '{new_path}' but directory is still at '{old_path}'", file=sys.stderr)
            if backup_path:
                print(f"A database backup is available at: {backup_path}", file=sys.stderr)
        finally:
            conn.close()
        return 1

    log("Done", verbose=args.verbose)
    return 0
