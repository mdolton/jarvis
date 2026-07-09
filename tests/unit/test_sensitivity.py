from jarvis.mcp.sensitivity import extract_sensitivity_terms, find_sensitive_match


def test_extract_terms_from_marked_preferences():
    prefs = [
        "Prefers metric units",
        "sensitive: mom@example.com, Salary; therapist",
        "SENSITIVE: mom@example.com",
    ]
    assert extract_sensitivity_terms(prefs) == ["mom@example.com", "salary", "therapist"]


def test_extract_ignores_unmarked_and_empty():
    assert extract_sensitivity_terms(["no marker here", "sensitive:", ""]) == []


def test_match_in_nested_arguments():
    terms = ["mom@example.com", "salary"]
    args = {"to": [{"email": "Mom@Example.com"}], "body": "hi"}
    assert find_sensitive_match(terms, tool_name="create_draft", arguments=args) == (
        "mom@example.com"
    )


def test_match_in_tool_name_only():
    assert find_sensitive_match(["payroll"], tool_name="get_payroll_report") == "payroll"


def test_no_match_returns_none():
    assert find_sensitive_match(["salary"], tool_name="create_draft", arguments={"to": "x"}) is None
    assert find_sensitive_match([], tool_name="anything", arguments={"salary": 1}) is None
