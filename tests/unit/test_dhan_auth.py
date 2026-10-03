"""Dhan token lifecycle: expiry, renewal, PIN+TOTP login, auto-login, CLI. Offline fakes."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from nifbot import cli
from nifbot.data import factory
from nifbot.data.adapter import DataError
from nifbot.data.dhan_auth import (
    TokenManager,
    TokenStore,
    _extract_token,
    token_expiry,
    totp_now,
)
from nifbot.timeutil import IST

NOW = datetime(2026, 10, 5, 8, 30, tzinfo=IST)


def jwt(exp: datetime, tag: str = "a") -> str:
    """Unsigned JWT-shaped string for tests (payload only matters)."""

    def b64(d: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'exp': int(exp.timestamp()), 't': tag})}.sig{tag}"


class FakeAuth:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.renew_status = 200
        self.login_status = 200

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if req.url.path.endswith("/RenewToken"):
            if self.renew_status != 200:
                return httpx.Response(self.renew_status, json={"errorMessage": "no renew"})
            return httpx.Response(200, json={"accessToken": jwt(NOW + timedelta(hours=24), "r")})
        if req.url.path.endswith("/generateAccessToken"):
            if self.login_status != 200:
                return httpx.Response(self.login_status, json={"message": "bad totp"})
            return httpx.Response(
                200, json={"data": {"accessToken": jwt(NOW + timedelta(hours=24), "l")}}
            )
        return httpx.Response(404)


def manager(tmp_path: Path, env: str | None, fake: FakeAuth, **kw: object) -> TokenManager:
    return TokenManager(
        "1000000001",
        TokenStore(tmp_path / "s" / "tok.json"),
        env,
        transport=httpx.MockTransport(fake.handler),
        clock=lambda: NOW,
        **kw,
    )


def test_token_expiry_and_extract() -> None:
    exp = NOW + timedelta(hours=3)
    assert token_expiry(jwt(exp)) == exp
    assert token_expiry("not-a-jwt") is None and token_expiry("a.b.c") is None
    assert _extract_token({"token": jwt(exp)}) == jwt(exp)
    with pytest.raises(DataError):
        _extract_token({"status": "ok"})


def test_totp_rfc6238_vectors() -> None:
    secret = base64.b32encode(b"12345678901234567890").decode()  # RFC 6238 SHA-1 key
    assert totp_now(secret, at=59, digits=8) == "94287082"
    assert totp_now(secret, at=1111111109, digits=8) == "07081804"
    assert totp_now(secret.lower(), at=59) == "287082"


def test_valid_token_used_as_is(tmp_path: Path) -> None:
    fake = FakeAuth()
    tok = jwt(NOW + timedelta(hours=10))
    assert manager(tmp_path, tok, fake).ensure_valid() == tok
    assert fake.calls == []


def test_renews_when_close_to_expiry_and_stores(tmp_path: Path) -> None:
    fake = FakeAuth()
    old = jwt(NOW + timedelta(hours=2))
    m = manager(tmp_path, old, fake)
    new = m.ensure_valid()
    assert new != old and token_expiry(new) == NOW + timedelta(hours=24)
    req = fake.calls[0]
    assert req.headers["access-token"] == old and req.headers["dhanClientId"] == "1000000001"
    store = tmp_path / "s" / "tok.json"
    assert json.loads(store.read_text())["obtained_via"] == "renew"
    if os.name == "posix":
        assert store.stat().st_mode & 0o077 == 0
    # the stored (newer) token now wins over the old .env token
    m2 = manager(tmp_path, old, fake)
    cur = m2.current()
    assert cur is not None and cur.source == "store" and m2.ensure_valid() == new
    m.close()


def test_renew_failure_keeps_valid_token(tmp_path: Path) -> None:
    fake = FakeAuth()
    fake.renew_status = 400
    old = jwt(NOW + timedelta(hours=1))
    assert manager(tmp_path, old, fake).ensure_valid() == old
    with pytest.raises(DataError, match="renew failed: HTTP 400 no renew"):
        manager(tmp_path, old, fake).renew(old)


def test_expired_without_auto_login_asks_user(tmp_path: Path) -> None:
    fake = FakeAuth()
    with pytest.raises(DataError, match="dhan-login"):
        manager(tmp_path, jwt(NOW - timedelta(hours=1)), fake).ensure_valid()
    with pytest.raises(DataError, match="dhan-login"):
        manager(tmp_path, None, fake).ensure_valid()
    assert fake.calls == []


def test_expired_with_auto_login(tmp_path: Path) -> None:
    fake = FakeAuth()
    secret = base64.b32encode(b"12345678901234567890").decode()
    m = manager(
        tmp_path,
        jwt(NOW - timedelta(hours=1)),
        fake,
        pin="1234",
        totp_secret=secret,
        auto_login_totp=True,
    )
    new = m.ensure_valid()
    assert token_expiry(new) == NOW + timedelta(hours=24)
    params = fake.calls[0].url.params
    assert params["pin"] == "1234" and len(params["totp"]) == 6
    assert params["dhanClientId"] == "1000000001"


def test_pin_totp_validation_and_errors(tmp_path: Path) -> None:
    fake = FakeAuth()
    m = manager(tmp_path, None, fake)
    with pytest.raises(DataError, match="6-digit"):
        m.login_pin_totp("12a4", "123456")
    with pytest.raises(DataError, match="6-digit"):
        m.login_pin_totp("1234", "12345")
    fake.login_status = 401
    with pytest.raises(DataError, match="login failed: HTTP 401 bad totp"):
        m.login_pin_totp("1234", "123456")
    with pytest.raises(DataError):
        TokenManager("", TokenStore(tmp_path / "x.json"))


def test_network_errors(tmp_path: Path) -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x")

    m = TokenManager("1", TokenStore(tmp_path / "t.json"), transport=httpx.MockTransport(boom))
    with pytest.raises(DataError, match="network"):
        m.renew(jwt(NOW))
    with pytest.raises(DataError, match="network"):
        m.login_pin_totp("1234", "123456")


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    p.write_text("{oops")
    assert TokenStore(p).load() is None
    p.write_text('["x"]')
    assert TokenStore(p).load() is None


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeAuth:
    fake = FakeAuth()
    real = factory.token_manager
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("DHAN_CLIENT_ID", "1000000001")
    for var in ("DHAN_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    def tm(settings: object, secrets: object, store_path: Path | None = None) -> TokenManager:
        m = real(settings, secrets, tmp_path / "store.json")
        m._http = httpx.Client(transport=httpx.MockTransport(fake.handler))
        m._clock = lambda: NOW
        return m

    monkeypatch.setattr(cli, "token_manager", tm)
    monkeypatch.setattr(cli, "now_ist", lambda: NOW)
    return fake


def test_cli_dhan_token(
    cli_env: FakeAuth, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["dhan-token"]) == 1
    assert "dhan-login" in capsys.readouterr().out
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", jwt(NOW + timedelta(hours=10)))
    assert cli.main(["dhan-token"]) == 0
    assert "Dhan token OK; expires Mon 05 Oct 18:30 IST" in capsys.readouterr().out
    assert cli.main(["dhan-token", "--renew"]) == 0
    assert "(renewed)" in capsys.readouterr().out


def test_cli_dhan_login(
    cli_env: FakeAuth, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "1234")
    monkeypatch.setattr("builtins.input", lambda prompt: "123456")
    assert cli.main(["dhan-login"]) == 0
    assert "logged in; token saved" in capsys.readouterr().out
    cli_env.login_status = 401
    assert cli.main(["dhan-login"]) == 1


def test_factory_uses_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from nifbot.config import Secrets, load_settings

    monkeypatch.setattr(factory, "TOKEN_FILE", tmp_path / "t.json")
    monkeypatch.setenv("DHAN_CLIENT_ID", "1")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", jwt(datetime.now(tz=IST) + timedelta(hours=20)))
    client = factory.dhan_client(load_settings(), Secrets(_env_file=None))
    client.close()
    monkeypatch.delenv("DHAN_CLIENT_ID")
    with pytest.raises(DataError, match="DHAN_CLIENT_ID"):
        factory.dhan_client(load_settings(), Secrets(_env_file=None))


def test_data_check_token_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nifbot import readiness
    from nifbot.config import Secrets

    monkeypatch.setenv("DHAN_CLIENT_ID", "1")
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    store = tmp_path / "tok.json"
    env = readiness.check_environment(Secrets(_env_file=None), tmp_path, tmp_path, store)
    assert env[0].status == "MISSING"
    TokenStore(store).save(jwt(datetime.now(tz=IST) + timedelta(hours=5)), "test")
    env = readiness.check_environment(Secrets(_env_file=None), tmp_path, tmp_path, store)
    assert env[0].status == "OK" and "valid until" in env[0].detail
    TokenStore(store).save(jwt(datetime.now(tz=IST) - timedelta(hours=5)), "test")
    env = readiness.check_environment(Secrets(_env_file=None), tmp_path, tmp_path, store)
    assert env[0].status == "MISSING" and "expired" in env[0].detail
