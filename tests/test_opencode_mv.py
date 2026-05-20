import glob
import os
import sqlite3

import pytest

from opencode_mv.main import (
    Change,
    apply_changes,
    collect_changes,
    find_global_sessions_by_path,
    find_project_by_worktree,
    find_sessions_by_project,
    is_protected_path,
    backup_database,
    main,
    normalize_dir,
    compute_new_session_path,
    validate_schema,
    BACKUP_KEEP_COUNT,
)


def create_db(conn, project_id="proj-1", worktree="/tmp/project", sessions=None):
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE project (id TEXT, worktree TEXT)")
    cursor.execute("CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, path TEXT)")
    cursor.execute("INSERT INTO project VALUES (?, ?)", (project_id, worktree))
    if sessions:
        for s in sessions:
            cursor.execute("INSERT INTO session VALUES (?, ?, ?, ?)", s)
    conn.commit()


# --- Schema validation ---


class TestValidateSchema:
    def test_valid_schema(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        create_db(conn)
        assert validate_schema(conn) is True
        conn.close()

    def test_missing_project_table(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, path TEXT)")
        assert validate_schema(conn) is False
        conn.close()

    def test_missing_session_table(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("CREATE TABLE project (id TEXT, worktree TEXT)")
        assert validate_schema(conn) is False
        conn.close()

    def test_missing_column_in_project(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("CREATE TABLE project (id TEXT)")
        conn.execute("CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, path TEXT)")
        assert validate_schema(conn) is False
        conn.close()

    def test_missing_column_in_session(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("CREATE TABLE project (id TEXT, worktree TEXT)")
        conn.execute("CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT)")
        assert validate_schema(conn) is False
        conn.close()

    def test_empty_database(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        assert validate_schema(conn) is False
        conn.close()


# --- normalize_dir ---


class TestNormalizeDir:
    def test_removes_trailing_slash(self):
        assert normalize_dir("/foo/bar/") == "/foo/bar"

    def test_normalizes_double_slash(self):
        assert normalize_dir("/foo//bar") == "/foo/bar"

    def test_no_change(self):
        assert normalize_dir("/foo/bar") == "/foo/bar"


# --- is_protected_path ---


class TestIsProtectedPath:
    def test_root_is_protected(self):
        assert is_protected_path("/") is True

    def test_home_is_protected(self):
        home = os.path.expanduser("~")
        assert is_protected_path(home) is True

    def test_usr_is_protected(self):
        assert is_protected_path("/usr") is True

    def test_normal_path_not_protected(self):
        assert is_protected_path("/home/user/project") is False

    def test_trailing_slash_still_protected(self):
        assert is_protected_path("/usr/") is True

    def test_symlink_to_protected_path_is_protected(self, tmp_path):
        """A symlink pointing to a protected path should be detected."""
        symlink = tmp_path / "link_to_etc"
        symlink.symlink_to("/etc")
        assert is_protected_path(str(symlink)) is True


# --- compute_new_session_path ---


class TestComputeNewSessionPath:
    def test_strips_leading_slash(self):
        assert compute_new_session_path("/home/user/project") == "home/user/project"

    def test_root_path_returns_empty(self):
        """Root path should return empty string (not '.')."""
        assert compute_new_session_path("/") == ""


# --- Project lookup ---


class TestFindProject:
    def test_exact_match(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        create_db(conn, worktree="/home/user/project")
        pid, wt = find_project_by_worktree(conn, "/home/user/project")
        assert pid == "proj-1"
        assert wt == "/home/user/project"
        conn.close()

    def test_trailing_slash_normalization(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        create_db(conn, worktree="/home/user/project")
        pid, _ = find_project_by_worktree(conn, "/home/user/project/")
        assert pid == "proj-1"
        conn.close()

    def test_stored_trailing_slash(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        create_db(conn, worktree="/home/user/project/")
        pid, _ = find_project_by_worktree(conn, "/home/user/project")
        assert pid == "proj-1"
        conn.close()

    def test_not_found(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        create_db(conn, worktree="/other/path")
        pid, wt = find_project_by_worktree(conn, "/home/user/project")
        assert pid is None
        assert wt is None
        conn.close()


# --- Session lookup ---


class TestFindSessions:
    def test_find_by_project(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [
            ("s1", "proj-1", "/tmp/project", "/tmp/project"),
            ("s2", "proj-1", "/tmp/project", "/tmp/project/sub"),
            ("s3", "proj-2", "/other", "/other"),
        ]
        create_db(conn, sessions=sessions)
        result = find_sessions_by_project(conn, "proj-1")
        assert len(result) == 2
        assert all(r[1] == "proj-1" for r in result)
        conn.close()

    def test_find_global_sessions(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [
            ("s1", "global", "/tmp/project", "/tmp/project"),
            ("s2", "global", "/tmp/project", "tmp/project"),
            ("s3", "proj-1", "/tmp/project", "/tmp/project"),
        ]
        create_db(conn, sessions=sessions)
        result = find_global_sessions_by_path(conn, "/tmp/project")
        assert len(result) == 2
        conn.close()


# --- find_global_sessions_by_path normalization ---


class TestFindGlobalSessionsNormalized:
    def test_matches_with_trailing_slash_in_db(self, tmp_path):
        """DB has directory='/tmp/project/' (trailing slash), query with '/tmp/project' should match."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [("s1", "global", "/tmp/project/", "/tmp/project/")]
        create_db(conn, sessions=sessions)
        result = find_global_sessions_by_path(conn, "/tmp/project")
        assert len(result) == 1
        assert result[0][0] == "s1"
        conn.close()

    def test_matches_with_double_slash_in_db(self, tmp_path):
        """DB has directory='/tmp//project', query with '/tmp/project' should match."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [("s1", "global", "/tmp//project", "/tmp//project")]
        create_db(conn, sessions=sessions)
        result = find_global_sessions_by_path(conn, "/tmp/project")
        assert len(result) == 1
        assert result[0][0] == "s1"
        conn.close()


# --- collect_changes ---


class TestCollectChanges:
    def test_project_with_sessions(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [
            ("s1", "proj-1", "/old", "/old"),
            ("s2", "proj-1", "/old", "/old/sub"),
        ]
        create_db(conn, worktree="/old", sessions=sessions)
        pid, changes = collect_changes(conn, "/old", "/new")
        assert pid == "proj-1"
        session_changes = [c for c in changes if c.change_type == "session"]
        project_changes = [c for c in changes if c.change_type == "project"]
        assert len(session_changes) == 2
        assert len(project_changes) == 1
        assert project_changes[0].new_directory == "/new"
        conn.close()

    def test_global_sessions_only(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [("s1", "global", "/old", "/old")]
        create_db(conn, worktree="/other", sessions=sessions)
        pid, changes = collect_changes(conn, "/old", "/new")
        assert pid is None
        assert len(changes) == 1
        assert changes[0].change_type == "session"
        conn.close()

    def test_no_matches(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        create_db(conn, worktree="/other")
        pid, changes = collect_changes(conn, "/old", "/new")
        assert pid is None
        assert len(changes) == 0
        conn.close()


# --- apply_changes ---


class TestApplyChanges:
    def test_updates_project_and_sessions(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [
            ("s1", "proj-1", "/old", "/old"),
            ("s2", "proj-1", "/old", "old/sub"),
        ]
        create_db(conn, worktree="/old", sessions=sessions)

        changes = [
            Change("session", "s1", "/old", "/new", "/old"),
            Change("session", "s2", "/old", "/new", "old/sub"),
            Change("project", "proj-1", "/old", "/new"),
        ]
        apply_changes(conn, changes, "/new")
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("SELECT worktree FROM project WHERE id = 'proj-1'")
        assert cursor.fetchone()[0] == "/new"

        cursor.execute("SELECT directory, path FROM session WHERE id = 's1'")
        row = cursor.fetchone()
        assert row[0] == "/new"
        assert row[1] == "new"

        cursor.execute("SELECT directory, path FROM session WHERE id = 's2'")
        row = cursor.fetchone()
        assert row[0] == "/new"
        assert row[1] == "new"

        conn.close()

    def test_updates_global_sessions(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [("s1", "global", "/old", "old")]
        create_db(conn, worktree="/other", sessions=sessions)

        changes = [Change("session", "s1", "/old", "/new", "old")]
        apply_changes(conn, changes, "/new")
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("SELECT directory, path FROM session WHERE id = 's1'")
        row = cursor.fetchone()
        assert row[0] == "/new"
        assert row[1] == "new"
        conn.close()


# --- apply_changes preserves path format ---


class TestApplyChangesPreservesPathFormat:
    def test_preserves_null_path(self, tmp_path):
        """Session with path=NULL should keep NULL after apply_changes."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [("s1", "proj-1", "/old", None)]
        create_db(conn, worktree="/old", sessions=sessions)

        changes = [Change("session", "s1", "/old", "/new", None)]
        apply_changes(conn, changes, "/new")
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("SELECT directory, path FROM session WHERE id = 's1'")
        row = cursor.fetchone()
        assert row[0] == "/new"
        assert row[1] is None  # NULL preserved
        conn.close()

    def test_preserves_empty_path(self, tmp_path):
        """Session with path='' should keep empty string after apply_changes."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [("s1", "proj-1", "/old", "")]
        create_db(conn, worktree="/old", sessions=sessions)

        changes = [Change("session", "s1", "/old", "/new", "")]
        apply_changes(conn, changes, "/new")
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("SELECT directory, path FROM session WHERE id = 's1'")
        row = cursor.fetchone()
        assert row[0] == "/new"
        assert row[1] == ""  # empty string preserved
        conn.close()

    def test_updates_real_path(self, tmp_path):
        """Session with a real path should be updated to new_session_path."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [("s1", "proj-1", "/old", "old/path")]
        create_db(conn, worktree="/old", sessions=sessions)

        changes = [Change("session", "s1", "/old", "/new", "old/path")]
        apply_changes(conn, changes, "/new")
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("SELECT directory, path FROM session WHERE id = 's1'")
        row = cursor.fetchone()
        assert row[0] == "/new"
        assert row[1] == "new"  # updated to new_session_path
        conn.close()


# --- apply_changes rollback ---


class TestApplyChangesRollback:
    def test_rollback_restores_original_values(self, tmp_path):
        """Rolling back changes should restore the original database state."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        sessions = [("s1", "proj-1", "/old", "old/path")]
        create_db(conn, worktree="/old", sessions=sessions)

        # Apply forward changes
        forward_changes = [
            Change("session", "s1", "/old", "/new", "old/path"),
            Change("project", "proj-1", "/old", "/new"),
        ]
        apply_changes(conn, forward_changes, "/new")
        conn.commit()

        # Apply rollback changes
        rollback_changes = [
            Change("session", "s1", "/new", "/old", "old/path"),
            Change("project", "proj-1", "/new", "/old"),
        ]
        apply_changes(conn, rollback_changes, "/old")
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("SELECT worktree FROM project WHERE id = 'proj-1'")
        assert cursor.fetchone()[0] == "/old"

        cursor.execute("SELECT directory, path FROM session WHERE id = 's1'")
        row = cursor.fetchone()
        assert row[0] == "/old"
        assert row[1] == "old"
        conn.close()


# --- backup_database ---


class TestBackupDatabase:
    def test_creates_backup_file(self, tmp_path):
        """Create a real db file, call backup_database, verify .backup. file exists."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn)
        conn.close()

        result = backup_database(str(db_path))

        assert result is not None
        assert os.path.exists(result)
        assert ".backup." in result

    def test_rotates_old_backups(self, tmp_path):
        """Create more than BACKUP_KEEP_COUNT backup files, verify only BACKUP_KEEP_COUNT remain."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn)
        conn.close()

        # Create more backups than BACKUP_KEEP_COUNT
        for _ in range(BACKUP_KEEP_COUNT + 3):
            backup_database(str(db_path))

        # Count remaining backups
        pattern = os.path.join(str(tmp_path), f"test.db.backup.*")
        backups = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        assert len(backups) == BACKUP_KEEP_COUNT

    def test_returns_none_for_missing_db(self, tmp_path):
        """backup_database with non-existent path returns None."""
        result = backup_database(str(tmp_path / "nonexistent.db"))
        assert result is None


# --- main() end-to-end ---


class TestMainDryRun:
    def test_dry_run_no_changes(self, tmp_path, capsys, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        new_dir = tmp_path / "new-project"
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree="/other/path")
        conn.close()

        ret = main(["--dry-run", str(old_dir), str(new_dir)])
        assert ret == 0
        assert old_dir.exists()
        assert not new_dir.exists()
        captured = capsys.readouterr()
        assert "No database updates needed" in captured.out

    def test_dry_run_shows_changes(self, tmp_path, capsys, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        new_dir = tmp_path / "new-project"
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir), sessions=[
            ("s1", "proj-1", str(old_dir), str(old_dir)),
        ])
        conn.close()

        ret = main(["--dry-run", str(old_dir), str(new_dir)])
        assert ret == 0
        assert old_dir.exists()
        captured = capsys.readouterr()
        assert "Would update database" in captured.out
        assert "project.worktree" in captured.out


class TestMainMove:
    def test_move_project_with_sessions(self, tmp_path, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        (old_dir / "file.txt").write_text("hello")
        new_dir = tmp_path / "new-project"
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir), sessions=[
            ("s1", "proj-1", str(old_dir), str(old_dir)),
        ])
        conn.close()

        ret = main([str(old_dir), str(new_dir)])

        assert ret == 0
        assert not old_dir.exists()
        assert new_dir.exists()
        assert (new_dir / "file.txt").read_text() == "hello"

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT worktree FROM project WHERE id = 'proj-1'")
        assert cursor.fetchone()[0] == str(new_dir)
        cursor.execute("SELECT directory FROM session WHERE id = 's1'")
        assert cursor.fetchone()[0] == str(new_dir)
        conn.close()

    def test_move_preserves_db_in_transaction(self, tmp_path, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        new_dir = tmp_path / "new-project"
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir), sessions=[
            ("s1", "proj-1", str(old_dir), str(old_dir)),
        ])
        conn.close()

        ret = main([str(old_dir), str(new_dir)])

        assert ret == 0
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT worktree FROM project WHERE id = 'proj-1'")
        assert cursor.fetchone()[0] == str(new_dir)
        cursor.execute("SELECT directory, path FROM session WHERE id = 's1'")
        row = cursor.fetchone()
        assert row[0] == str(new_dir)
        assert row[1] == os.path.relpath(str(new_dir), "/")
        conn.close()


class TestMainForce:
    def test_force_overwrites_existing(self, tmp_path, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        (old_dir / "file.txt").write_text("new")
        new_dir = tmp_path / "new-project"
        new_dir.mkdir()
        (new_dir / "old.txt").write_text("old")
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir), sessions=[
            ("s1", "proj-1", str(old_dir), str(old_dir)),
        ])
        conn.close()

        ret = main(["-f", str(old_dir), str(new_dir)])

        assert ret == 0
        assert not old_dir.exists()
        assert (new_dir / "file.txt").read_text() == "new"
        assert not (new_dir / "old.txt").exists()

    def test_no_force_rejects_existing(self, tmp_path, capsys, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        new_dir = tmp_path / "new-project"
        new_dir.mkdir()
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir))
        conn.close()

        ret = main([str(old_dir), str(new_dir)])

        assert ret == 1
        captured = capsys.readouterr()
        assert "already exists" in captured.err


class TestMainForceFile:
    def test_force_overwrites_existing_file(self, tmp_path, isolate_db_env):
        """--force with file target uses os.remove instead of shutil.rmtree."""
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        (old_dir / "file.txt").write_text("hello")
        new_path = tmp_path / "new-project"
        new_path.write_text("I am a file")  # new_path is a file, not directory
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir), sessions=[
            ("s1", "proj-1", str(old_dir), str(old_dir)),
        ])
        conn.close()

        ret = main(["-f", str(old_dir), str(new_path)])

        assert ret == 0
        assert not old_dir.exists()
        assert new_path.exists()
        assert new_path.is_dir()  # old file was replaced with directory
        assert (new_path / "file.txt").read_text() == "hello"


class TestMainProtectedPaths:
    def test_force_rejects_root(self, tmp_path, capsys, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir))
        conn.close()

        ret = main(["-f", str(old_dir), "/"])

        assert ret == 1
        captured = capsys.readouterr()
        assert "protected path" in captured.err

    def test_force_rejects_home(self, tmp_path, capsys, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir))
        conn.close()

        ret = main(["-f", str(old_dir), os.path.expanduser("~")])

        assert ret == 1
        captured = capsys.readouterr()
        assert "protected path" in captured.err

    def test_rejects_root_without_force(self, tmp_path, capsys, isolate_db_env):
        """Protected path check should apply even without --force."""
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir))
        conn.close()

        ret = main([str(old_dir), "/"])

        assert ret == 1
        captured = capsys.readouterr()
        assert "protected path" in captured.err


class TestMainSamePath:
    def test_rejects_same_path(self, tmp_path, capsys, isolate_db_env):
        """Moving to the same path should be rejected."""
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir))
        conn.close()

        ret = main([str(old_dir), str(old_dir)])

        assert ret == 1
        captured = capsys.readouterr()
        assert "same" in captured.err.lower()


class TestMainErrors:
    def test_old_path_not_exists(self, tmp_path, capsys, isolate_db_env):
        ret = main(["/nonexistent/path", "/tmp/new"])
        assert ret == 1
        captured = capsys.readouterr()
        assert "does not exist" in captured.err

    def test_old_path_is_file(self, tmp_path, capsys, isolate_db_env):
        f = tmp_path / "file.txt"
        f.write_text("hi")
        ret = main([str(f), str(tmp_path / "new")])
        assert ret == 1
        captured = capsys.readouterr()
        assert "is not a directory" in captured.err

    def test_db_not_found(self, tmp_path, capsys, monkeypatch):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        monkeypatch.setenv("OPENCODE_DB_PATH", str(tmp_path / "nonexistent.db"))
        ret = main([str(old_dir), str(tmp_path / "new")])
        assert ret == 1
        captured = capsys.readouterr()
        assert "database not found" in captured.err

    def test_invalid_schema(self, tmp_path, capsys, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE foo (id TEXT)")
        conn.close()

        ret = main([str(old_dir), str(tmp_path / "new")])
        assert ret == 1
        captured = capsys.readouterr()
        assert "schema" in captured.err.lower()


class TestMainSubdirectory:
    def test_rejects_moving_into_subdirectory(self, tmp_path, capsys, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        new_dir = old_dir / "sub" / "new-project"
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir))
        conn.close()

        ret = main([str(old_dir), str(new_dir)])

        assert ret == 1
        captured = capsys.readouterr()
        assert "cannot move" in captured.err
        assert "subdirectory of itself" in captured.err

    def test_rejects_moving_into_subdirectory_before_rmtree(self, tmp_path, capsys, isolate_db_env):
        """Test that subdirectory check fires BEFORE rmtree when --force is used.

        Create old_dir, create new_dir as subdirectory of old_path, add a file in new_dir,
        run with -f, verify new_dir still exists (not deleted) and error returned.
        """
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        new_dir = old_dir / "sub" / "new-project"
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        new_dir.mkdir()
        (new_dir / "precious.txt").write_text("do not delete me")
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir))
        conn.close()

        ret = main(["-f", str(old_dir), str(new_dir)])

        assert ret == 1
        captured = capsys.readouterr()
        assert "cannot move" in captured.err
        # The subdirectory should NOT have been deleted by rmtree
        assert new_dir.exists()
        assert (new_dir / "precious.txt").read_text() == "do not delete me"


class TestMainMoveFailure:
    def test_db_not_updated_when_move_fails(self, tmp_path, capsys, monkeypatch, isolate_db_env):
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        new_dir = tmp_path / "new-project"
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir), sessions=[
            ("s1", "proj-1", str(old_dir), str(old_dir)),
        ])
        conn.close()

        def failing_move(src, dst):
            raise OSError("permission denied")

        import shutil
        monkeypatch.setattr(shutil, "move", failing_move)

        ret = main([str(old_dir), str(new_dir)])

        assert ret == 1
        assert old_dir.exists()
        assert not new_dir.exists()

        # Verify database was rolled back (not left pointing to new_path)
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT worktree FROM project WHERE id = 'proj-1'")
        assert cursor.fetchone()[0] == str(old_dir)
        cursor.execute("SELECT directory FROM session WHERE id = 's1'")
        assert cursor.fetchone()[0] == str(old_dir)
        conn.close()

        captured = capsys.readouterr()
        assert "roll back" in captured.err.lower() or "rolled back" in captured.err.lower()

    def test_move_failure_rollback_message(self, tmp_path, capsys, monkeypatch, isolate_db_env):
        """When move fails, the error message should mention rollback."""
        old_dir = tmp_path / "project"
        old_dir.mkdir()
        new_dir = tmp_path / "new-project"
        db_path = tmp_path / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        create_db(conn, worktree=str(old_dir))
        conn.close()

        def failing_move(src, dst):
            raise OSError("permission denied")

        import shutil
        monkeypatch.setattr(shutil, "move", failing_move)

        ret = main([str(old_dir), str(new_dir)])
        assert ret == 1
        captured = capsys.readouterr()
        assert "roll back" in captured.err.lower()
