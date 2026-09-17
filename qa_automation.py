#!/usr/bin/env python3
"""Simple QA automation script.

Features:
- scans a selected project directory
- validates common file types (JSON, Python, text/markdown)
- logs console messages with clear pass/fail status
- generates a report in HTML and JSON
- works as a single-file solution
"""

import argparse
import ast
import json
import sys
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List


@dataclass
class CheckResult:
    file: str
    status: str
    details: str
    size: int = 0


def log_info(message: str) -> None:
    print(f"[INFO] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}")


def log_error(message: str) -> None:
    print(f"[ERROR] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}", file=sys.stderr)


def scan_file(file_path: Path) -> CheckResult:
    size = 0
    try:
        size = file_path.stat().st_size
        suffix = file_path.suffix.lower()

        if suffix == ".json":
            with file_path.open("r", encoding="utf-8") as fh:
                json.load(fh)
        elif suffix == ".py":
            source = file_path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(file_path))
        elif suffix in {".txt", ".md", ".log"}:
            if size == 0:
                raise ValueError("file is empty")

        return CheckResult(
            file=str(file_path),
            status="PASS",
            details="Validation successful",
            size=size,
        )
    except (OSError, UnicodeDecodeError, ValueError, SyntaxError, json.JSONDecodeError) as exc:
        return CheckResult(
            file=str(file_path),
            status="FAIL",
            details=f"{type(exc).__name__}: {exc}",
            size=size,
        )


def scan_directory(root_path: Path) -> List[CheckResult]:
    results: List[CheckResult] = []

    for file_path in sorted(root_path.rglob("*")):
        if file_path.is_dir():
            continue

        if any(part in {".git", ".venv", "venv", "__pycache__"} for part in file_path.parts):
            continue

        log_info(f"Checking file: {file_path}")
        result = scan_file(file_path)
        results.append(result)

        if result.status == "PASS":
            log_info(f"PASS - {result.file}")
        else:
            log_error(f"FAIL - {result.file} :: {result.details}")

    return results


def generate_html_report(root_path: Path, results: List[CheckResult]) -> str:
    passed = sum(1 for item in results if item.status == "PASS")
    failed = sum(1 for item in results if item.status == "FAIL")

    rows = "".join(
        f"""
        <tr>
            <td>{item.file}</td>
            <td class=\"{item.status.lower()}\">{item.status}</td>
            <td>{item.details}</td>
            <td>{item.size}</td>
        </tr>
        """ for item in results
    )

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    <title>QA Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 30px; background: #f7f7f7; color: #222; }}
        h1 {{ margin-bottom: 20px; }}
        .summary {{ margin-bottom: 20px; }}
        .badge {{ display: inline-block; padding: 6px 12px; border-radius: 12px; font-weight: bold; color: #fff; }}
        .pass {{ background: #2e7d32; }}
        .fail {{ background: #c62828; }}
        table {{ border-collapse: collapse; width: 100%; background: white; }}
        th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; vertical-align: top; }}
        th {{ background: #eeeeee; }}
        .meta {{ margin-bottom: 20px; color: #555; }}
    </style>
</head>
<body>
    <h1>QA Automation Report</h1>
    <div class=\"meta\">Scanned path: {root_path}</div>
    <div class=\"summary\">
        <span class=\"badge pass\">PASS: {passed}</span>
        <span class=\"badge fail\">FAIL: {failed}</span>
    </div>

    <table>
        <thead>
            <tr>
                <th>File</th>
                <th>Status</th>
                <th>Details</th>
                <th>Size (bytes)</th>
            </tr>
        </thead>
        <tbody>
            {rows}
        </tbody>
    </table>
</body>
</html>
"""


def generate_json_report(results: List[CheckResult], root_path: Path) -> str:
    data = {
        "generated_at": datetime.now().isoformat(),
        "scanned_path": str(root_path),
        "summary": {
            "total": len(results),
            "pass": sum(1 for item in results if item.status == "PASS"),
            "fail": sum(1 for item in results if item.status == "FAIL"),
        },
        "results": [asdict(item) for item in results],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="QA automation script with report generation")
    parser.add_argument("--path", default=".", help="Path to project directory to scan")
    parser.add_argument("--report", default="qa_report.html", help="Output report file name")
    args = parser.parse_args()

    try:
        root_path = Path(args.path).resolve()
        if not root_path.exists():
            raise FileNotFoundError(f"Directory does not exist: {root_path}")
        if not root_path.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {root_path}")

        log_info(f"Starting QA scan for: {root_path}")
        results = scan_directory(root_path)

        html_report_path = Path(args.report).resolve()
        html_report_path.write_text(generate_html_report(root_path, results), encoding="utf-8")

        json_report_path = html_report_path.with_suffix(".json")
        json_report_path.write_text(generate_json_report(results, root_path), encoding="utf-8")

        passed = sum(1 for item in results if item.status == "PASS")
        failed = sum(1 for item in results if item.status == "FAIL")

        log_info(f"QA scan completed. PASS: {passed}, FAIL: {failed}")
        log_info(f"HTML report saved to: {html_report_path}")
        log_info(f"JSON report saved to: {json_report_path}")

        if failed > 0:
            log_error("One or more checks failed. Review the report for details.")
            return 1

        return 0

    except Exception as exc:
        log_error(f"Fatal error: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
