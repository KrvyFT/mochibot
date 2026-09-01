"""Shared fixtures for unit tests.

Provides:
- offline_model_pool: keep model access off the network and deterministic
- fresh_db: isolated SQLite database per test
- mock_config: override config values so tests don't need .env
"""

import pytest
from datetime import timezone, timedelta

from mochi.db import init_db
import mochi.skills as skill_registry


UTC = timezone.utc

# Width of the sqlite-vec column for every test that stores an embedding.
# The value is arbitrary; what matters is that it is fixed. Tests must not
# inherit it from the developer's embedding model, whose dimension varies per
# deployment and can only be learned by calling the provider.
TEST_EMBEDDING_DIM = 1536

# Ensure skills are discovered once (module-level state)
_skills_discovered = False


class _OfflineModelPool:
    """Offline stand-in for ModelPool.

    Only the embedding width is provided, because that is all init_db() needs
    to size the vector table. Everything else is deliberately absent: a test
    that wants real model behaviour should arrange it explicitly rather than
    silently reaching the network with the developer's credentials.
    """

    def get_embed_dim(self) -> int:
        return TEST_EMBEDDING_DIM


@pytest.fixture(autouse=True)
def offline_model_pool(monkeypatch):
    """Pin the model pool to an offline stub with a fixed embedding width.

    Without this, init_db() builds a real ModelPool, which probes the
    configured embedding endpoint over the network. That made vector-backed
    tests pass or fail according to connectivity and to whichever model the
    developer happens to have configured.
    """
    import mochi.model_pool as model_pool
    monkeypatch.setattr(model_pool, "_pool", _OfflineModelPool())


@pytest.fixture
def embedding_dim() -> int:
    """The vector width the test database was built with."""
    return TEST_EMBEDDING_DIM


# offline_model_pool is requested explicitly so it is installed before
# init_db() reads the embedding width.
@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch, offline_model_pool):
    """Fresh SQLite database for each test."""
    global _skills_discovered
    db_path = tmp_path / "unit_test.db"
    import mochi.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    import mochi.core_store as core_store
    monkeypatch.setattr(core_store, "DATA_DIR", tmp_path / "core_data")
    init_db()
    if not _skills_discovered:
        skill_registry.discover()
        _skills_discovered = True
    skill_registry.init_all_skill_schemas()
    yield db_path


@pytest.fixture(autouse=True)
def mock_config(monkeypatch):
    """Override config values so unit tests never rely on .env."""
    import mochi.config as cfg
    monkeypatch.setattr(cfg, "OWNER_USER_ID", 1)
    monkeypatch.setattr(cfg, "TIMEZONE_OFFSET_HOURS", 0)
    monkeypatch.setattr(cfg, "TZ", UTC)
    # Also patch TZ in modules that imported it at module level
    import mochi.db as db_module
    monkeypatch.setattr(db_module, "TZ", UTC)
    monkeypatch.setattr(cfg, "MAINTENANCE_HOUR", 3)
    monkeypatch.setattr(cfg, "WEEKLY_MAINTENANCE_ENABLED", True)
    monkeypatch.setattr(cfg, "WEEKLY_MAINTENANCE_MINUTE", 15)
    monkeypatch.setattr(cfg, "TOOL_ROUTER_ENABLED", False)
    monkeypatch.setattr(cfg, "TOOL_ESCALATION_ENABLED", False)
    monkeypatch.setattr(cfg, "TOOL_LOOP_MAX_ROUNDS", 5)
