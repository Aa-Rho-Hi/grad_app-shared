"""Cloud-mode guardrails.

Every check here is about the same failure: defaults that are correct on a
laptop and dangerous the moment the app has a public address. The app is
supposed to refuse to start rather than come up quietly misconfigured, so most
of these assert on SystemExit at import time — a container that won't boot is a
page you notice, an open one is a page you don't.

These reload `src.app` under different environments, and always reload it back
to local afterwards so the rest of the suite sees the module it expects.
"""
import contextlib
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CLOUD = {"GRADAPP_ENV": "cloud", "GRADAPP_SECRET_KEY": "k" * 64,
         "GRADAPP_SETUP_TOKEN": "claim-me"}


@contextlib.contextmanager
def app_with(env, monkeypatch):
    """Import src.app under `env`. Anything the module raises comes to you."""
    from src import app as appmod
    for key in ("GRADAPP_ENV", "GRADAPP_SECRET_KEY", "GRADAPP_SETUP_TOKEN",
                "GRADAPP_DEBUG", "GRADAPP_FORCE_SECURE_COOKIES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    try:
        yield importlib.reload(appmod)
    finally:
        for key in env:
            monkeypatch.delenv(key, raising=False)
        importlib.reload(appmod)          # back to local for everyone else


@pytest.fixture(autouse=True)
def _restore_local():
    yield
    from src import app as appmod
    if appmod.IS_CLOUD:                   # a failed reload can leave it cloudy
        importlib.reload(appmod)


def test_cloud_refuses_to_boot_without_a_secret_key(monkeypatch):
    """A per-instance random key means sessions minted by one instance are
    rejected by the next — which looks like a flaky app, not a config error."""
    with pytest.raises(SystemExit) as e:
        with app_with({"GRADAPP_ENV": "cloud"}, monkeypatch):
            pass
    assert "GRADAPP_SECRET_KEY" in str(e.value)


def test_cloud_refuses_the_interactive_debugger(monkeypatch):
    with pytest.raises(SystemExit) as e:
        with app_with({**CLOUD, "GRADAPP_DEBUG": "1"}, monkeypatch):
            pass
    assert "debugger" in str(e.value).lower()


def test_cloud_refuses_to_boot_unclaimed(tmp_path, monkeypatch):
    """No accounts means no login at all. On a laptop that's the feature; on a
    public URL it's every applicant's file served to whoever finds it."""
    from src import users as usersmod
    monkeypatch.setattr(usersmod, "USERS_YAML", str(tmp_path / "users.yaml"))
    env = {"GRADAPP_ENV": "cloud", "GRADAPP_SECRET_KEY": "k" * 64}
    with pytest.raises(SystemExit) as e:
        with app_with(env, monkeypatch):
            pass
    assert "GRADAPP_SETUP_TOKEN" in str(e.value)


def test_cloud_tightens_the_defaults_that_matter(tmp_path, monkeypatch):
    from src import users as usersmod
    monkeypatch.setattr(usersmod, "USERS_YAML", str(tmp_path / "users.yaml"))
    with app_with(CLOUD, monkeypatch) as appmod:
        assert appmod.IS_CLOUD
        assert appmod.app.config["SESSION_COOKIE_SECURE"] is True
        assert appmod._MIN_PASSWORD >= 12
        assert appmod.app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert appmod.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_an_unclaimed_cloud_instance_serves_nothing(tmp_path, monkeypatch):
    from src import users as usersmod
    monkeypatch.setattr(usersmod, "USERS_YAML", str(tmp_path / "users.yaml"))
    with app_with(CLOUD, monkeypatch) as appmod:
        monkeypatch.setattr(appmod, "DATA", str(tmp_path))
        os.makedirs(tmp_path / "raw", exist_ok=True)
        appmod.app.config.update(TESTING=True)
        client = appmod.app.test_client()

        for url in ("/", "/applicants", "/triage", "/summaries", "/prof",
                    "/download/status.xlsx"):
            r = client.get(url)
            assert r.status_code == 302 and "/setup" in r.headers["Location"], url

        # and /setup itself won't mint an advisor for a passer-by
        assert client.get("/setup").status_code == 404
        assert client.get("/setup?token=wrong").status_code == 404
        assert client.get("/setup?token=claim-me").status_code == 200

        r = client.post("/setup?token=claim-me",
                        data={"username": "maya", "password": "a-long-enough-one",
                              "confirm": "a-long-enough-one", "token": "claim-me"})
        assert r.status_code == 302
        assert [u["username"] for u in usersmod.load_users()] == ["maya"]


def test_cloud_enforces_the_longer_password(tmp_path, monkeypatch):
    from src import users as usersmod
    monkeypatch.setattr(usersmod, "USERS_YAML", str(tmp_path / "users.yaml"))
    with app_with(CLOUD, monkeypatch) as appmod:
        monkeypatch.setattr(appmod, "DATA", str(tmp_path))
        os.makedirs(tmp_path / "raw", exist_ok=True)
        appmod.app.config.update(TESTING=True)
        client = appmod.app.test_client()
        client.post("/setup?token=claim-me",
                    data={"username": "short", "password": "demo", "confirm": "demo",
                          "token": "claim-me"})
        assert not usersmod.load_users(), "a 4-character password was accepted"


def test_cloud_infrastructure_without_cloud_mode_refuses_to_boot(monkeypatch):
    """The dangerous middle state: real infrastructure wired up, guardrails off,
    and nothing about it looks wrong. A gcloud deploy that repeats
    --set-env-vars keeps only the last one, which drops GRADAPP_ENV and leaves
    everything else — exactly this."""
    for var in ("GRADAPP_DATABASE_URL", "GRADAPP_BUCKET", "GRADAPP_TASK_QUEUE"):
        with pytest.raises(SystemExit) as e:
            with app_with({var: "something-real"}, monkeypatch):
                pass
        assert var in str(e.value) and "GRADAPP_ENV" in str(e.value)


def test_local_defaults_are_untouched(monkeypatch):
    """The whole point is that developing on a laptop stays pleasant."""
    with app_with({}, monkeypatch) as appmod:
        assert not appmod.IS_CLOUD
        assert appmod._MIN_PASSWORD == 4
        assert appmod.app.config["SESSION_COOKIE_SECURE"] is False
        assert appmod.app.secret_key                     # random is fine here
