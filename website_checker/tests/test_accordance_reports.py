import csv
import json

import accordance_tester


def test_write_reports_localizes_html_csv_and_json(tmp_path, monkeypatch):
    monkeypatch.setattr(accordance_tester, "REPORTS_DIR", tmp_path)
    report = {
        "technology_index": "https://example.test/technologies/",
        "engineers_page": "https://example.test/engineers/",
        "technology_count": 2,
        "engineers_page_count": 1,
        "technology_tags": [
            {
                "technology": "Python",
                "url": "https://example.test/technologies/python/",
                "engineer_count": 1,
                "missing_from_engineers_page": [
                    "https://example.test/engineers/alice/"
                ],
                "duplicate_engineers": [],
                "profile_tag_mismatches": [
                    "https://example.test/engineers/alice/"
                ],
                "status": "ERROR",
                "message": "Wykryto niezgodność.",
                "checked_at": "2026-10-01T12:00:00+00:00",
            },
            {
                "technology": "Ruby",
                "url": "https://example.test/technologies/ruby/",
                "engineer_count": 1,
                "missing_from_engineers_page": [],
                "duplicate_engineers": [],
                "profile_tag_mismatches": [],
                "status": "OK",
                "message": "Wszyscy oznaczeni inżynierowie znajdują się na stronie inżynierów.",
                "checked_at": "2026-10-01T12:00:00+00:00",
            },
        ],
        "contradictions": ["Wykryto niezgodność."],
        "checked_at": "2026-10-01T12:00:00+00:00",
    }

    accordance_tester.write_reports(report)

    html_report = (tmp_path / "accordance_report.html").read_text(encoding="utf-8")
    assert '<html lang="pl">' in html_report
    assert "Raport zgodności stron" in html_report
    assert "Błędy i sprzeczności" in html_report
    assert "Brak na stronie inżynierów" in html_report
    assert "Brak znacznika technologii w profilu inżyniera" in html_report
    assert "<td>NIEZGODNY</td>" in html_report
    assert "<td>ZGODNY</td>" in html_report
    assert "Szczegóły" in html_report

    with (tmp_path / "accordance_report.csv").open(
        encoding="utf-8", newline=""
    ) as file:
        csv_report = list(csv.DictReader(file))
    assert "Liczba inżynierów" in csv_report[0]
    assert csv_report[0]["Status"] == "NIEZGODNY"
    assert csv_report[1]["Status"] == "ZGODNY"

    json_report = json.loads(
        (tmp_path / "accordance_report.json").read_text(encoding="utf-8")
    )
    assert json_report["liczba_technologii"] == 2
    assert json_report["technologie"][0]["status"] == "NIEZGODNY"
    assert json_report["technologie"][1]["status"] == "ZGODNY"
    assert json_report["sprzeczności"] == ["Wykryto niezgodność."]
