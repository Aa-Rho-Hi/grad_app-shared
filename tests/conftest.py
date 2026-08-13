"""Shared test isolation.

The accounts file is global state that decides whether the whole app requires a
login. Left alone, the suite reads the developer's real `config/users.yaml`, so
every route test would pass or fail depending on whether that machine happens to
have logins configured. Point it at a per-test temp path instead; a test that
wants accounts creates them there itself.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_accounts(tmp_path, monkeypatch):
    from src import users as usersmod
    monkeypatch.setattr(usersmod, "USERS_YAML", str(tmp_path / "users.yaml"))
    yield
