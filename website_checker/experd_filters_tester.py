"""Sprawdza, czy filtry na stronie case studies Experd zmieniają wyniki.

Testowana strona jest wczytywana z pierwszego niepustego wiersza pliku
``experd_filters_tester_paths.txt``. Skrypt wykrywa kontrolki na stronie, zamiast polegać na
wewnętrznych nazwach klas CSS Experd.
"""

import argparse
import asyncio
import csv
import hashlib
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
DEFAULT_INPUT = "experd_filters_tester_paths.txt"
REPORT_NAME = "experd_filters_report"


def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    return value.strip("._-")[:150] or "filter"


def load_target(filename: str) -> str:
    path = Path(filename)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.exists():
        raise FileNotFoundError(f"Nie znaleziono pliku wejściowego: {path}")

    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            target_url = urljoin("https://example.com/", value)
            hostname = urlparse(target_url).hostname
            if hostname in {"example.com", "your-experd-page.example"}:
                raise ValueError(
                    f"Zastąp przykładowy adres URL w pliku {path} "
                    "prawidłowym adresem strony case studies Experd"
                )
            return target_url
    raise ValueError(f"Plik wejściowy nie zawiera adresu URL: {path}")


async def wait_for_page(page: Page, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)


async def page_signature(page: Page) -> str:
    """Zwraca stabilny, niewielki skrót widocznej, przefiltrowanej zawartości."""
    content = await page.locator("body").inner_text(timeout=10000)
    links = await page.locator("a:visible").evaluate_all(
        """links => links.map(link => `${link.href}|${link.innerText.trim()}`)
        .filter(value => value !== "|").join("\\n")"""
    )
    value = f"{content}\n---links---\n{links}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def visible_result_count(page: Page) -> int:
    """Zlicza prawdopodobne karty wyników bez zależności od konkretnej strony."""
    candidates = page.locator(
        '[data-testid*="card" i], [class*="card" i], '
        '[data-testid*="result" i], [class*="result" i], article'
    )
    return await candidates.filter(visible=True).count()


def normalize_tag(value: str) -> str:
    return " ".join(value.split()).casefold()


def tag_matches(tags: set[str], expected_tag: str) -> bool:
    return normalize_tag(expected_tag) in tags


async def case_study_cards(page: Page) -> list[dict[str, str]]:
    return await page.locator("a[data-case-study-card]").evaluate_all(
        "cards => cards.map(card => ({url: card.href, text: card.innerText.trim()}))"
    )


async def case_study_tags(page: Page) -> set[str]:
    labels = page.locator('span:text-is("Tags:")')
    if await labels.count() != 1:
        raise AssertionError(
            f"Nie znaleziono jednoznacznej sekcji tagów na stronie {page.url}"
        )
    tag_links = await labels.locator("xpath=..").locator("a").all_text_contents()
    tags = {normalize_tag(tag) for tag in tag_links if tag.strip()}
    if not tags:
        raise AssertionError(f"Strona case study nie zawiera tagów: {page.url}")
    return tags


async def discover_filters(page: Page) -> list[dict[str, Any]]:
    """Wykrywa listy wyboru i dostępne opcje na stronie bazowej."""
    filters: list[dict[str, Any]] = []
    selects = page.locator("select:visible")
    for index in range(await selects.count()):
        select = selects.nth(index)
        options = await select.locator("option").evaluate_all(
            """options => options.map(option => ({
                label: option.textContent.trim(),
                value: option.value,
                disabled: option.disabled
            }))"""
        )
        usable = [
            option for option in options
            if not option["disabled"]
            and option["value"]
            and option["label"]
            and option["label"].lower() not in {"all", "all categories", "all types"}
        ]
        if not usable:
            continue
        name = await select.get_attribute("aria-label")
        name = name or await select.get_attribute("name")
        name = name or f"filter-{index + 1}"
        filters.append({"index": index, "name": name, "options": usable})

    tag_links = await page.locator('a[href*="tag="]').evaluate_all(
        """links => links.map(link => ({
            label: link.textContent.trim(),
            href: link.href,
            visible: Boolean(link.getClientRects().length) &&
                getComputedStyle(link).visibility !== "hidden" &&
                getComputedStyle(link).display !== "none"
        })).filter(item => item.label && item.href)"""
    )
    if tag_links:
        filters.append({
            "name": "tag",
            "kind": "tag",
            "options": tag_links,
        })
    return filters


async def click_tag_filter(
    page: Page, tag_url: str, tag: str, click_tag: bool
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
        "tag => new URL(location.href).searchParams.getAll('tag')"
        ".some(value => value === tag)",
        arg=tag,
        timeout=10000,
    )
    await page.wait_for_load_state("domcontentloaded", timeout=10000)


