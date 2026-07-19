from pathlib import Path

from devclean.evidence.redaction import redact_argument, redact_path, redact_text


def test_redact_path_replaces_home_case_insensitively() -> None:
    result = redact_path(r"C:\Users\Alice\Projects\client", home=Path(r"C:\Users\Alice"))
    assert "Alice" not in result
    assert result.startswith("<USER_HOME>")


def test_redact_text_removes_common_secrets() -> None:
    text = "token=abc123 Bearer ey.secret https://alice:pass@example.test/repo"
    result = redact_text(text, home=Path(r"C:\Users\Nobody"))
    assert "abc123" not in result
    assert "ey.secret" not in result
    assert "alice:pass" not in result
    assert result.count("<REDACTED>") >= 3


def test_redact_argument_broadly_replaces_windows_and_posix_absolute_paths() -> None:
    assert redact_argument(r"C:\Users\Alice\project\tool.exe") == r"C:\<REDACTED_PATH>"
    assert redact_argument("/home/alice/private/tool") == "<ABSOLUTE_PATH>"
    assert redact_argument("--root=/home/alice/private") == "--root=<ABSOLUTE_PATH>"
