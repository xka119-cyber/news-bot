from scripts.check_secrets import find_problems, is_forbidden_file, read_env_values

FAKE_TELEGRAM = "123456789" + ":" + "b" * 35


def test_env_files_are_forbidden():
    assert is_forbidden_file(".env")
    assert is_forbidden_file("sub/.env.local")
    assert not is_forbidden_file(".env.example")


def test_token_is_reported_without_its_value():
    problems = find_problems("a.py", f"x = 1\ntoken = '{FAKE_TELEGRAM}'\n", [])
    assert len(problems) == 1
    assert problems[0].startswith("a.py:2")
    assert FAKE_TELEGRAM not in problems[0]


def test_real_env_value_is_detected_without_its_value():
    problems = find_problems("a.py", "id = 17841400000", [("IG_USER_ID", "17841400000")])
    assert len(problems) == 1
    assert "IG_USER_ID" in problems[0]
    assert "17841400000" not in problems[0]


def test_read_env_values_skips_empty_and_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# note=longcomment\nEMPTY=\nSHORT=abc\nKEY='long-value-1'\n", encoding="utf-8")
    assert read_env_values(env) == [("KEY", "long-value-1")]
