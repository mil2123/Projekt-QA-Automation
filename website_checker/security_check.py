import argparse
import asyncio
import csv
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    async_playwright,
)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = "security_check_paths.txt"
REPORTS_DIR = BASE_DIR / "reports"
REPORT_NAME = "security_check"
RATE_LIMIT_HEADERS = (
    "ratelimit",
    "ratelimit-policy",
    "retry-after",
    "x-ratelimit",
)
SESSION_COOKIE_NAME = re.compile(r"(session|sess|auth|token|jwt|sid|login)", re.I)
CSRF_FIELD_NAME = re.compile(
    r"(csrf|xsrf|authenticity_token|requestverificationtoken|_token)",
    re.I,
)
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}


def load_urls(filename: str) -> list[str]:
    path = Path(filename)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.exists():
        raise FileNotFoundError(f"Nie znaleziono pliku wejściowego: {path}")

    urls = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not urls:
        raise ValueError(f"Plik wejściowy nie zawiera adresów URL: {path}")

    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Oczekiwano pełnego adresu HTTP(S), otrzymano: {url}")
    return urls


def same_origin(first_url: str, second_url: str) -> bool:
    first = urlparse(first_url)
    second = urlparse(second_url)
    first_port = first.port or (443 if first.scheme.lower() == "https" else 80)
    second_port = second.port or (443 if second.scheme.lower() == "https" else 80)
    return (
        first.scheme.lower(),
        first.hostname,
        first_port,
    ) == (
        second.scheme.lower(),
        second.hostname,
        second_port,
    )


def add_finding(
    findings: list[dict[str, str]],
    severity: str,
    check: str,
    detail: str,
) -> None:
    findings.append(
        {"severity": severity, "check": check, "detail": detail}
    )


def meets_fail_threshold(
    findings: list[dict[str, str]],
    threshold: str | None,
) -> bool:
    return threshold is not None and any(
        SEVERITY_RANK[finding["severity"]] >= SEVERITY_RANK[threshold]
        for finding in findings
    )


def inspect_headers(
    url: str,
    headers: dict[str, str],
    findings: list[dict[str, str]],
) -> dict[str, Any]:
    headers = {name.lower(): value for name, value in headers.items()}
    observed_security_headers = {
        name: headers[name]
        for name in (
            "strict-transport-security",
            "content-security-policy",
            "content-security-policy-report-only",
            "x-content-type-options",
            "x-frame-options",
            "referrer-policy",
            "permissions-policy",
        )
        if name in headers
    }

    if urlparse(url).scheme == "https" and "strict-transport-security" not in headers:
        add_finding(
            findings,
            "medium",
            "HSTS",
            "Brak nagłówka Strict-Transport-Security.",
        )
    if "content-security-policy" not in headers:
        detail = (
            "Obecna jest wyłącznie wersja raportująca CSP, która nie egzekwuje polityki."
            if "content-security-policy-report-only" in headers
            else "Brak nagłówka Content-Security-Policy."
        )
        add_finding(findings, "medium", "Polityka bezpieczeństwa treści (CSP)", detail)
    if headers.get("x-content-type-options", "").lower() != "nosniff":
        add_finding(
            findings,
            "low",
            "Ochrona przed zgadywaniem typu MIME",
            "Brak nagłówka X-Content-Type-Options: nosniff.",
        )
    has_frame_protection = "x-frame-options" in headers or bool(
        re.search(r"(?:^|;)\s*frame-ancestors\b", headers.get("content-security-policy", ""), re.I)
    )
    if not has_frame_protection:
        add_finding(
            findings,
            "low",
            "Ochrona przed clickjackingiem",
            "Brak nagłówka X-Frame-Options i dyrektywy CSP frame-ancestors.",
        )
    if "referrer-policy" not in headers:
        add_finding(
            findings,
            "low",
            "Polityka przekazywania adresu odsyłającego",
            "Brak nagłówka Referrer-Policy.",
        )

    rate_limit_headers = {
        name: value
        for name, value in headers.items()
        if any(marker in name for marker in RATE_LIMIT_HEADERS)
    }
    rate_limiting: dict[str, Any] = {
        "status": "observed" if rate_limit_headers else "not_observed",
        "headers": rate_limit_headers,
        "note": (
            "W odpowiedzi strony znaleziono nagłówki limitowania żądań; nie potwierdza to jego egzekwowania."
            if rate_limit_headers
            else "Nie znaleziono nagłówków limitowania żądań. Nie wysyłano formularzy, dlatego nie można potwierdzić egzekwowania limitów."
        ),
    }
    return {
        "security_headers": observed_security_headers,
        "rate_limiting": rate_limiting,
    }


