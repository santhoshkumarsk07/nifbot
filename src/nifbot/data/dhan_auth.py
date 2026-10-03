"""Dhan access-token lifecycle: read expiry, renew, quick PIN+TOTP login.

Dhan access tokens are valid for about 24 hours. Instead of pasting a new one
every day:

1. ``ensure_valid()`` renews a still-valid token through Dhan's ``/RenewToken``
   endpoint when it is close to expiry (no user action).
2. If the token already expired, ``login_pin_totp()`` gets a new one from your
   PIN and the 6-digit code from your authenticator app (``nifbot dhan-login``).
3. Optional and OFF by default: with ``DHAN_PIN`` and ``DHAN_TOTP_SECRET`` in
   ``.env`` and ``broker.dhan.auto_login_totp: true`` the bot logs in by itself.
   Anyone who can read that file can then log in to your Dhan account.

The current token is kept in ``data/secrets/dhan_token.json`` (permissions 600).
JWT payloads are only *read* for the expiry time; the token is never trusted for
anything else and never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from nifbot.data.adapter import DataError
from nifbot.timeutil import IST, now_ist

log = logging.getLogger(__name__)

API_BASE = "https://api.dhan.co/v2"
AUTH_BASE = "https://auth.dhan.co"


def token_expiry(token: str) -> datetime | None:
    """Expiry (IST) from the JWT ``exp`` claim, or None if it cannot be read."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return datetime.fromtimestamp(int(exp), tz=IST)
    except (ValueError, KeyError, TypeError):
        return None


def totp_now(secret_b32: str, at: float | None = None, digits: int = 6, step: int = 30) -> str:
    """RFC 6238 TOTP (SHA-1), same codes as Google Authenticator / Authy."""
    key = base64.b32decode(secret_b32.replace(" ", "").upper() + "=" * (-len(secret_b32) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**digits
    return str(code).zfill(digits)


def _extract_token(body: Any) -> str:
    """Find the access token in Dhan's auth responses (field names vary by endpoint)."""
    if isinstance(body, dict):
        for key in ("accessToken", "access_token", "token"):
            val = body.get(key)
            if isinstance(val, str) and val.count(".") == 2:
                return val
        if "data" in body:
            return _extract_token(body["data"])
    raise DataError("Dhan auth: no access token in response")


@dataclass(frozen=True)
class TokenState:
    token: str
    expires_at: datetime | None
    source: str  # "store" or "env"

    def remaining(self, now: datetime) -> timedelta | None:
        return None if self.expires_at is None else self.expires_at - now


class TokenStore:
    """Single JSON file with the current token (mode 600)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        tok = data.get("access_token") if isinstance(data, dict) else None
        return tok if isinstance(tok, str) else None

    def save(self, token: str, how: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exp = token_expiry(token)
        body = {
            "access_token": token,
            "expires_at": exp.isoformat() if exp else None,
            "obtained_via": how,
            "saved_at": now_ist().isoformat(),
        }
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        os.replace(tmp, self.path)


class TokenManager:
    """Chooses the freshest token and keeps it valid."""

    def __init__(
        self,
        client_id: str,
        store: TokenStore,
        env_token: str | None = None,
        *,
        renew_before: timedelta = timedelta(hours=8),
        pin: str | None = None,
        totp_secret: str | None = None,
        auto_login_totp: bool = False,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = now_ist,
        timeout: float = 15.0,
    ) -> None:
        if not client_id:
            raise DataError("DHAN_CLIENT_ID is not set")
        self._client_id = client_id
        self._store = store
        self._env = env_token if env_token and env_token.count(".") == 2 else None  # JWT only
        self._renew_before = renew_before
        self._pin = pin
        self._totp_secret = totp_secret
        self._auto = auto_login_totp
        self._clock = clock
        self._http = httpx.Client(timeout=timeout, transport=transport, verify=True)

    def close(self) -> None:
        self._http.close()

    def current(self) -> TokenState | None:
        """The candidate with the latest expiry among stored and .env tokens."""
        cands = []
        stored = self._store.load()
        if stored:
            cands.append(TokenState(stored, token_expiry(stored), "store"))
        if self._env:
            cands.append(TokenState(self._env, token_expiry(self._env), "env"))
        if not cands:
            return None
        floor = datetime.min.replace(tzinfo=IST)
        return max(cands, key=lambda c: c.expires_at or floor)

    def _check(self, resp: httpx.Response, what: str) -> Any:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code != 200:
            msg = ""
            if isinstance(body, dict):
                msg = str(body.get("errorMessage") or body.get("message") or "")[:150]
            raise DataError(f"Dhan {what} failed: HTTP {resp.status_code} {msg}".strip())
        return body

    def renew(self, token: str) -> str:
        """Exchange a still-valid token for a fresh one (Dhan /RenewToken)."""
        try:
            resp = self._http.get(
                f"{API_BASE}/RenewToken",
                headers={"access-token": token, "dhanClientId": self._client_id},
            )
        except httpx.HTTPError as exc:
            raise DataError(f"Dhan renew: network error ({type(exc).__name__})") from None
        new = _extract_token(self._check(resp, "renew"))
        self._store.save(new, "renew")
        return new

    def login_pin_totp(self, pin: str, totp: str) -> str:
        """New token from PIN + current authenticator code."""
        if not (pin.isdigit() and totp.isdigit() and len(totp) == 6):
            raise DataError("PIN must be digits and TOTP must be the 6-digit code")
        try:
            resp = self._http.post(
                f"{AUTH_BASE}/app/generateAccessToken",
                params={"dhanClientId": self._client_id, "pin": pin, "totp": totp},
            )
        except httpx.HTTPError as exc:
            raise DataError(f"Dhan login: network error ({type(exc).__name__})") from None
        new = _extract_token(self._check(resp, "login"))
        self._store.save(new, "pin_totp")
        return new

    def ensure_valid(self) -> str:
        """Return a usable token, renewing or (if enabled) logging in as needed."""
        now = self._clock()
        cur = self.current()
        if cur is not None:
            left = cur.remaining(now)
            if left is None or left > self._renew_before:
                return cur.token
            if left > timedelta(minutes=1):
                try:
                    return self.renew(cur.token)
                except DataError as exc:
                    log.warning("token renewal failed: %s", exc)
                    return cur.token  # still valid for now; renewal retried next run
        if self._auto and self._pin and self._totp_secret:
            log.info("token expired; logging in with stored PIN + TOTP secret")
            return self.login_pin_totp(self._pin, totp_now(self._totp_secret))
        raise DataError(
            "Dhan access token expired or missing. Run `nifbot dhan-login` "
            "(PIN + 6-digit code from your authenticator app)."
        )