async def take_screenshot(page: Page, case_name: str) -> str | None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOTS_DIR / f"{safe_filename(case_name)}.png"
    try:
        await page.screenshot(path=str(path), full_page=True, timeout=30000)
    except Exception as error:
        print(f"Nie udało się zapisać zrzutu ekranu dla {case_name}: {error}", file=sys.stderr)
        return None
    return str(path)


async def check_filter(
    browser: Browser,
    page: Page,
    result: dict[str, Any],
    filter_info: dict[str, Any],
    option: dict[str, Any],
    baseline_signature: str,
    tag_cache: dict[str, set[str]],
    click_tag: bool,
) -> None:
    if filter_info.get("kind") == "tag":
        await click_tag_filter(
            page, option["href"], option["label"], click_tag
        )
        actual_tags = {
            normalize_tag(tag)
            for tag in parse_qs(urlparse(page.url).query).get("tag", [])
        }
        if not tag_matches(actual_tags, option["label"]):
            raise AssertionError(
                f"Adres po wybraniu tagu nie zawiera wartości "
                f"{option['label']!r}: {page.url}"
            )

        cards = await case_study_cards(page)
        result["result_count"] = len(cards)
        result["selected_value"] = option["label"]
        if not cards:
            raise AssertionError(
                f"Filtr tagu {option['label']!r} nie zwrócił żadnych case studies"
            )

        detail_page = await browser.new_page(
            viewport={"width": 1440, "height": 1000},
            ignore_https_errors=True,
        )
        invalid_urls = []
        try:
            for card in cards:
                case_study_url = card["url"]
                if case_study_url not in tag_cache:
                    await wait_for_page(detail_page, case_study_url)
                    tag_cache[case_study_url] = await case_study_tags(detail_page)
                if not tag_matches(tag_cache[case_study_url], option["label"]):
                    invalid_urls.append(case_study_url)
        finally:
            await detail_page.close()

        if invalid_urls:
            raise AssertionError(
                f"Filtr tagu {option['label']!r} wyświetlił case studies "
                f"bez tego tagu: {sorted(invalid_urls)}"
            )
        result["message"] = (
            f"Zweryfikowano tag {option['label']!r} na wszystkich "
            f"{len(cards)} case studies."
        )
        return

    select = page.locator("select:visible").nth(filter_info["index"])
    initial_text = await page.locator("body").inner_text(timeout=10000)
    await select.select_option(value=option["value"])

    await page.wait_for_function(
        """initial => location.href !== initial.url ||
        document.body.innerText !== initial.text""",
        arg={"url": page.url, "text": initial_text},
        timeout=10000,
    )

    signature = await page_signature(page)
    result["result_count"] = await visible_result_count(page)
    result["selected_value"] = option["label"]
    changed_url = page.url != result["url"]
    changed_content = signature != baseline_signature
    if not changed_content and not changed_url:
        raise AssertionError(
            f"Wybór {option['label']!r} nie zmienił adresu URL ani widocznych wyników"
        )
    if changed_url and changed_content:
        result["message"] = "Filtr zmienił adres URL i widoczną zawartość."
    elif changed_url:
        result["message"] = "Filtr zmienił adres URL."
    else:
        result["message"] = "Filtr zmienił widoczną zawartość."