def inspect_cookies(
    cookies: list[dict[str, Any]],
    page_url: str,
    findings: list[dict[str, str]],
) -> list[dict[str, Any]]:
    observed: list[dict[str, Any]] = []
    is_https = urlparse(page_url).scheme == "https"
    for cookie in cookies:
        item = {
            "name": cookie["name"],
            "secure": cookie["secure"],
            "http_only": cookie["httpOnly"],
            "same_site": cookie["sameSite"],
        }
        observed.append(item)
        if re.search(r"(csrf|xsrf)", cookie["name"], re.I):
            continue
        if not SESSION_COOKIE_NAME.search(cookie["name"]):
            continue
        if is_https and not cookie["secure"]:
            add_finding(
                findings,
                "medium",
                "Atrybut Secure ciasteczka sesyjnego",
                f"Ciasteczko {cookie['name']!r} nie ma ustawionego atrybutu Secure.",
            )
        if not cookie["httpOnly"]:
            add_finding(
                findings,
                "medium",
                "Atrybut HttpOnly ciasteczka sesyjnego",
                f"Ciasteczko {cookie['name']!r} jest dostępne dla kodu JavaScript.",
            )
        if cookie["sameSite"] == "None" and not cookie["secure"]:
            add_finding(
                findings,
                "medium",
                "Atrybut SameSite ciasteczka sesyjnego",
                f"Ciasteczko {cookie['name']!r} używa SameSite=None bez atrybutu Secure.",
            )
    return observed


async def inspect_forms(page: Page, page_url: str) -> list[dict[str, Any]]:
    forms = await page.locator("form").evaluate_all(
        """elements => elements.map(form => {
            const fields = Array.from(form.querySelectorAll("input, button, select, textarea"))
                .map(field => ({
                    name: field.name || "",
                    type: (field.type || field.tagName).toLowerCase(),
                    autocomplete: field.autocomplete || ""
                }));
            return {
                action: new URL(form.getAttribute("action") || location.href, document.baseURI).href,
                method: (form.getAttribute("method") || "get").toLowerCase(),
                fields
            };
        })"""
    )

    results: list[dict[str, Any]] = []
    for index, form in enumerate(forms, start=1):
        action = form["action"]
        method = form["method"]
        fields = form["fields"]
        findings: list[dict[str, str]] = []
        if urlparse(page_url).scheme == "https" and urlparse(action).scheme != "https":
            add_finding(
                findings,
                "high",
                "Niebezpieczny transport formularza",
                f"Formularz wysyła dane pod adres bez HTTPS: {action}",
            )
        if not same_origin(page_url, action):
            add_finding(
                findings,
                "medium",
                "Formularz wysyła dane do innej domeny",
                f"Adres docelowy formularza ma inne źródło: {action}",
            )
        has_password = any(field["type"] == "password" for field in fields)
        if method == "get" and has_password:
            add_finding(
                findings,
                "high",
                "Hasło w formularzu GET",
                "Wysyłanie hasła metodą GET może ujawnić je w adresie URL i logach.",
            )
        if method == "post" and not any(
            CSRF_FIELD_NAME.search(field["name"]) for field in fields
        ):
            add_finding(
                findings,
                "info",
                "Ochrona CSRF",
                "Nie znaleziono typowego pola z tokenem CSRF. Ochrona może być wdrożona w inny sposób; to wynik heurystyczny.",
            )
        for finding in findings:
            finding["form"] = str(index)
        results.append(
            {
                "index": index,
                "action": action,
                "method": method.upper(),
                "fields": fields,
                "findings": findings,
            }
        )
    return results


async def check_url(browser: Browser, url: str, timeout: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "url": url,
        "final_url": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "OK",
        "http_status": None,
        "security_headers": {},
        "rate_limiting": {},
        "cookies": [],
        "forms": [],
        "findings": [],
        "error": None,
    }
    context = await browser.new_context()
    try:
        page = await context.new_page()
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        result["final_url"] = page.url
        if response is None:
            result["status"] = "ERROR"
            result["error"] = "Nawigacja zakończyła się bez odpowiedzi głównego dokumentu."
            return result

        result["http_status"] = response.status
        headers = await response.all_headers()
        header_results = inspect_headers(page.url, headers, result["findings"])
        result["security_headers"] = header_results["security_headers"]
        result["rate_limiting"] = header_results["rate_limiting"]
        if urlparse(page.url).scheme != "https":
            add_finding(
                result["findings"],
                "high",
                "Połączenie HTTPS",
                "Strona nie jest udostępniana przez HTTPS.",
            )
        if response.status >= 400:
            add_finding(
                result["findings"],
                "high",
                "Odpowiedź strony",
                f"Strona zwróciła kod HTTP {response.status}.",
            )

        result["forms"] = await inspect_forms(page, page.url)
        for form in result["forms"]:
            result["findings"].extend(form["findings"])
        result["cookies"] = inspect_cookies(
            await context.cookies([page.url]),
            page.url,
            result["findings"],
        )
    except PlaywrightError as error:
        result["status"] = "ERROR"
        result["error"] = str(error)
    finally:
        await context.close()
    return result


