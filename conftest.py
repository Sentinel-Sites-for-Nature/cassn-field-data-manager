"""Configure isolation before pytest imports any project test modules."""
import sys
sys.dont_write_bytecode = True

from pathlib import Path
from test_support.isolation import start_session

collect_ignore = ["local_data", "output", "outputs", "dist", "build", ".aws", ".codex", ".agents", ".git", ".venv"]

TEST_SESSION_ROOT = start_session()

import pytest
from _pytest.assertion import rewrite

# Do not reuse assertion bytecode copied from another checkout. Such code can
# carry old absolute filenames and make pytest inspect an unrelated directory.
# Compile assertions in memory, preserving their normal useful diagnostics.
rewrite._read_pyc = lambda *args, **kwargs: None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if config.option.basetemp is not None:
        supplied = Path(config.option.basetemp).resolve()
        if not supplied.is_relative_to(TEST_SESSION_ROOT):
            raise pytest.UsageError('Do not supply an external --basetemp; tests choose a private temporary directory')
    config.option.basetemp = str(TEST_SESSION_ROOT / 'pytest')


@pytest.fixture(scope='session')
def test_session_root():
    return TEST_SESSION_ROOT
