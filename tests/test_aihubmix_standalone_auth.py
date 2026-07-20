import importlib.util
from pathlib import Path

SERVER_PATH = (
    Path(__file__).resolve().parents[1]
    / "drafts"
    / "aihubmix_standalone"
    / "server.py"
)


def load_server_module():
    """Load the standalone server module directly from drafts.

    Returns:
        The imported module object.
    """
    spec = importlib.util.spec_from_file_location("aihubmix_standalone_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_auth_is_disabled_without_token(monkeypatch):
    monkeypatch.delenv("DIANCHI_TOOLBOX_ACCESS_TOKEN", raising=False)
    server = load_server_module()

    assert server.auth_enabled() is False
    assert server.verify_auth_session("") is True


def test_signed_auth_session_validates_and_expires(monkeypatch):
    monkeypatch.setenv("DIANCHI_TOOLBOX_ACCESS_TOKEN", "desk-token")
    server = load_server_module()

    cookie = server.sign_auth_session(now=1000)

    assert server.auth_enabled() is True
    assert server.verify_auth_session(cookie, now=1001) is True
    assert server.verify_auth_session(f"{cookie}x", now=1001) is False
    assert server.verify_auth_session(cookie, now=1000 + server.AUTH_SESSION_SECONDS + 1) is False