def write_reports(report: dict[str, Any]) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"{REPORT_NAME}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    severity_labels = {
        "high": "Wysoki",
        "medium": "Średni",
        "low": "Niski",
        "info": "Informacja",
    }
    fields = (
        "Adres URL",
        "Status skanowania",
        "Kod HTTP",
        "Formularz",
        "Ważność",
        "Kontrola",
        "Szczegóły",
    )
    with (REPORTS_DIR / f"{REPORT_NAME}.csv").open(
        "w", encoding="utf-8", newline=""
    ) as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for page in report["pages"]:
            status = "Błąd" if page["status"] == "ERROR" else (
                "Wymaga uwagi" if page["findings"] else "OK"
            )
            rows: list[dict[str, str]] = []
            if not page["findings"]:
                rows.append(
                    {
                        "Adres URL": page["url"],
                        "Status skanowania": status,
                        "Kod HTTP": str(page["http_status"] or ""),
                        "Formularz": "",
                        "Ważność": "",
                        "Kontrola": "",
                        "Szczegóły": page["error"] or "Brak wykrytych problemów.",
                    }
                )
            for finding in page["findings"]:
                rows.append(
                    {
                        "Adres URL": page["url"],
                        "Status skanowania": status,
                        "Kod HTTP": str(page["http_status"] or ""),
                        "Formularz": finding.get("form", ""),
                        "Ważność": severity_labels[finding["severity"]],
                        "Kontrola": finding["check"],
                        "Szczegóły": finding["detail"],
                    }
                )
            for row in rows:
                writer.writerow(row)

    page_rows = []
    for page in report["pages"]:
        findings_count = len(page["findings"])
        status = "Błąd" if page["status"] == "ERROR" else (
            "Wymaga uwagi" if findings_count else "OK"
        )
        status_class = "error" if page["status"] == "ERROR" else (
            "warning" if findings_count else "ok"
        )
        page_url = str(page["url"])
        forms = "".join(
            "<li>Formularz "
            + str(form["index"])
            + ": "
            + html.escape(form["method"])
            + " → "
            + html.escape(form["action"])
            + " ("
            + str(len(form["fields"]))
            + " pól)</li>"
            for form in page["forms"]
        ) or "<li>Nie znaleziono formularzy.</li>"
        findings = "".join(
            "<li><strong>"
            + html.escape(severity_labels[finding["severity"]])
            + " — "
            + html.escape(finding["check"])
            + ":</strong> "
            + html.escape(finding["detail"])
            + "</li>"
            for finding in page["findings"]
        ) or "<li>Nie wykryto problemów.</li>"
        headers = "".join(
            "<li><code>"
            + html.escape(name)
            + "</code>: "
            + html.escape(value)
            + "</li>"
            for name, value in page["security_headers"].items()
        ) or "<li>Nie znaleziono sprawdzanych nagłówków bezpieczeństwa.</li>"
        cookies = "".join(
            "<li><code>"
            + html.escape(cookie["name"])
            + "</code> — Secure: "
            + ("tak" if cookie["secure"] else "nie")
            + ", HttpOnly: "
            + ("tak" if cookie["http_only"] else "nie")
            + ", SameSite: "
            + html.escape(cookie["same_site"] or "brak")
            + "</li>"
            for cookie in page["cookies"]
        ) or "<li>Nie znaleziono ciasteczek.</li>"
        details = (
            "<p><strong>Ograniczanie liczby żądań:</strong> "
            + html.escape(
                page.get("rate_limiting", {}).get("note", "Nie sprawdzono.")
            )
            + "</p><h4>Formularze</h4><ul>"
            + forms
            + "</ul><h4>Wykryte problemy</h4><ul>"
            + findings
            + "</ul><h4>Nagłówki bezpieczeństwa</h4><ul>"
            + headers
            + "</ul><h4>Ciasteczka</h4><ul>"
            + cookies
            + "</ul>"
        )
        if page["error"]:
            details += "<p><strong>Błąd:</strong> " + html.escape(page["error"]) + "</p>"
        page_rows.append(
            f"""<tr class="{status_class}">
                <td><a href="{html.escape(page_url)}" target="_blank">
                    {html.escape(page_url)}</a></td>
                <td>{status}</td>
                <td>{html.escape(str(page["http_status"] or "—"))}</td>
                <td>{len(page["forms"])}</td>
                <td>{findings_count}</td>
                <td>{html.escape(page["checked_at"])}</td>
                <td><details><summary>Szczegóły</summary>{details}</details></td>
            </tr>"""
        )

    pages = report["pages"]
    total_findings = sum(len(page["findings"]) for page in pages)
    scan_errors = sum(page["status"] == "ERROR" for page in pages)
    document = f"""<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <title>Raport bezpieczeństwa formularzy</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background: #f5f5f5;
            color: #222;
        }}
        h1 {{ margin-bottom: 5px; }}
        .summary {{
            margin-bottom: 20px;
            padding: 15px;
            background: white;
            border-radius: 6px;
            line-height: 1.7;
        }}
        table {{
            border-collapse: collapse;
            width: 100%;
            background: white;
            font-size: 14px;
        }}
        th, td {{
            border: 1px solid #ccc;
            padding: 8px;
            text-align: left;
            vertical-align: top;
        }}
        th {{ background: #333; color: white; }}
        tr.ok {{ background: #effff0; }}
        tr.warning {{ background: #fffbe6; }}
        tr.error {{ background: #fff0f0; }}
        details {{ min-width: 180px; }}
        details ul {{ padding-left: 20px; }}
    </style>
</head>
<body>
    <h1>Raport bezpieczeństwa formularzy</h1>
    <div class="summary">
        <strong>Sprawdzone strony:</strong> {len(pages)}<br>
        <strong>Błędy skanowania:</strong> {scan_errors}<br>
        <strong>Wykryte problemy:</strong> {total_findings}<br>
        <strong>Próg bramki:</strong> {html.escape(str(report["fail_on"] or "brak"))}<br>
        <strong>Wynik bramki:</strong> {html.escape(report["gate_status"])}<br>
        <strong>Data wygenerowania:</strong>
        {html.escape(report["checked_at"])}
    </div>
    <p>Skan jest pasywny i nie wysyła formularzy. Nie można potwierdzić
    egzekwowania limitów żądań bez autoryzowanego testu aktywnego.</p>
    <table>
        <thead>
            <tr>
                <th>Adres URL</th>
                <th>Status</th>
                <th>Kod HTTP</th>
                <th>Formularze</th>
                <th>Problemy</th>
                <th>Data sprawdzenia</th>
                <th>Szczegóły</th>
            </tr>
        </thead>
        <tbody>
            {"".join(page_rows)}
        </tbody>
    </table>
</body>
</html>"""
    (REPORTS_DIR / f"{REPORT_NAME}.html").write_text(document, encoding="utf-8")


