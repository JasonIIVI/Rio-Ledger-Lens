import os

from ledgerlens.env import load_dotenv, parse_dotenv


def test_parse_handles_comments_quotes_export_and_empties():
    text = """
    # a comment
    PLAIN=value
    export EXPORTED=yes
    QUOTED="with spaces"
    SINGLE='single'
    TRAILING=abc # inline comment
    EMPTY=
    NOT_A_PAIR
    """
    assert parse_dotenv(text) == {
        "PLAIN": "value",
        "EXPORTED": "yes",
        "QUOTED": "with spaces",
        "SINGLE": "single",
        "TRAILING": "abc",
    }


def test_load_never_overrides_a_real_environment_variable(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("NEW_KEY=fresh\nEXISTING=from-file\n")
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.setenv("EXISTING", "from-shell")

    applied = load_dotenv(env_file)

    assert applied == {"NEW_KEY": "fresh"}
    assert os.environ["NEW_KEY"] == "fresh"
    assert os.environ["EXISTING"] == "from-shell"


def test_missing_file_is_a_noop(tmp_path):
    assert load_dotenv(tmp_path / "absent.env") == {}
