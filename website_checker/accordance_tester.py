import argparse
import asyncio
import csv
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.async_api import Browser, Page, async_playwright


BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
REPORT_NAME = "accordance_report"
DEFAULT_INPUT = "accordance_tester_paths.txt"
CARD_SELECTOR = 'a[data-ga-cta="engineer_card"]'

STATUS_LABELS = {
    "OK": "ZGODNY",
    "ERROR": "NIEZGODNY",
}


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") + "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def load_sites(filename: str) -> tuple[str, str]:
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
    if len(urls) != 2:
        raise ValueError(
            f"Oczekiwano dokładnie dwóch adresów URL w pliku {path}; "
            f"znaleziono: {len(urls)}."
        )

    technology_url = next((url for url in urls if "/technologies" in urlparse(url).path), None)
    engineers_url = next((url for url in urls if "/engineers" in urlparse(url).path), None)
    if not technology_url or not engineers_url:
        raise ValueError(
            "Plik wejściowy musi zawierać jeden adres strony /technologies/ "
            "i jeden adres strony /engineers/."
        )
    return normalize_url(technology_url), normalize_url(engineers_url)


async def wait_for_page(page: Page, url: str) -> None:
    await page.goto(url, wait_until="load", timeout=30000)


async def engineer_cards(page: Page) -> list[dict[str, str]]:
    count = await page.locator(CARD_SELECTOR).count()
    cards = []
    for index in range(count):
        card = page.locator(CARD_SELECTOR).nth(index)
        cards.append({
            "url": normalize_url(await card.get_attribute("href") or ""),
            "text": (await card.inner_text()).strip(),
        })
    return cards


async def pagination_urls(page: Page, current_url: str) -> list[str]:
    locator = page.locator('a[href*="page="]')
    count = await locator.count()
    urls = []
    for index in range(count):
        href = await locator.nth(index).get_attribute("href")
        if not href:
            continue
        url = normalize_url(urljoin(current_url, href))
        if url != normalize_url(current_url):
            urls.append(url)
    return urls


async def all_engineer_cards(page: Page, engineers_url: str) -> list[dict[str, str]]:
    pending = [normalize_url(engineers_url)]
    visited: set[str] = set()
    cards_by_url: dict[str, dict[str, str]] = {}

    while pending:
        page_url = pending.pop(0)
        if page_url in visited:
            continue
        visited.add(page_url)

        await wait_for_page(page, page_url)
        await page.locator(CARD_SELECTOR).first.wait_for(
            state="visible", timeout=30000
        )
        for card in await engineer_cards(page):
            cards_by_url[card["url"]] = card

        for next_url in await pagination_urls(page, page_url):
            if next_url not in visited and next_url not in pending:
                pending.append(next_url)

    return list(cards_by_url.values())


async def technology_links(page: Page, base_url: str) -> list[dict[str, str]]:
    locator = page.locator('a[href*="/technologies/"]')
    count = await locator.count()
    links = []
    seen: set[str] = set()
    for index in range(count):
        link = locator.nth(index)
        href = urljoin(base_url, await link.get_attribute("href") or "")
        href = normalize_url(href)
        if href.rstrip("/") == base_url.rstrip("/"):
            continue
        if href in seen:
            continue
        seen.add(href)
        text_lines = [
            line.strip()
            for line in (await link.inner_text()).splitlines()
            if line.strip()
        ]
        links.append({"name": text_lines[0] if text_lines else href, "url": href})
    return links


async def profile_technology_links(page: Page, profile_url: str) -> set[str]:
    await wait_for_page(page, profile_url)
    links = await technology_links(page, profile_url)
    return {link["url"] for link in links}


def localized_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "adres_strony_technologii": report["technology_index"],
        "adres_strony_inżynierów": report["engineers_page"],
        "liczba_technologii": report["technology_count"],
        "liczba_inżynierów": report["engineers_page_count"],
        "technologie": [
            {
                "technologia": result["technology"],
                "adres_url": result["url"],
                "liczba_inżynierów": result["engineer_count"],
                "brak_na_stronie_inżynierów": result[
                    "missing_from_engineers_page"
                ],
                "zduplikowane_karty_inżynierów": result["duplicate_engineers"],
                "brak_odnośnika_do_technologii_w_profilu": result[
                    "profile_tag_mismatches"
                ],
                "status": STATUS_LABELS[result["status"]],
                "komunikat": result["message"],
                "data_sprawdzenia": result["checked_at"],
            }
            for result in report["technology_tags"]
        ],
        "sprzeczności": report["contradictions"],
        "data_sprawdzenia": report["checked_at"],
    }