async def run(args: argparse.Namespace) -> int:
    urls = load_urls(args.input)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        try:
            pages = []
            for url in urls:
                print(f"Sprawdzanie: {url}")
                page_result = await check_url(browser, url, args.timeout)
                pages.append(page_result)
                if page_result["status"] == "ERROR":
                    print(f"  Błąd: {page_result['error']}", file=sys.stderr)
                else:
                    print(
                        f"  HTTP {page_result['http_status']}; "
                        f"formularze: {len(page_result['forms'])}; "
                        f"problemy: {len(page_result['findings'])}"
                    )
        finally:
            await browser.close()

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "fail_on": args.fail_on,
        "gate_status": "NOT_CONFIGURED",
        "pages": pages,
    }
    findings = [
        finding
        for page in pages
        for finding in page["findings"]
    ]
    gate_failed = meets_fail_threshold(findings, args.fail_on)
    if args.fail_on is not None:
        report["gate_status"] = "FAIL" if gate_failed else "PASS"

    write_reports(report)
    print(f"Raporty zapisano w: {REPORTS_DIR}")
    scan_failed = any(page["status"] == "ERROR" for page in pages)
    if scan_failed:
        return 1
    if gate_failed:
        return 2
    return 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pasywnie sprawdza bezpieczeństwo formularzy na wskazanych stronach."
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help=f"Plik z adresami URL HTTP(S) (domyślnie: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30000,
        help="Limit czasu nawigacji w milisekundach (domyślnie: 30000)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Uruchom Chromium w widocznym oknie.",
    )
    parser.add_argument(
        "--fail-on",
        choices=tuple(SEVERITY_RANK),
        default=None,
        help=(
            "Zakończ kodem 2, jeśli wystąpi problem o tej ważności lub wyższej. "
            "Domyślnie tylko raportuje problemy."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_arguments())))
    except (FileNotFoundError, ValueError) as error:
        print(f"Błąd: {error}", file=sys.stderr)
        raise SystemExit(2) from error
