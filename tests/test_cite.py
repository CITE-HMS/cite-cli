import time

import pytest
from typer.testing import CliRunner

import cite._cleanup as _cleanup
from cite._cleanup import iter_empty_dirs, iter_old_files
from cite.cli import app

runner = CliRunner()


@pytest.fixture()
def data_dir(tmp_path):
    """Create a temp directory with files of varying ages."""
    # Create a recent file
    recent = tmp_path / "recent.txt"
    recent.write_text("recent")

    # Create subdirectory with a file
    sub = tmp_path / "subdir"
    sub.mkdir()
    nested = sub / "nested.txt"
    nested.write_text("nested")

    # Create an empty subdirectory
    (tmp_path / "empty_dir").mkdir()

    return tmp_path


def test_iter_old_files_no_old_files(data_dir):
    """Freshly created files should not be yielded."""
    result = list(iter_old_files(data_dir, min_age=30))
    assert result == []


def test_iter_old_files_with_old_files(tmp_path, monkeypatch):
    """Files older than min_age should be yielded."""
    import cite._cleanup as _cleanup

    old_file = tmp_path / "old.txt"
    old_file.write_text("old")

    # Advance the module-level TIME by 60 days so the file appears old
    monkeypatch.setattr(_cleanup, "TIME", time.time() + 60 * 86400)

    result = list(iter_old_files(tmp_path, min_age=30))
    assert len(result) == 1
    assert result[0][0] == old_file
    assert result[0][1] > 30


def test_iter_old_files_skip(tmp_path, monkeypatch):
    """Files matching the skip pattern should be excluded."""
    import cite._cleanup as _cleanup

    old_file = tmp_path / "delete_me.txt"
    old_file.write_text("old")

    monkeypatch.setattr(_cleanup, "TIME", time.time() + 60 * 86400)

    result = list(iter_old_files(tmp_path, min_age=30, skip="delete"))
    assert result == []


def test_iter_empty_dirs(data_dir):
    """Should find empty directories."""
    result = list(iter_empty_dirs(data_dir))
    assert len(result) == 1
    assert result[0].name == "empty_dir"


def test_iter_empty_dirs_skip(data_dir):
    """Should skip directories matching the skip pattern."""
    result = list(iter_empty_dirs(data_dir, skip="empty"))
    assert result == []


def test_iter_empty_dirs_no_empty(tmp_path):
    """No empty dirs should yield nothing."""
    (tmp_path / "file.txt").write_text("content")
    result = list(iter_empty_dirs(tmp_path))
    assert result == []


# --- CLI tests ---


def test_clean_explicit_directory_no_old_files(tmp_path):
    """clean with an explicit directory containing only recent files."""
    (tmp_path / "recent.txt").write_text("hi")
    result = runner.invoke(app, ["clean", str(tmp_path)])
    assert result.exit_code == 0
    assert "No files found" in result.output


def test_clean_explicit_directory_dry_run(tmp_path, monkeypatch):
    """clean --dry-run should list files but not delete them."""
    f = tmp_path / "old.txt"
    f.write_text("data")
    monkeypatch.setattr(_cleanup, "TIME", time.time() + 60 * 86400)

    result = runner.invoke(app, ["clean", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "Would delete" in result.output
    assert f.exists()


def test_clean_explicit_directory_force(tmp_path, monkeypatch):
    """clean --force should delete old files without prompting."""
    f = tmp_path / "old.txt"
    f.write_text("data")
    monkeypatch.setattr(_cleanup, "TIME", time.time() + 60 * 86400)

    result = runner.invoke(app, ["clean", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert "Deleted" in result.output
    assert not f.exists()


def test_clean_nonexistent_directory():
    """clean with a path that doesn't exist should report an error."""
    result = runner.invoke(app, ["clean", "/no/such/path"])
    assert result.exit_code == 0
    assert "does not exist" in result.output


def test_clean_default_paths_uses_defaults(tmp_path, monkeypatch):
    """clean with no directory argument should use DEFAULT_PATHS."""
    (tmp_path / "recent.txt").write_text("hi")
    monkeypatch.setattr("cite.cli.DEFAULT_PATHS", [str(tmp_path)])

    result = runner.invoke(app, ["clean"])
    assert result.exit_code == 0
    assert "Cleaning default path" in result.output


def test_clean_default_paths_none_exist(monkeypatch):
    """clean with no args and no existing default paths should error."""
    monkeypatch.setattr("cite.cli.DEFAULT_PATHS", ["/no/such/path"])

    result = runner.invoke(app, ["clean"])
    assert result.exit_code == 1
    assert "No default directories found" in result.output