def write_reports(report: dict[str, Any]) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    results = report["technology_tags"]
    (REPORTS_DIR / f"{REPORT_NAME}.json").write_text(
        json.dumps(localized_report(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fields = [
        ("Technologia", "technology"),
        ("Adres URL", "url"),
        ("Liczba inżynierów", "engineer_count"),
        ("Brak na stronie inżynierów", "missing_from_engineers_page"),
        ("Zduplikowane karty inżynierów", "duplicate_engineers"),
        ("Brak odnośnika do technologii w profilu", "profile_tag_mismatches"),
        ("Status", "status"),
        ("Komunikat", "message"),
        ("Sprawdzono", "checked_at"),
    ]
    with (REPORTS_DIR / f"{REPORT_NAME}.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=[label for label, _ in fields])
        writer.writeheader()
        for result in results:
            writer.writerow({
                label: STATUS_LABELS[result[field]] if field == "status"
                else result.get(field)
                for label, field in fields
            })

    ordered_results = sorted(
        results,
        key=lambda result: result["status"] == "OK",
    )
    rows = []
    for result in ordered_results:
        status_class = "ok" if result["status"] == "OK" else "error"
        details = []
        if result["missing_from_engineers_page"]:
            details.append(
                "<h4>Brak na stronie inżynierów</h4><ul>"
                + "".join(
                    f"<li>{html.escape(url)}</li>"
                    for url in result["missing_from_engineers_page"]
                )
                + "</ul>"
            )
        if result["duplicate_engineers"]:
            details.append(
                "<h4>Powielone karty inżynierów</h4><ul>"
                + "".join(
                    f"<li>{html.escape(url)}</li>"
                    for url in result["duplicate_engineers"]
                )
                + "</ul>"
            )
        if result["profile_tag_mismatches"]:
            details.append(
                "<h4>Brak znacznika technologii w profilu inżyniera</h4><ul>"
                + "".join(
                    f"<li>{html.escape(url)}</li>"
                    for url in result["profile_tag_mismatches"]
                )
                + "</ul>"
            )
        details_html = "".join(details) or "<p>Brak błędów.</p>"
        rows.append(
            f"<tr class='{status_class}'><td>{html.escape(result['technology'])}</td>"
            f"<td><a href='{html.escape(result['url'])}' target='_blank'>"
            f"{html.escape(result['url'])}</a></td><td>{result['engineer_count']}</td>"
            f"<td>{STATUS_LABELS[result['status']]}</td>"
            f"<td>{html.escape(result['message'])}"
            f"<details><summary>Szczegóły</summary>{details_html}</details></td></tr>"
        )
    contradictions = report["contradictions"]
    contradiction_html = "".join(
        f"<li class='error-detail'>{html.escape(item)}</li>" for item in contradictions
    ) or "<li>Brak</li>"
    error_count = sum(result["status"] != "OK" for result in results)
    document = f"""<!doctype html><html lang="pl"><head><meta charset="utf-8">
<title>Raport zgodności stron</title>
<style>body{{font-family:Arial;margin:20px}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:8px;text-align:left;vertical-align:top}}
.ok{{background:#effff0}}.error{{background:#fff0f0}}
details{{margin-top:8px}}.error-detail{{margin-bottom:6px}}</style></head><body>
<h1>Raport zgodności stron</h1>
<p>Liczba technologii: {report['technology_count']} |
Liczba inżynierów na stronie: {report['engineers_page_count']} |
Błędne kontrole: {error_count} | Sprzeczności: {len(contradictions)}</p>
<section><h2>Błędy i sprzeczności</h2>
<p>Niespełnione kontrole i wykryte niezgodności są wymienione poniżej.</p>
<ul>{contradiction_html}</ul></section>
<table><tr><th>Technologia</th><th>Adres URL technologii</th>
<th>Liczba inżynierów</th><th>Status</th><th>Szczegóły</th></tr>{''.join(rows)}</table>
</body></html>"""
    (REPORTS_DIR / f"{REPORT_NAME}.html").write_text(document, encoding="utf-8")


async def run(args: argparse.Namespace) -> int:
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        technology_index, engineers_page = load_sites(args.input)
    except (FileNotFoundError, ValueError) as error:
        print(f"BŁĄD: {error}", file=sys.stderr)
        return 1

    report: dict[str, Any] = {
        "technology_index": technology_index,
        "engineers_page": engineers_page,
        "technology_count": 0,
        "engineers_page_count": 0,
        "technology_tags": [],
        "contradictions": [],
        "checked_at": checked_at,
    }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed, timeout=30000)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000}, ignore_https_errors=True)
            main_cards = await all_engineer_cards(page, engineers_page)
            main_urls = {card["url"] for card in main_cards}
            report["engineers_page_count"] = len(main_urls)

            await wait_for_page(page, technology_index)
            await page.locator('a[href*="/technologies/"]').first.wait_for(
                state="visible", timeout=30000
            )
            tags = await technology_links(page, technology_index)
            report["technology_count"] = len(tags)
            tag_memberships: dict[str, set[str]] = {}

            for tag in tags:
                await wait_for_page(page, tag["url"])
                cards = await engineer_cards(page)
                urls = [card["url"] for card in cards]
                membership = set(urls)
                tag_memberships[tag["url"]] = membership
                missing = sorted(membership - main_urls)
                duplicates = sorted({url for url in urls if urls.count(url) > 1})
                result = {
                    "technology": tag["name"], "url": tag["url"],
                    "engineer_count": len(membership),
                    "missing_from_engineers_page": missing,
                    "duplicate_engineers": duplicates,
                    "profile_tag_mismatches": [],
                    "status": "OK" if not missing and not duplicates else "ERROR",
                    "message": "",
                    "checked_at": checked_at,
                }
                if missing:
                    report["contradictions"].extend(
                        f"Technologia {tag['name']} zawiera adres {url}, "
                        "którego nie ma na stronie inżynierów."
                        for url in missing
                    )
                if duplicates:
                    report["contradictions"].append(
                        f"Technologia {tag['name']} zawiera powielone karty "
                        f"inżynierów: {', '.join(duplicates)}."
                    )
                report["technology_tags"].append(result)
                write_reports(report)

            # Only flag the direction requested by the business rule:
            # a technology page must not list an engineer whose profile lacks
            # that technology tag. The reverse is allowed because tags may be
            # curated differently on profiles and technology landing pages.
            results_by_url = {
                result["url"]: result for result in report["technology_tags"]
            }
            for engineer_url in sorted(set().union(*tag_memberships.values()) if tag_memberships else set()):
                declared = await profile_technology_links(page, engineer_url)
                listed = {tag_url for tag_url, members in tag_memberships.items() if engineer_url in members}
                for tag_url in sorted(listed - declared):
                    results_by_url[tag_url]["profile_tag_mismatches"].append(engineer_url)
                    results_by_url[tag_url]["status"] = "ERROR"
                    report["contradictions"].append(
                        f"Inżynier {engineer_url} widnieje na stronie technologii "
                        f"{tag_url}, ale jego profil nie zawiera odnośnika do tej "
                        "technologii."
                    )
        finally:
            await browser.close()

    for result in report["technology_tags"]:
        if result["status"] == "OK":
            result["message"] = (
                "Wszyscy oznaczeni inżynierowie znajdują się na stronie inżynierów."
            )
    write_reports(report)
    errors = sum(result["status"] != "OK" for result in report["technology_tags"])
    errors += len(report["contradictions"])
    print(
        f"Sprawdzono {report['technology_count']} technologii i "
        f"{report['engineers_page_count']} inżynierów."
    )
    print(f"Liczba sprzeczności: {len(report['contradictions'])}")
    print(f"Raporty zapisano w: {REPORTS_DIR}")
    return 2 if errors else 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sprawdza zgodność stron technologii z profilami inżynierów."
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help="Plik zawierający adresy URL strony technologii i strony inżynierów.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Wyświetla okno przeglądarki podczas sprawdzania.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run(parse_arguments())))
    except KeyboardInterrupt:
        print("\nPrzerwano działanie na żądanie użytkownika.", file=sys.stderr)
        sys.exit(130)