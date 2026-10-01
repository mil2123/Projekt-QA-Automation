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
    BrowserContext,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


REPORTS_DIR = Path("reports")
SCREENSHOTS_DIR = Path("screenshots")


def safe_filename(value: str, max_length: int = 150) -> str:
    """
    Zamienia ścieżkę URL na bezpieczną nazwę pliku.
    """
    value = value.strip()

    if not value:
        value = "root"

    value = re.sub(r"^https?://", "", value)
    value = value.replace("/", "_")
    value = value.replace("\\", "_")
    value = value.replace("?", "_")
    value = value.replace("&", "_")
    value = value.replace("=", "_")
    value = value.replace(":", "_")
    value = value.replace("#", "_")

    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value)
    value = value.strip("._-")

    if not value:
        value = "page"

    return value[:max_length]


def load_paths(filename: str) -> list[str]:
    """
    Wczytuje ścieżki z pliku all_paths.txt.
    """
    path = Path(filename)

    if not path.exists():
        raise FileNotFoundError(f"Nie znaleziono pliku: {filename}")

    paths: list[str] = []

    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line:
                continue

            if line.startswith("#"):
                continue

            paths.append(line)

    return paths


def build_url(base_url: str, path_or_url: str) -> str:
    """
    Tworzy pełny adres URL.
    Jeśli podana wartość jest już pełnym adresem, zostanie użyta bez zmian.
    """
    parsed = urlparse(path_or_url)

    if parsed.scheme in ("http", "https"):
        return path_or_url

    return urljoin(base_url.rstrip("/") + "/", path_or_url.lstrip("/"))


def print_terminal_error(
    browser_name: str,
    url: str,
    error_type: str,
    message: str,
) -> None:
    """
    Wyświetla błąd w terminalu.
    """
    print(
        f"[{browser_name.upper()}] [{error_type}] {url}\n"
        f"    {message}",
        file=sys.stderr,
    )


