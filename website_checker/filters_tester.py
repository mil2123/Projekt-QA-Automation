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
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import Browser, Page, async_playwright


BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
DEFAULT_URL = "https://dev.hire.engineer/engineers/"

FILTERS = (
    ("position", "Position", "All positions"),
    ("seniority", "Seniority", "All seniority levels"),
    ("availability", "Availability", "All availability"),
)


def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    return value.strip("._-")[:150] or "filter"


def load_target(filename: str) -> str:
    path = Path(filename)
    if not path.is_absolute():
        path = BASE_DIR / path

    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            return urljoin(DEFAULT_URL, value)

    raise ValueError(f"Input file contains no URL or path: {path}")


async def wait_for_page(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded")


async def card_records(page: Page) -> list[dict[str, str]]:
    return await page.locator('a[data-ga-cta="engineer_card"]').evaluate_all(
        """
        cards => cards.map(card => ({
            url: card.href,
            text: card.innerText,
            position: card.querySelector("p")?.innerText.trim() || ""
        }))
        """
    )


def query_value(url: str, name: str) -> str:
    return parse_qs(urlparse(url).query).get(name, [""])[0]


def expected_query_value(filter_name: str, value: str) -> str:
    if filter_name == "seniority":
        return {"Junior": "junior", "Regular": "regular", "Senior": "senior",
                "Tech Lead": "lead"}[value]
    if filter_name == "availability":
        return {"Available Now": "now", "Available Soon": "soon"}[value]
    return value


def matching_cards(
    cards: list[dict[str, str]],
    filter_name: str,
    value: str,
) -> list[dict[str, str]]:
    if filter_name == "position":
        return [card for card in cards if card["position"] == value]

    if filter_name == "seniority":
        return [
            card
            for card in cards
            if re.search(rf"(?m)^\s*{re.escape(value)}\s*$", card["text"])
        ]

    availability = {"Available Now": "Now", "Available Soon": "Soon"}[value]
    return [
        card
        for card in cards
        if re.search(rf"(?m)^\s*{re.escape(availability)}\s*$", card["text"])
    ]


def normalize_tag(value: str) -> str:
    return " ".join(value.split()).casefold()


def tag_matches(tags: set[str], expected_tag: str) -> bool:
    return normalize_tag(expected_tag) in tags


async def profile_technology_tags(page: Page) -> set[str]:
    tags = await page.locator('a[href*="/technologies/"]').evaluate_all(
        """
        links => links
            .filter(link => new URL(link.href).pathname !== "/technologies/")
            .map(link => link.innerText.trim())
            .filter(Boolean)
        """
    )
    normalized_tags = {normalize_tag(tag) for tag in tags}
    if not normalized_tags:
        raise AssertionError(
            f"Nie znaleziono tagów technologii na profilu {page.url}"
        )
    return normalized_tags


async def take_screenshot(page: Page, case_name: str) -> str | None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOTS_DIR / f"{safe_filename(case_name)}.png"
    try:
        await page.screenshot(path=str(path), full_page=True, timeout=30000)
        return str(path)
    except Exception as error:
        print(f"Nie udało się zapisać zrzutu ekranu dla {case_name}: {error}", file=sys.stderr)
        return None


async def run_case(
    browser: Browser,
    target_url: str,
    case_name: str,
    action: Any,
    load_target: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "case": case_name,
        "url": target_url,
        "status": "OK",
        "message": "",
        "result_count": None,
        "screenshot": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    page = await browser.new_page(
        viewport={"width": 1440, "height": 1000},
        ignore_https_errors=True,
    )

    try:
        if load_target:
            await wait_for_page(page, target_url)
        await action(page, result)
    except Exception as error:
        result["status"] = "ERROR"
        result["message"] = str(error)
        result["screenshot"] = await take_screenshot(page, case_name)
    finally:
        await page.close()

    return result


async def check_select_filter(
    page: Page,
    result: dict[str, Any],
    target_url: str,
    filter_index: int,
    query_name: str,
    value: str,
    baseline_urls: set[str],
) -> None:
    select = page.locator("select").nth(filter_index)
    options = await select.locator("option").all_text_contents()
    if value not in [option.strip() for option in options]:
        raise AssertionError(f"Brakuje opcji filtra: {value}")

    await select.select_option(label=value)
    expected_query = expected_query_value(query_name, value)
    await page.wait_for_function(
        "args => new URL(location.href).searchParams.get(args.name) === args.value",
        arg={"name": query_name, "value": expected_query},
        timeout=30000,
    )
    # The change handler navigates the page; wait for the new document before
    # evaluating card locators, otherwise the old execution context can vanish.
    await page.wait_for_load_state("load", timeout=30000)
    cards = await card_records(page)
    result["result_count"] = len(cards)

    actual_query = query_value(page.url, query_name)
    if actual_query != expected_query:
        raise AssertionError(
            f"Parametr URL {query_name!r} ma wartość {actual_query!r}, "
            f"oczekiwano {expected_query!r}"
        )

    unexpected_urls = {card["url"] for card in cards} - baseline_urls
    if unexpected_urls:
        raise AssertionError(
            f"Filtr zwrócił karty nieobecne na liście bez filtrowania: "
            f"{sorted(unexpected_urls)}"
        )

    invalid_cards = [
        card for card in cards if card not in matching_cards(cards, query_name, value)
    ]
    if invalid_cards:
        raise AssertionError(
            f"Liczba kart niepasujących do wartości {value!r}: {len(invalid_cards)}"
        )


async def check_tag_filter(
    browser: Browser,
    page: Page,
    result: dict[str, Any],
    tag_url: str,
    tag: str,
    baseline_urls: set[str],
    tag_cache: dict[str, set[str]],
    click_tag: bool,
) -> None:
    if click_tag:
        tag_links = page.locator('a[href*="tag="]:visible')
        for index in range(await tag_links.count()):
            link = tag_links.nth(index)
            href = await link.get_attribute("href")
            label = (await link.inner_text()).strip()
            if href and label == tag and urljoin(page.url, href) == tag_url:
                await link.click(timeout=5000)
                break
        else:
            await wait_for_page(page, tag_url)
    else:
        await wait_for_page(page, tag_url)

    await page.wait_for_function(
        "tag => new URL(location.href).searchParams.get('tag') === tag",
        arg=tag,
        timeout=10000,
    )
    await page.wait_for_load_state("domcontentloaded", timeout=10000)
    cards = await card_records(page)
    result["result_count"] = len(cards)

    actual_query = query_value(page.url, "tag")
    if actual_query != tag:
        raise AssertionError(
            f"Parametr URL 'tag' ma wartość {actual_query!r}, oczekiwano {tag!r}"
        )

    unexpected_urls = {card["url"] for card in cards} - baseline_urls
    if unexpected_urls:
        raise AssertionError(
            f"Filtr tagu zwrócił karty nieobecne na liście bez filtrowania: "
            f"{sorted(unexpected_urls)}"
        )

    if not cards:
        raise AssertionError(f"Filtr tagu {tag!r} nie zwrócił żadnych kart")

    profile_page = await browser.new_page(
        viewport={"width": 1440, "height": 1000},
        ignore_https_errors=True,
    )
    invalid_urls = []
    try:
        for card in cards:
            profile_url = card["url"]
            if profile_url not in tag_cache:
                await wait_for_page(profile_page, profile_url)
                tag_cache[profile_url] = await profile_technology_tags(profile_page)
            if not tag_matches(tag_cache[profile_url], tag):
                invalid_urls.append(profile_url)
    finally:
        await profile_page.close()

    if invalid_urls:
        raise AssertionError(
            f"Filtr tagu {tag!r} wyświetlił profile bez tego tagu: "
            f"{sorted(invalid_urls)}"
        )


async def collect_cases(
    browser: Browser,
    target_url: str,
) -> list[dict[str, Any]]:
    baseline_page = await browser.new_page(
        viewport={"width": 1440, "height": 1000},
        ignore_https_errors=True,
    )
    try:
        await wait_for_page(baseline_page, target_url)
        await baseline_page.locator(
            'a[data-ga-cta="engineer_card"]'
        ).first.wait_for(state="visible", timeout=30000)
        baseline_cards = await card_records(baseline_page)
        if not baseline_cards:
            raise AssertionError("Strona bez filtrów nie zawiera kart specjalistów")
        baseline_urls = {card["url"] for card in baseline_cards}

        controls = await baseline_page.locator("select").count()
        if controls != len(FILTERS):
            raise AssertionError(
                f"Oczekiwano {len(FILTERS)} list rozwijanych, znaleziono: {controls}"
            )

        tag_links = await baseline_page.locator('a[href*="tag="]').evaluate_all(
            """links => links.map(link => ({
                tag: link.innerText.trim(),
                href: link.href,
                visible: Boolean(link.getClientRects().length) &&
                    getComputedStyle(link).visibility !== "hidden" &&
                    getComputedStyle(link).display !== "none"
            }))"""
        )
    finally:
        await baseline_page.close()

    results: list[dict[str, Any]] = []
    tag_cache: dict[str, set[str]] = {}
    write_reports(results)
    for index, (query_name, label, all_label) in enumerate(FILTERS):
        page = await browser.new_page(
            viewport={"width": 1440, "height": 1000},
            ignore_https_errors=True,
        )
        try:
            await wait_for_page(page, target_url)
            values = [
                option.strip()
                for option in await page.locator("select").nth(index)
                .locator("option")
                .all_text_contents()
                if option.strip() != all_label
            ]
        finally:
            await page.close()

        for value in values:
            case_name = f"{label.lower()}-{value}"
            print(f"Testowanie: {case_name}", flush=True)
            result = await run_case(
                browser,
                target_url,
                case_name,
                lambda page, result, i=index, q=query_name, v=value: check_select_filter(
                    page, result, target_url, i, q, v, baseline_urls
                ),
            )
            results.append(result)
            write_reports(results)

    for tag_link in tag_links:
        tag = tag_link["tag"]
        print(f"Testowanie tagu: {tag}", flush=True)
        result = await run_case(
            browser,
            target_url,
            f"tag-{tag}",
            lambda page, result, link=tag_link: check_tag_filter(
                browser, page, result, link["href"], link["tag"], baseline_urls,
                tag_cache, link["visible"]
            ),
            load_target=tag_link["visible"],
        )
        results.append(result)
        write_reports(results)

    return results


def write_reports(results: list[dict[str, Any]]) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with (REPORTS_DIR / "filters_report.json").open("w", encoding="utf-8") as file:
        json.dump(results, file, ensure_ascii=False, indent=2)

    fields = ["case", "url", "status", "result_count", "message", "screenshot", "checked_at"]
    with (REPORTS_DIR / "filters_report.csv").open(
        "w", encoding="utf-8", newline=""
    ) as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: result.get(field) for field in fields} for result in results)

    total = len(results)
    errors = sum(1 for result in results if result.get("status") != "OK")
    successful = total - errors
    rows = []

    # Keep failing checks visible first while preserving their original order.
    ordered_results = sorted(
        results,
        key=lambda result: result.get("status") == "OK",
    )

    for result in ordered_results:
        status = result.get("status", "")
        status_class = "ok" if status == "OK" else "error"
        screenshot = ""
        if result.get("screenshot"):
            screenshot_url = "../" + str(result["screenshot"]).replace("\\", "/")
            screenshot = (
                f'<a href="{html.escape(screenshot_url)}" target="_blank">'
                "zrzut ekranu</a>"
            )

        message = result.get("message")
        details_html = (
            f"<p>{html.escape(str(message))}</p>"
            if message
            else "<p>Brak dodatkowych informacji.</p>"
        )

        rows.append(
            f"""
            <tr class="{status_class}">
                <td>{html.escape(str(result.get("case", "")))}</td>
                <td>
                    <a href="{html.escape(str(result.get("url", "")))}"
                       target="_blank">
                        {html.escape(str(result.get("url", "")))}
                    </a>
                </td>
                <td>{html.escape(str(result.get("status", "")))}</td>
                <td>{html.escape(str(result.get("result_count", "")))}</td>
                <td>{html.escape(str(result.get("checked_at", "")))}</td>
                <td>
                    {screenshot}
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
    <title>Raport filtrów specjalistów</title>
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
    </style>
</head>
<body>
    <h1>Raport filtrów specjalistów</h1>

    <div class="summary">
        <strong>Łącznie testów:</strong> {total}<br>
        <strong>Poprawne:</strong> {successful}<br>
        <strong>Z błędami:</strong> {errors}<br>
        <strong>Data wygenerowania:</strong>
        {html.escape(datetime.now().isoformat())}
    </div>

    <table>
        <tr>
            <th>Przypadek</th>
            <th>URL</th>
            <th>Status</th>
            <th>Wyniki</th>
            <th>Data sprawdzenia</th>
            <th>Szczegóły</th>
        </tr>
        {"".join(rows)}
    </table>
</body>
</html>
"""
    (REPORTS_DIR / "filters_report.html").write_text(document, encoding="utf-8")


async def run(args: argparse.Namespace) -> int:
    try:
        target_url = load_target(args.input)
    except (FileNotFoundError, ValueError) as error:
        print(f"BŁĄD: {error}", file=sys.stderr)
        return 1

    print(f"Rozpoczynanie testów filtrów dla {target_url}", flush=True)
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=not args.headed,
                timeout=30000,
            )
        except Exception as error:
            message = f"Nie udało się uruchomić Chromium w Playwright: {error}"
            print(f"BŁĄD: {message}", file=sys.stderr)
            write_reports([{
                "case": "inicjalizacja",
                "url": target_url,
                "status": "ERROR",
                "message": message,
                "result_count": None,
                "screenshot": None,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }])
            return 1

        try:
            results = await collect_cases(browser, target_url)
        except Exception as error:
            results = [{
                "case": "inicjalizacja",
                "url": target_url,
                "status": "ERROR",
                "message": str(error),
                "result_count": None,
                "screenshot": None,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }]
        finally:
            await browser.close()

    write_reports(results)
    errors = sum(result["status"] != "OK" for result in results)
    print(
        f"Sprawdzono filtrów: {len(results)}; "
        f"poprawne: {len(results) - errors}; błędne: {errors}"
    )
    print(f"Raporty zapisano w: {REPORTS_DIR}")
    return 2 if errors else 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test filters on the engineers page")
    parser.add_argument(
        "--input",
        default="filters_tester_paths.txt",
        help="File containing the page URL or path",
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_arguments())))