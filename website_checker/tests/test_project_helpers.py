import pytest

import accordance_tester
import all_paths_checker
import experd_filters_tester
import filters_tester
import security_check


@pytest.mark.parametrize(
    ("base_url", "path_or_url", "expected"),
    [
        ("https://example.com", "/about/", "https://example.com/about/"),
        (
            "https://example.com/",
            "https://other.example/page",
            "https://other.example/page",
        ),
    ],
)
def test_build_url(base_url, path_or_url, expected):
    assert all_paths_checker.build_url(base_url, path_or_url) == expected


def test_load_paths_skips_comments_and_blank_lines(tmp_path):
    path_file = tmp_path / "paths.txt"
    path_file.write_text("# comment\n\n/about/\n/contact/\n", encoding="utf-8")

    assert all_paths_checker.load_paths(str(path_file)) == ["/about/", "/contact/"]


def test_all_paths_checker_defaults_to_all_paths_file(monkeypatch):
    monkeypatch.setattr("sys.argv", ["all_paths_checker.py"])

    assert all_paths_checker.parse_arguments().input == "all_paths.txt"


def test_filters_load_target_uses_default_site_for_relative_path(tmp_path):
    path_file = tmp_path / "target.txt"
    path_file.write_text("# target\n/engineers/\n", encoding="utf-8")

    assert filters_tester.load_target(str(path_file)) == (
        "https://dev.hire.engineer/engineers/"
    )


@pytest.mark.parametrize(
    ("filter_name", "value", "expected"),
    [
        ("seniority", "Tech Lead", "lead"),
        ("availability", "Available Soon", "soon"),
        ("position", "Python Developer", "Python Developer"),
    ],
)
def test_expected_query_value(filter_name, value, expected):
    assert filters_tester.expected_query_value(filter_name, value) == expected


def test_query_value_returns_first_value_and_empty_for_missing_parameter():
    url = "https://example.com/?tag=python&tag=qa"

    assert filters_tester.query_value(url, "tag") == "python"
    assert filters_tester.query_value(url, "missing") == ""


@pytest.mark.parametrize(
    ("module", "tags", "expected_tag", "expected"),
    [
        (filters_tester, {"javascript"}, " JavaScript ", True),
        (filters_tester, {"javascript"}, "script", False),
        (experd_filters_tester, {"4d emr"}, "4D EMR", True),
        (experd_filters_tester, {"4d emr"}, "4D", False),
    ],
)
def test_tag_matches_normalizes_case_and_whitespace_but_requires_exact_match(
    module, tags, expected_tag, expected
):
    assert module.tag_matches(tags, expected_tag) is expected


@pytest.mark.parametrize(
    ("filter_name", "value", "matching_urls"),
    [
        ("position", "Python Developer", {"python"}),
        ("seniority", "Senior", {"senior"}),
        ("availability", "Available Now", {"available"}),
    ],
)
def test_matching_cards_returns_only_cards_matching_filter(
    filter_name, value, matching_urls
):
    cards = [
        {
            "url": "python",
            "position": "Python Developer",
            "text": "Junior\nAvailable Soon",
        },
        {
            "url": "senior",
            "position": "Frontend Developer",
            "text": "Senior\nNot available",
        },
        {
            "url": "available",
            "position": "Frontend Developer",
            "text": "Regular\nNow",
        },
    ]

    matching = filters_tester.matching_cards(cards, filter_name, value)

    assert {card["url"] for card in matching} == matching_urls


def test_experd_load_target_accepts_absolute_url(tmp_path):
    path_file = tmp_path / "target.txt"
    path_file.write_text(
        "# target\nhttps://experd.io/case-studies/\n", encoding="utf-8"
    )

    assert experd_filters_tester.load_target(str(path_file)) == (
        "https://experd.io/case-studies/"
    )


def test_experd_load_target_rejects_example_placeholder(tmp_path):
    path_file = tmp_path / "target.txt"
    path_file.write_text("https://example.com/case-studies/\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Zastąp przykładowy adres URL"):
        experd_filters_tester.load_target(str(path_file))


def test_normalize_url_trailing_slash_and_fragment():
    assert accordance_tester.normalize_url(
        "https://hire.engineer/engineers?page=2#results"
    ) == "https://hire.engineer/engineers/?page=2"


def test_load_sites_classifies_and_normalizes_urls(tmp_path):
    path_file = tmp_path / "sites.txt"
    path_file.write_text(
        "# technology and engineers pages\n"
        "https://hire.engineer/engineers\n"
        "https://hire.engineer/technologies/python\n",
        encoding="utf-8",
    )

    assert accordance_tester.load_sites(str(path_file)) == (
        "https://hire.engineer/technologies/python/",
        "https://hire.engineer/engineers/",
    )


def test_load_sites_requires_both_page_types(tmp_path):
    path_file = tmp_path / "sites.txt"
    path_file.write_text(
        "https://hire.engineer/engineers/\n"
        "https://hire.engineer/about/\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="Plik wejściowy musi zawierać jeden adres strony /technologies/",
    ):
        accordance_tester.load_sites(str(path_file))


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("https://example.com/a", "https://example.com/b", True),
        ("https://example.com", "http://example.com", False),
        ("https://example.com", "https://example.com:444", False),
        ("https://example.com", "https://other.example", False),
    ],
)
def test_same_origin(first, second, expected):
    assert security_check.same_origin(first, second) is expected


@pytest.mark.parametrize(
    ("findings", "threshold", "expected"),
    [
        ([{"severity": "high"}], "medium", True),
        ([{"severity": "medium"}], "medium", True),
        ([{"severity": "low"}], "medium", False),
        ([{"severity": "high"}], None, False),
        ([], "low", False),
    ],
)
def test_security_gate_threshold(findings, threshold, expected):
    assert security_check.meets_fail_threshold(findings, threshold) is expected


def test_inspect_headers_detects_missing_headers_and_records_observed_values():
    findings = []

    observed = security_check.inspect_headers(
        "https://example.com/",
        {
            "content-security-policy-report-only": "default-src 'self'",
            "x-content-type-options": "nosniff",
        },
        findings,
    )

    assert observed["security_headers"] == {
        "content-security-policy-report-only": "default-src 'self'",
        "x-content-type-options": "nosniff",
    }
    assert {finding["check"] for finding in findings} == {
        "HSTS",
        "Polityka bezpieczeństwa treści (CSP)",
        "Ochrona przed clickjackingiem",
        "Polityka przekazywania adresu odsyłającego",
    }
    csp_finding = next(
        finding
        for finding in findings
        if finding["check"] == "Polityka bezpieczeństwa treści (CSP)"
    )
    assert "wyłącznie wersja raportująca CSP" in csp_finding["detail"]


def test_inspect_headers_does_not_flag_hsts_for_http():
    findings = []

    security_check.inspect_headers("http://example.com/", {}, findings)

    assert "HSTS" not in {finding["check"] for finding in findings}