async def check_page(
    browser: Browser,
    browser_name: str,
    url: str,
    original_path: str,
    timeout: int,
    headed: bool,
) -> dict[str, Any]:
    """
    Sprawdza pojedynczy adres URL w konkretnej przeglądarce.
    """

    browser_dir = SCREENSHOTS_DIR / browser_name
    browser_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "browser": browser_name,
        "path": original_path,
        "url": url,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "OK",
        "http_status": None,
        "title": None,
        "load_time_ms": None,
        "screenshot": None,
        "console_messages": [],
        "javascript_errors": [],
        "request_failures": [],
        "http_errors": [],
        "page_errors": [],
        "navigation_error": None,
    }

    context: BrowserContext | None = None
    page: Page | None = None

    start_time = asyncio.get_running_loop().time()

    try:
        context = await browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1440, "height": 1000},
        )

        page = await context.new_page()

        async def on_console(message: Any) -> None:
            message_type = message.type
            message_text = message.text

            # Zapisujemy wszystkie komunikaty do raportu.
            result["console_messages"].append(
                {
                    "type": message_type,
                    "text": message_text,
                    "location": message.location,
                }
            )

            # Do terminala wypisujemy głównie błędy i ostrzeżenia.
            if message_type in ("error", "warning"):
                print_terminal_error(
                    browser_name,
                    url,
                    f"CONSOLE {message_type.upper()}",
                    message_text,
                )

        def on_page_error(exception: Any) -> None:
            message = str(exception)

            result["javascript_errors"].append(message)
            result["status"] = "ERROR"

            print_terminal_error(
                browser_name,
                url,
                "JAVASCRIPT",
                message,
            )

        def on_request_failed(request: Any) -> None:
            failure = getattr(request, "failure", None)

            if callable(failure):
                failure = failure()

            failure_text = str(failure) if failure else "Nieznany błąd żądania"
            is_main_document = (
                request.is_navigation_request() and request.frame == page.main_frame
            )

            item = {
                "url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "failure": failure_text,
                "main_document": is_main_document,
            }

            result["request_failures"].append(item)
            if is_main_document:
                result["status"] = "ERROR"

            print_terminal_error(
                browser_name,
                url,
                "REQUEST FAILED"
                if is_main_document
                else "RESOURCE REQUEST FAILED",
                f"{request.method} {request.url} - {failure_text}",
            )

        def on_response(response: Response) -> None:
            if response.status >= 400:
                is_main_document = (
                    response.request.is_navigation_request()
                    and response.request.frame == page.main_frame
                )
                item = {
                    "url": response.url,
                    "status": response.status,
                    "status_text": response.status_text,
                    "request_method": response.request.method,
                    "resource_type": response.request.resource_type,
                    "main_document": is_main_document,
                }

                result["http_errors"].append(item)

                print_terminal_error(
                    browser_name,
                    url,
                    f"HTTP {response.status}"
                    if is_main_document
                    else f"HTTP RESOURCE {response.status}",
                    f"{response.request.method} {response.url}",
                )

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)
        page.on("response", on_response)

        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout,
            )

            if response is not None:
                result["http_status"] = response.status

                if response.status >= 400:
                    result["status"] = "ERROR"

        except Exception as exception:
            error_message = str(exception)

            result["navigation_error"] = error_message
            result["status"] = "ERROR"

            print_terminal_error(
                browser_name,
                url,
                "NAVIGATION",
                error_message,
            )

        # Próba zaczekania na zakończenie aktywności sieciowej.
        # Nie traktujemy timeoutu networkidle jako krytycznego błędu,
        # ponieważ niektóre strony mają stałe połączenia, np. WebSocket.
        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=min(timeout, 15000),
            )
        except PlaywrightTimeoutError:
            pass

        try:
            result["title"] = await page.title()
        except Exception:
            result["title"] = None

        if result["status"] != "OK":
            screenshot_name = safe_filename(original_path) + ".png"
            screenshot_path = browser_dir / screenshot_name
            result["screenshot"] = str(screenshot_path)

            try:
                await page.screenshot(
                    path=str(screenshot_path),
                    full_page=True,
                    timeout=timeout,
                )
            except Exception as exception:
                screenshot_error = str(exception)

                result["status"] = "ERROR"
                result["screenshot_error"] = screenshot_error

                print_terminal_error(
                    browser_name,
                    url,
                    "SCREENSHOT",
                    screenshot_error,
                )

    except Exception as exception:
        error_message = str(exception)

        result["navigation_error"] = error_message
        result["status"] = "ERROR"

        print_terminal_error(
            browser_name,
            url,
            "GENERAL",
            error_message,
        )

    finally:
        elapsed = asyncio.get_running_loop().time() - start_time
        result["load_time_ms"] = round(elapsed * 1000)

        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

        if context is not None:
            try:
                await context.close()
            except Exception:
                pass

    if result["status"] == "OK":
        print(f"[{browser_name.upper()}] OK {url}")
    else:
        print(f"[{browser_name.upper()}] BŁĘDY {url}")

    return result


def write_json_report(results: list[dict[str, Any]]) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    report_path = REPORTS_DIR / "report.json"

    with report_path.open("w", encoding="utf-8") as file:
        json.dump(results, file, ensure_ascii=False, indent=2)

    print(f"Zapisano raport JSON: {report_path}")


def write_csv_report(results: list[dict[str, Any]]) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    report_path = REPORTS_DIR / "report.csv"

    rows: list[dict[str, Any]] = []

    for result in results:
        rows.append(
            {
                "browser": result.get("browser"),
                "path": result.get("path"),
                "url": result.get("url"),
                "status": result.get("status"),
                "http_status": result.get("http_status"),
                "title": result.get("title"),
                "load_time_ms": result.get("load_time_ms"),
                "javascript_errors": len(
                    result.get("javascript_errors", [])
                ),
                "request_failures": len(
                    result.get("request_failures", [])
                ),
                "http_errors": len(result.get("http_errors", [])),
                "console_messages": len(
                    result.get("console_messages", [])
                ),
                "navigation_error": result.get("navigation_error"),
                "screenshot": result.get("screenshot"),
            }
        )

    fieldnames = list(rows[0].keys()) if rows else []

    with report_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Zapisano raport CSV: {report_path}")


