"""
Tests for the startup DB-writability check (Fix A, post-launch regression).

Root cause: the non-root container (uid 10001 'appuser') couldn't write the bind-mounted
./data on Oracle, and because the DB runs in WAL mode, even a SELECT needs to write -wal/-shm
sidecar files. The previous entrypoint silenced the chown failure (`2>/dev/null || true`), so
the bot booted "successfully" and only failed later, per-command, with a confusing
"attempt to write a readonly database" deep in a handler traceback (e.g. on /threads).

These tests pin the fix: a startup check that fails LOUD and FAST with an actionable message,
rather than deferring the failure to first DB access.
"""
import os
import stat
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest

from storage.database_storage import _assert_data_dir_writable, init_database
import config

# Permission bits don't restrict root — skip the readonly-simulation tests if the test
# runner itself is root (e.g. some CI containers), since chmod(0o500) wouldn't actually
# block a write and the test would be meaningless rather than failing for the wrong reason.
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def test_writable_dir_passes(tmp_path):
    """A normal writable directory passes the probe without raising."""
    _assert_data_dir_writable(str(tmp_path))  # must not raise


@pytest.mark.skipif(_IS_ROOT, reason="root bypasses file permission bits")
def test_unwritable_dir_raises_actionable_error(tmp_path):
    """A directory without write permission raises a clear, actionable RuntimeError —
    not a bare OSError, and not silence."""
    ro_dir = tmp_path / "readonly_data"
    ro_dir.mkdir()
    ro_dir.chmod(stat.S_IREAD | stat.S_IEXEC)  # r-x, no write
    try:
        with pytest.raises(RuntimeError) as exc_info:
            _assert_data_dir_writable(str(ro_dir))
        # The message must guide the operator to the actual fix, not just say "failed".
        assert "chown" in str(exc_info.value)
        assert "10001" in str(exc_info.value)
    finally:
        ro_dir.chmod(stat.S_IRWXU)  # restore so tmp_path cleanup can remove it


@pytest.mark.skipif(_IS_ROOT, reason="root bypasses file permission bits")
@pytest.mark.asyncio
async def test_init_database_fails_loud_on_unwritable_dir(tmp_path):
    """init_database must raise (not silently continue / crash later) when the data
    directory is unwritable — this is what makes the failure surface at boot instead of
    on the first /threads or DB write."""
    ro_dir = tmp_path / "readonly_data"
    ro_dir.mkdir()
    ro_dir.chmod(stat.S_IREAD | stat.S_IEXEC)

    original_db_path = config.DB_PATH
    config.DB_PATH = str(ro_dir / "test.db")
    try:
        with pytest.raises(RuntimeError, match="not writable"):
            await init_database()
    finally:
        config.DB_PATH = original_db_path
        ro_dir.chmod(stat.S_IRWXU)


@pytest.mark.asyncio
async def test_init_database_succeeds_on_writable_dir(tmp_path):
    """Sanity check: the new writability gate doesn't break the normal, healthy path."""
    original_db_path = config.DB_PATH
    config.DB_PATH = str(tmp_path / "test.db")
    try:
        await init_database()  # must not raise
        assert os.path.exists(config.DB_PATH)
    finally:
        config.DB_PATH = original_db_path
