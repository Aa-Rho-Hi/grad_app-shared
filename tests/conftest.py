"""Shared test isolation.

Two files under `config/` are process-global and live at a fixed path rather
than under the per-run data directory, so anything that writes them writes the
developer's own copy.

`users.yaml` decides whether the whole app requires a login, so without this
every route test passes or fails depending on whether that machine happens to
have accounts configured.

`professors.yaml` is worse, because the damage is silent and permanent: tests
that exercise the add-a-professor route append to the real faculty list and
nothing complains. That is not hypothetical — the adversarial suite's XSS and
YAML-injection probes ended up in a live config and showed up on the dashboard
as two professors named `<img src=x onerror=alert(1)>` and `*anchor: &a ...`.
The escaping held, so they rendered as harmless text, but they had to be
deleted by hand.

Both get a per-test temp path. A test that wants entries creates them there.
"""
import pytest


@pytest.fixture(scope="session", autouse=True)
def _real_config_is_untouched():
    """Fail the run if anything wrote to the developer's actual config.

    The monkeypatching below is per-test and easy to bypass by accident — a new
    module-level constant, a helper that resolves the path itself, a subprocess.
    This watches the real files instead of trusting that every route was
    isolated, so the next version of this mistake is caught by the suite rather
    than by someone spotting a stray name on their dashboard.
    """
    import hashlib
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    watched = [os.path.join(root, "config", "professors.yaml"),
               os.path.join(root, "config", "users.yaml"),
               os.path.join(root, "config", "groups.yaml")]

    def digest():
        out = {}
        for path in watched:
            try:
                with open(path, "rb") as fh:
                    out[path] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                out[path] = None        # absent is a fine, stable state
        return out

    before = digest()
    yield
    for path, was in before.items():
        assert digest()[path] == was, (
            f"the test suite modified {path}. Tests must never touch the real "
            "config — isolate the module constant (see _isolate_config).")


#: Seeded into every test's isolated professors.yaml. Spelled out here rather
#: than copied from config/ so the suite asserts against a known list instead of
#: whatever faculty this particular machine happens to have configured.
DEMO_FACULTY = [
    {"id": "duffield-nick", "name": "Nick Duffield"},
    {"id": "fan-zhiwen", "name": "Zhiwen Fan"},
    {"id": "gratz-paul", "name": "Paul Gratz"},
    {"id": "narayanan-krishna", "name": "Krishna Narayanan"},
]


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """The limiter's memory:// storage is per-process, and every test client
    looks like the same IP, so counts pile up across unrelated tests until one
    of them gets a surprise 429. Clear it between tests; a test that wants to
    prove the limiter works trips it deliberately.
    """
    yield
    try:
        from src.app import limiter
        limiter.reset()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    from src import triage as triagemod
    from src import users as usersmod
    monkeypatch.setattr(usersmod, "USERS_YAML", str(tmp_path / "users.yaml"))

    professors = tmp_path / "professors.yaml"
    monkeypatch.setattr(triagemod, "PROFESSORS_YAML", str(professors))
    triagemod.save_professors(DEMO_FACULTY, str(professors))

    # Groups start empty per test; a test that wants one creates it.
    from src import groups as groupsmod
    monkeypatch.setattr(groupsmod, "GROUPS_YAML", str(tmp_path / "groups.yaml"))
    yield