def write_html_report(results: list[dict[str, Any]]) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    report_path = REPORTS_DIR / "report.html"

    total = len(results)
    errors = sum(1 for result in results if result["status"] != "OK")
    successful = total - errors

    rows: list[str] = []

    # Keep failing tests visible first while preserving their original order.
    ordered_results = sorted(
        results,
        key=lambda result: result.get("status") == "OK",
    )

    for result in ordered_results:
        status = result.get("status", "")
        status_class = "ok" if status == "OK" else "error"

        console_messages = result.get("console_messages", [])
        javascript_errors = result.get("javascript_errors", [])
        request_failures = result.get("request_failures", [])
        http_errors = result.get("http_errors", [])

        details = []

        if javascript_errors:
            details.append(
                "<h4>Błędy JavaScript</h4><ul>"
                + "".join(
                    f"<li>{html.escape(str(error))}</li>"
                    for error in javascript_errors
                )
                + "</ul>"
            )

        if request_failures:
            details.append(
                "<h4>Nieudane żądania</h4><ul>"
                + "".join(
                    "<li>"
                    + html.escape(
                        f"{item.get('method')} "
                        f"{item.get('url')} - "
                        f"{item.get('failure')} "
                        f"({'dokument główny' if item.get('main_document') else 'zasób'})"
                    )
                    + "</li>"
                    for item in request_failures
                )
                + "</ul>"
            )

        if http_errors:
            details.append(
                "<h4>Błędy HTTP</h4><ul>"
                + "".join(
                    "<li>"
                    + html.escape(
                        f"{item.get('status')} "
                        f"{item.get('url')} "
                        f"({'dokument główny' if item.get('main_document') else 'zasób'})"
                    )
                    + "</li>"
                    for item in http_errors
                )
                + "</ul>"
            )

        if console_messages:
            console_errors = [
                item
                for item in console_messages
                if item.get("type") in ("error", "warning")
            ]

            if console_errors:
                details.append(
                    "<h4>Błędy i ostrzeżenia konsoli</h4><ul>"
                    + "".join(
                        "<li>"
                        + html.escape(
                            f"{item.get('type')}: "
                            f"{item.get('text')}"
                        )
                        + "</li>"
                        for item in console_errors
                    )
                    + "</ul>"
                )

        if result.get("navigation_error"):
            details.append(
                "<h4>Błąd nawigacji</h4>"
                f"<p>{html.escape(str(result['navigation_error']))}</p>"
            )

        details_html = "".join(details) or "<p>Brak szczegółowych błędów.</p>"

        screenshot_path = result.get("screenshot")
        screenshot_html = ""

        if screenshot_path:
            screenshot_url = "../" + str(screenshot_path).replace("\\", "/")
            screenshot_html = (
                f"""
                    <a href="{html.escape(screenshot_url)}"
                       target="_blank">
                        zrzut ekranu
                    </a>
                """
            )

        rows.append(
            f"""
            <tr class="{status_class}">
                <td>{html.escape(str(result.get("browser")))}</td>
                <td>{html.escape(str(result.get("path")))}</td>
                <td>
                    <a href="{html.escape(str(result.get("url")))}"
                       target="_blank">
                        {html.escape(str(result.get("url")))}
                    </a>
                </td>
                <td>{html.escape(str(result.get("http_status")))}</td>
                <td>{html.escape(str(result.get("title")))}</td>
                <td>{html.escape(str(result.get("load_time_ms")))} ms</td>
                <td>{html.escape(str(result.get("status")))}</td>
                <td>
                    {screenshot_html}
                    <details>
                        <summary>Szczegóły</summary>
                        {details_html}
                    </details>
                </td>
            </tr>
            """
        )

    document = f"""<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <title>Raport testowania stron</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background: #f5f5f5;
            color: #222;
        }}

        h1 {{
            margin-bottom: 5px;
        }}

        .summary {{
            margin-bottom: 20px;
            padding: 15px;
            background: white;
            border-radius: 6px;
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

        th {{
            background: #333;
            color: white;
        }}

        tr.ok {{
            background: #effff0;
        }}

        tr.error {{
            background: #fff0f0;
        }}

        details {{
            margin-top: 8px;
        }}

        code {{
            white-space: pre-wrap;
        }}
    </style>
</head>
<body>
    <h1>Raport testowania stron</h1>

    <div class="summary">
        <strong>Łącznie testów:</strong> {total}<br>
        <strong>Poprawne:</strong> {successful}<br>
        <strong>Z błędami:</strong> {errors}<br>
        <strong>Data wygenerowania:</strong>
        {html.escape(datetime.now().isoformat())}
    </div>

    <table>
        <thead>
            <tr>
                <th>Przeglądarka</th>
                <th>Ścieżka</th>
                <th>URL</th>
                <th>HTTP</th>
                <th>Tytuł</th>
                <th>Czas</th>
                <th>Status</th>
                <th>Szczegóły</th>
            </tr>
        </thead>
        <tbody>
            {"".join(rows)}
        </tbody>
    </table>
</body>
</html>
"""

    with report_path.open("w", encoding="utf-8") as file:
        file.write(document)

    print(f"Zapisano raport HTML: {report_path}")


