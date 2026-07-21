"""Unit tests for the dependency-free .env loader (config._apply_dotenv).

The parser is tested directly against a dict env, so it never touches os.environ
or the real project .env. The auto-load wrapper (_load_dotenv) is skipped under
pytest by design.
"""

from vibedentify.config import _apply_dotenv


def _write(tmp_path, body):
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


def test_dotenv_sets_absent_keys(tmp_path):
    env = {}
    applied = _apply_dotenv(_write(tmp_path, "FOO=bar\nBAZ=qux\n"), env)
    assert env == {"FOO": "bar", "BAZ": "qux"}
    assert applied == {"FOO": "bar", "BAZ": "qux"}


def test_dotenv_real_env_wins(tmp_path):
    # a variable already present is never overwritten; only the gap is filled
    env = {"FOO": "real"}
    applied = _apply_dotenv(_write(tmp_path, "FOO=fromfile\nBAR=baz\n"), env)
    assert env["FOO"] == "real"
    assert env["BAR"] == "baz"
    assert "FOO" not in applied and applied == {"BAR": "baz"}


def test_dotenv_tolerant_parsing(tmp_path):
    body = (
        "\n"
        "# a comment\n"
        "   # indented comment\n"
        "export EXPORTED=yes\n"
        'QUOTED="he said hi"\n'
        "SQUOTED='single'\n"
        "SPACED =  padded  \n"
        "malformed line without equals\n"
        "=noKey\n"
    )
    env = {}
    _apply_dotenv(_write(tmp_path, body), env)
    assert env["EXPORTED"] == "yes"  # 'export ' prefix accepted
    assert env["QUOTED"] == "he said hi"  # surrounding double quotes stripped
    assert env["SQUOTED"] == "single"  # surrounding single quotes stripped
    assert env["SPACED"] == "padded"  # key/value whitespace trimmed
    assert "malformed line without equals" not in env  # no '=' -> ignored
    assert "" not in env  # empty key ('=noKey') -> ignored


def test_dotenv_missing_file_is_noop(tmp_path):
    env = {"X": "1"}
    assert _apply_dotenv(tmp_path / "does-not-exist.env", env) == {}
    assert env == {"X": "1"}