async def run_case(
    browser: Browser,
    target_url: str,
    case_name: str,
    filter_info: dict[str, Any],
    option: dict[str, Any],
    baseline_signature: str,
    tag_cache: dict[str, set[str]],
    load_target: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "case": case_name,
        "filter": filter_info["name"],
        "option": option["label"],
        "url": target_url,
        "status": "OK",
        "result_count": None,
        "message": "",
        "screenshot": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    page = await browser.new_page(
        viewport={"width": 1440, "height": 1000}, ignore_https_errors=True
    )
    try:
        if load_target:
            await wait_for_page(page, target_url)
        await check_filter(
            browser, page, result, filter_info, option,
            baseline_signature, tag_cache,
            option.get("visible", True),
        )
    except Exception as error:
        result["status"] = "ERROR"
        result["message"] = str(error)
        result["screenshot"] = await take_screenshot(page, case_name)
    finally:
        await page.close()
    return result


async def collect_cases(browser: Browser, target_url: str) -> list[dict[str, Any]]:
    baseline_page = await browser.new_page(
        viewport={"width": 1440, "height": 1000}, ignore_https_errors=True
    )
    try:
        await wait_for_page(baseline_page, target_url)
        await baseline_page.locator(
            'select:visible, a[href*="tag="]:visible'
        ).first.wait_for(state="visible", timeout=30000)
        filters = await discover_filters(baseline_page)
        if not filters:
            raise AssertionError("Nie znaleziono widocznego filtra z dostępnymi opcjami")
        baseline_signature = await page_signature(baseline_page)
    finally:
        await baseline_page.close()

    results: list[dict[str, Any]] = []
    tag_cache: dict[str, set[str]] = {}
    write_reports(results)
    for filter_info in filters:
        for option in filter_info["options"]:
            case_name = f"{safe_filename(filter_info['name'])}-{safe_filename(option['label'])}"
            print(f"Testowanie: {case_name}", flush=True)
            result = await run_case(
                browser, target_url, case_name, filter_info, option,
                baseline_signature, tag_cache,
                load_target=option.get("visible", True)
                if filter_info.get("kind") == "tag"
                else True,
            )
            results.append(result)
            write_reports(results)
    return results


def write_reports(results: list[dict[str, Any]]) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / f"{REPORT_NAME}.json"
    csv_path = REPORTS_DIR / f"{REPORT_NAME}.csv"
    html_path = REPORTS_DIR / f"{REPORT_NAME}.html"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "case", "filter", "option", "url", "status", "result_count",
        "message", "screenshot", "checked_at",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: result.get(field) for field in fields} for result in results)

    total = len(results)
    errors = sum(result.get("status") != "OK" for result in results)
    rows = []
    for result in sorted(results, key=lambda item: item.get("status") == "OK"):
        screenshot = ""
        if result.get("screenshot"):
            path = html.escape("../" + str(result["screenshot"]).replace("\\", "/"))
            screenshot = f'<a href="{path}" target="_blank">zrzut ekranu</a>'
        rows.append(
            f"<tr class=\"{'ok' if result.get('status') == 'OK' else 'error'}\">"
            f"<td>{html.escape(str(result.get('case', '')))}</td>"
            f"<td>{html.escape(str(result.get('filter', '')))}</td>"
            f"<td>{html.escape(str(result.get('option', '')))}</td>"
            f"<td>{html.escape(str(result.get('status', '')))}</td>"
            f"<td>{html.escape(str(result.get('result_count', '')))}</td>"
            f"<td>{html.escape(str(result.get('message', '')))} {screenshot}</td></tr>"
        )
    document = f"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><title>Raport filtrów Experd</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
.summary, table {{ background: white; }} .summary {{ padding: 15px; margin-bottom: 20px; }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ border: 1px solid #ccc;
padding: 8px; text-align: left; vertical-align: top; }} th {{ background: #333; color: white; }}
tr.ok {{ background: #effff0; }} tr.error {{ background: #fff0f0; }}
</style></head><body><h1>Raport filtrów Experd</h1>
<div class="summary"><strong>Łącznie testów:</strong> {total}<br>
<strong>Poprawne:</strong> {total - errors}<br><strong>Z błędami:</strong> {errors}<br>
<strong>Data wygenerowania:</strong> {html.escape(datetime.now(timezone.utc).isoformat())}</div>
<table><tr><th>Przypadek</th><th>Filtr</th><th>Opcja</th><th>Status</th>
<th>Wyniki</th><th>Szczegóły</th></tr>{"".join(rows)}</table></body></html>"""
    html_path.write_text(document, encoding="utf-8")


async def run(args: argparse.Namespace) -> int:
    try:
        target_url = load_target(args.input)
    except (FileNotFoundError, ValueError) as error:
        print(f"BŁĄD: {error}", file=sys.stderr)
        write_reports([{
            "case": "inicjalizacja",
            "url": str(args.input),
            "status": "ERROR",
            "message": str(error),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }])
        print(f"Raporty zapisano w: {REPORTS_DIR}")
        return 1

    print(f"Rozpoczynanie testów filtrów Experd dla {target_url}", flush=True)
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=not args.headed, timeout=30000
            )
        except Exception as error:
            message = f"Nie udało się uruchomić Chromium w Playwright: {error}"
            print(f"BŁĄD: {message}", file=sys.stderr)
            write_reports([{
                "case": "inicjalizacja", "url": target_url, "status": "ERROR",
                "message": message, "checked_at": datetime.now(timezone.utc).isoformat(),
            }])
            return 1
        try:
            results = await collect_cases(browser, target_url)
        except Exception as error:
            results = [{
                "case": "inicjalizacja", "url": target_url, "status": "ERROR",
                "message": str(error), "checked_at": datetime.now(timezone.utc).isoformat(),
            }]
        finally:
            await browser.close()

    write_reports(results)
    errors = sum(result.get("status") != "OK" for result in results)
    print(
        f"Sprawdzono filtrów: {len(results)}; "
        f"poprawne: {len(results) - errors}; błędne: {errors}"
    )
    print(f"Raporty zapisano w: {REPORTS_DIR}")
    return 2 if errors else 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test filters on an Experd page")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="File containing the page URL")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(parse_arguments())))
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)