async def run(args: argparse.Namespace) -> int:
    try:
        paths = load_paths(args.input)
    except FileNotFoundError as exception:
        print(f"BŁĄD: {exception}", file=sys.stderr)
        return 1

    if not paths:
        print("BŁĄD: Plik nie zawiera żadnych ścieżek.", file=sys.stderr)
        return 1

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []

    async with async_playwright() as playwright:
        browsers: list[tuple[str, Browser]] = []

        try:
            print("Uruchamianie Google Chrome...")

            chrome = await playwright.chromium.launch(
                channel="chrome",
                headless=not args.headed,
            )

            browsers.append(("chrome", chrome))

        except Exception as exception:
            print(
                "Nie udało się uruchomić Google Chrome.\n"
                "Upewnij się, że Chrome jest zainstalowany.\n"
                f"Szczegóły: {exception}",
                file=sys.stderr,
            )

        try:
            print("Uruchamianie Firefox...")

            firefox = await playwright.firefox.launch(
                headless=not args.headed,
            )

            browsers.append(("firefox", firefox))

        except Exception as exception:
            print(
                "Nie udało się uruchomić Firefoxa.\n"
                "Uruchom: playwright install firefox\n"
                f"Szczegóły: {exception}",
                file=sys.stderr,
            )

        if not browsers:
            print(
                "Nie udało się uruchomić żadnej przeglądarki.",
                file=sys.stderr,
            )
            return 1

        try:
            for browser_name, browser in browsers:
                print(f"\n===== TESTY: {browser_name.upper()} =====")

                for original_path in paths:
                    url = build_url(args.base_url, original_path)

                    result = await check_page(
                        browser=browser,
                        browser_name=browser_name,
                        url=url,
                        original_path=original_path,
                        timeout=args.timeout,
                        headed=args.headed,
                    )

                    results.append(result)

        finally:
            for _, browser in browsers:
                try:
                    await browser.close()
                except Exception:
                    pass

    write_json_report(results)
    write_csv_report(results)
    write_html_report(results)

    errors = sum(
        1 for result in results if result["status"] != "OK"
    )

    print("\n===== PODSUMOWANIE =====")
    print(f"Liczba testów: {len(results)}")
    print(f"Poprawne: {len(results) - errors}")
    print(f"Z błędami: {errors}")

    # Kod 2 oznacza, że program zakończył testy,
    # ale wykrył błędy na stronach.
    return 2 if errors else 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sprawdza ścieżki z pliku w Chrome i Firefoxie."
        )
    )

    parser.add_argument(
        "--base-url",
        default="https://dev.hire.engineer",
        help=(
            "Adres bazowy, np. https://example.com. "
            "Domyślnie: https://dev.hire.engineer"
        ),
    )

    parser.add_argument(
        "--input",
        default="all_paths.txt",
        help=(
            "Plik z listą ścieżek. Domyślnie: all_paths.txt"
        ),
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=30000,
        help=(
            "Timeout pojedynczej operacji w milisekundach. "
            "Domyślnie: 30000"
        ),
    )

    parser.add_argument(
        "--headed",
        action="store_true",
        help=(
            "Pokazuje okna przeglądarek podczas testowania."
        ),
    )

    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()

    try:
        exit_code = asyncio.run(run(arguments))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nPrzerwano przez użytkownika.")
        sys.exit(130)