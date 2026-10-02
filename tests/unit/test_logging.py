"""Secret redaction and audit log."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from nifbot.logging_setup import REDACTED, Redactor, audit, setup_logging

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopq"


def test_redactor_known_and_pattern_secrets() -> None:
    r = Redactor(["my-dhan-secret"])
    assert "my-dhan-secret" not in r("token is my-dhan-secret")
    assert TOKEN not in r(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    assert r("access_token=abc123xyz") == f"access_token={REDACTED}"
    assert REDACTED in r("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4")
    assert r("nothing secret here") == "nothing secret here"


def test_files_are_redacted_and_private(tmp_path: Path) -> None:
    setup_logging(tmp_path, ["dhan-secret-value"], console=True)
    log = logging.getLogger("nifbot.test")
    log.info("using %s and %s", "dhan-secret-value", TOKEN)
    try:
        raise RuntimeError("boom dhan-secret-value")
    except RuntimeError:
        log.exception("failed")
    audit("button_press", chat_id=1, note="dhan-secret-value")
    for handler in logging.getLogger().handlers + logging.getLogger("nifbot.audit").handlers:
        handler.flush()

    main_log = (tmp_path / "nifbot.log").read_text()
    audit_log = (tmp_path / "audit.log").read_text()
    for text in (main_log, audit_log):
        assert "dhan-secret-value" not in text
        assert TOKEN not in text
    lines = [json.loads(line) for line in main_log.splitlines()]
    assert lines[0]["ts"].endswith("+05:30")
    assert "exc" in lines[1]
    rec = json.loads(audit_log.splitlines()[0])
    assert rec["event"] == "button_press" and rec["chat_id"] == "1"
    if os.name == "posix":
        assert (tmp_path / "audit.log").stat().st_mode & 0o077 == 0
