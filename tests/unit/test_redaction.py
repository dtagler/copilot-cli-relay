import copilot_cli_relay.logging_setup as logging_setup
from copilot_cli_relay.logging_setup import redact_bytes, redact_text


def test_redacts_gh_tokens():
    s = "header=gho_AbCdEf0123456789ABCDEF0123456789abcdEFGH and ghu_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx1234"
    out = redact_text(s)
    assert "gho_" in out  # prefix kept (we redact suffix)
    assert "***REDACTED***" in out


def test_redacts_authorization_header_text():
    s = "Authorization: Bearer gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    out = redact_text(s)
    assert "Bearer gho_" not in out
    assert "***REDACTED***" in out


def test_redact_bytes_truncates_data_uri():
    big_b64 = "A" * 5000
    body = f'{{"image":"data:image/png;base64,{big_b64}"}}'.encode()
    out = redact_bytes(body)
    assert b"truncated" in out
    assert len(out) < len(body)


def test_redacts_jwt():
    s = 'token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c'
    out = redact_text(s)
    assert "***REDACTED***" in out
    assert "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c" not in out


def test_redacts_sk_keys():
    s = 'k=sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz1234567890 and sk-ant-aaaaaaaaaaaaaaaaaaaaaa'
    out = redact_text(s)
    assert "sk-proj-***REDACTED***" in out
    assert "sk-ant-***REDACTED***" in out
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz1234567890" not in out


def test_redacts_aws_access_key():
    s = "creds AKIAIOSFODNN7EXAMPLE in body"
    out = redact_text(s)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "AKIA***REDACTED***" in out


def test_redacts_x_api_key_header():
    s = "x-api-key: super-secret-value-1234"
    out = redact_text(s)
    assert "super-secret-value-1234" not in out
    assert "***REDACTED***" in out


def test_redacts_bearer_outside_authorization():
    s = '{"value": "Bearer abcdefghijklmnopqrstuvwxyz0123456789"}'
    out = redact_text(s)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in out
    assert "Bearer ***REDACTED***" in out


def test_redacts_json_secret_keys():
    s = '{"api_key": "abcdefghijkl1234", "password": "hunter2hunter2", "other": "keep-me"}'
    out = redact_text(s)
    assert "abcdefghijkl1234" not in out
    assert "hunter2hunter2" not in out
    assert "keep-me" in out
    assert out.count("***REDACTED***") == 2


def test_redacts_basic_auth_full_value():
    # Regression: the prior \S+ pattern only redacted the scheme word ("Basic"),
    # leaving the base64 credential blob visible.
    s = "Authorization: Basic dXNlcjpwYXNzd29yZA=="
    out = redact_text(s)
    assert "dXNlcjpwYXNzd29yZA" not in out
    assert "***REDACTED***" in out


def test_redacts_non_gh_bearer_in_header():
    # Regression: any non-gh_/non-sk_ token after `Bearer` would leak via the
    # header regex matching only \S+. Now the whole value to end-of-line is gone.
    s = "Authorization: Bearer notghprefix-secret-value-9999"
    out = redact_text(s)
    assert "notghprefix-secret-value-9999" not in out
    assert "***REDACTED***" in out


def test_header_redaction_stops_at_json_quote():
    # When headers are embedded in a JSON object, redaction must not eat past
    # the closing quote.
    s = '{"Authorization": "Bearer abc-def-ghi-jkl-mno", "next": "ok"}'
    out = redact_text(s)
    assert "abc-def-ghi-jkl-mno" not in out
    assert '"next": "ok"' in out


def test_header_redaction_stops_at_comma():
    s = "Authorization: Bearer abc-secret, X-Trace: keep-me"
    out = redact_text(s)
    assert "abc-secret" not in out
    assert "keep-me" in out


def test_redacts_dict_repr_authorization():
    # Headers shown in Python dict-repr form (e.g. inside an upstream error
    # body or a logged exception) — quoted key, quoted value. The plain
    # `Authorization:` regex can't match this because the value starts with a
    # quote (a terminator); a quoted-form regex handles it.
    s = "{'Authorization': 'Basic dXNlcjpwYXNzd29yZA==', 'next': 'ok'}"
    out = redact_text(s)
    assert "dXNlcjpwYXNzd29yZA" not in out
    assert "***REDACTED***" in out
    assert "'next': 'ok'" in out


def test_redacts_json_repr_x_api_key():
    s = '{"x-api-key": "abc-secret-value-9999", "trace": "keep"}'
    out = redact_text(s)
    assert "abc-secret-value-9999" not in out
    assert "***REDACTED***" in out
    assert '"trace": "keep"' in out


def test_redacts_single_quoted_python_repr_secret_keys():
    """Python dict-repr form (single quotes) of secret-named keys must redact too.
    This shows up when an exception or repr() of a dict containing credentials
    leaks into a log line."""
    s = "{'api_key': 'leaked-single-quoted-1234', 'other': 'keep-me'}"
    out = redact_text(s)
    assert "leaked-single-quoted-1234" not in out
    assert "***REDACTED***" in out
    assert "'other': 'keep-me'" in out


def test_redacts_double_quoted_password_value_containing_apostrophe():
    """Regression: a JSON-quoted secret value that legitimately contains a
    single quote must still be fully redacted (the dual-quote regex must not
    stop early at an inner apostrophe)."""
    s = '{"password": "hunter2\'apostrophe", "next": "ok"}'
    out = redact_text(s)
    assert "hunter2" not in out
    assert "apostrophe" not in out
    assert '"next": "ok"' in out


def test_redacts_github_fine_grained_pat():
    s = "token=github_pat_11ABCDEFG0abcdefghij_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890"
    out = redact_text(s)
    assert "ABCDEFG0abcdefghij" not in out
    assert "github_pat_***REDACTED***" in out


def test_redacts_aws_secret_access_key_when_named():
    s = 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
    out = redact_text(s)
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in out
    assert "***REDACTED***" in out


def test_redacts_slack_token():
    s = "slack=xoxb-1234567890-9876543210-AbCdEfGhIjKlMnOp"
    out = redact_text(s)
    assert "1234567890-9876543210" not in out
    assert "xoxb-***REDACTED***" in out


def test_redacts_stripe_key():
    s = "stripe_key=sk_live_AbCdEfGhIjKlMnOpQrStUvWx and rk_live_1234567890abcdef"
    out = redact_text(s)
    assert "AbCdEfGhIjKlMnOpQrStUvWx" not in out
    assert "1234567890abcdef" not in out
    assert out.count("***REDACTED***") == 2


def test_redacts_google_api_key():
    s = "google=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI"
    out = redact_text(s)
    assert "SyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI" not in out
    assert "AIza***REDACTED***" in out


def test_redact_bytes_returns_body_if_redaction_fails(monkeypatch):
    body = b"not-secret"

    def fail_redaction(text):
        raise RuntimeError("redaction failure")

    monkeypatch.setattr(logging_setup, "redact_text", fail_redaction)
    assert logging_setup.redact_bytes(body) == body
