import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from epistemic_monitor.config import load_config  # noqa: E402
from epistemic_monitor.store import LedgerStore  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "epistemic"
    monkeypatch.setenv("EPISTEMIC_DATA_DIR", str(d))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return d


@pytest.fixture
def cfg(data_dir, tmp_path):
    (tmp_path / "project").mkdir(exist_ok=True)
    return load_config(cwd=tmp_path / "project")


@pytest.fixture
def store(cfg):
    s = LedgerStore(cfg)
    yield s
    s.close()
