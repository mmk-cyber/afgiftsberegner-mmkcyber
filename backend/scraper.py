"""
Scraper for Bilbasen, DBA og bilopslag.nu — encoder den "Fast metode" og de kendte faldgruber
fra afgiftsberegner-mmkcyber's index.html-instruktioner som kode, i stedet for at et menneske
(eller Claude i en browser) gør det manuelt hver gang.

VIGTIGT: Dette er skrevet UDEN mulighed for at teste mod de faktiske sider (mit sandbox-miljø
har ingen netværksadgang til bilbasen.dk/dba.dk/bilopslag.nu). Selector-navne og JSON-strukturer
er baseret på hvad der er observeret manuelt via browser i tidligere samtaler, men SKAL
verificeres og formentlig justeres, når scraperen faktisk køres et sted med netværksadgang.

Bruger Playwright (headless Chromium), ikke bare `requests`, fordi Bilbasen/DBA/bilopslag.nu
alle så ud til at være JS-renderede sider i tidligere manuel browsing denne session — et rent
HTTP-fetch uden JS-eksekvering vil sandsynligvis ikke se annonce-/registreringsafgiftsdata.
Det betyder hosting skal understøtte headless Chromium (Docker-baseret platform, ikke en let
serverless-funktion) — se README.md.
"""
import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

from playwright.async_api import async_playwright, Page

EXCLUDE_PATTERNS = re.compile(
    r"uden afgift|engros|cvr[\s-]?nummer|eksport(?:pris|godtgørelse)?",
    re.IGNORECASE,
)

WRONG_BODY_HINTS = {
    # Ord i variant/titel-tekst der IKKE må optræde, hvis brugerens bil er af en given type.
    # Bruges kun som et hurtigt, billigt filter FØR vi besøger detalje-siden — den endelige
    # afgørelse skal stadig ske ud fra hele beskrivelsesteksten/TYPE-feltet, jf. instruktionerne
    # om at "Variant"-teksten alene ikke er pålidelig.
    "cabriolet": ["coupé", "coupe", "touring", "stationcar", "sedan", " 4d"],
    "coupe": ["cabriolet", "touring", "stationcar", "sedan", " 4d"],
    "coupé": ["cabriolet", "touring", "stationcar", "sedan", " 4d"],
    "stationcar": ["cabriolet", "coupé", "coupe", "sedan"],
    "touring": ["cabriolet", "coupé", "coupe", "sedan"],
    "sedan": ["cabriolet", "coupé", "coupe", "touring", "stationcar"],
}


@dataclass
class RawListing:
    kilde: str
    beskrivelse: str
    pris: float
    dato: str
    km: float
    link: str
    body_type_text: str = ""
    excluded: bool = False
    exclude_reason: str = ""
    vaerdi_u_afgift: float = 0
    regafgift: float = 0
    opkraevet: float = 0
    andel_pct: float = 0


def _parse_number(text: str) -> float:
    """'189.900 kr.' -> 189900.0"""
    digits = re.sub(r"[^\d]", "", text or "")
    return float(digits) if digits else 0.0


async def _new_page(browser) -> Page:
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="da-DK",
    )
    page = await context.new_page()
    return page


async def _goto_and_settle(page: Page, url: str, wait_selector: Optional[str] = None,
                            timeout: int = 15000, settle_ms: int = 800):
    """
    Erstatning for wait_until="networkidle", som viste sig at time'e ud i praksis (bekræftet
    live på Render mod dba.dk — siden er faktisk hurtigt klar, men baggrunds-telemetri/annoncer
    gør at netværket aldrig bliver "idle" inden for 30s). "domcontentloaded" er langt mere
    robust, evt. efterfulgt af en eksplicit ventning på et konkret element vi ved skal komme.
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    if wait_selector:
        try:
            await page.wait_for_selector(wait_selector, timeout=timeout)
        except Exception:
            pass
    else:
        await page.wait_for_timeout(settle_ms)


async def check_listing_body_and_tax_status(page: Page, url: str, expected_body: Optional[str]) -> tuple[bool, str]:
    """
    Besøger en enkelt annonce og afgør om den skal EKSKLUDERES.
    Returnerer (excluded, reason). Kun IKKE-ekskluderet == brugbar.

    Dette er den vigtigste — og dyreste (én sidevisning pr. kandidat) — del af hele scraperen,
    fordi "Uden afgift"/"Engros" ofte kun står i selve annoncens brødtekst, ikke i søgeresultat-
    kortet (fundet i praksis, se index.html-instruktionerne).
    """
    try:
        await _goto_and_settle(page, url, timeout=25000, settle_ms=1200)
    except Exception as e:
        return True, f"kunne ikke hente annonce: {e}"

    body_text = (await page.inner_text("body")).lower()

    if EXCLUDE_PATTERNS.search(body_text):
        return True, "uden afgift / engros / CVR nævnt i annoncens beskrivelse"

    if expected_body:
        wrong_words = WRONG_BODY_HINTS.get(expected_body.lower(), [])
        # Kun ekskludér på karosseri, hvis en tydelig MODSAT betegnelse findes i overskriften
        # (ikke i hele body_text, som kan nævne "Touring" i fx udstyrslisten/tilbehør).
        try:
            title_text = (await page.inner_text("h1")).lower()
        except Exception:
            title_text = body_text[:300]
        for w in wrong_words:
            if w in title_text:
                return True, f"karosseri ser forkert ud ('{w}' fundet i overskrift, forventede {expected_body})"

    return False, ""


async def search_bilbasen(maerke: str, model: str, expected_body: Optional[str] = None,
                           max_candidates: int = 15) -> list[RawListing]:
    url = (
        f"https://www.bilbasen.dk/brugt/bil/{quote(maerke.lower())}/{quote(model.lower())}"
        f"?includeengroscvr=true&includeleasing=false"
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        page = await _new_page(browser)
        await _goto_and_settle(page, url, wait_selector="a[href*='/brugt/bil/']", timeout=25000)

        cards = await page.query_selector_all("a[href*='/brugt/bil/']")
        seen_hrefs = set()
        candidates = []
        for card in cards:
            href = await card.get_attribute("href")
            if not href or href in seen_hrefs or not re.search(r"/\d+$", href):
                continue
            seen_hrefs.add(href)
            text = await card.inner_text()
            candidates.append((href, text))
            if len(candidates) >= max_candidates:
                break

        results = []
        for href, text in candidates:
            full_url = href if href.startswith("http") else f"https://www.bilbasen.dk{href}"
            price_match = re.search(r"([\d.]+)\s*kr", text)
            date_match = re.search(r"(\d{1,2}/\d{4})", text)
            km_match = re.search(r"([\d.]+)\s*km", text)
            if not price_match:
                continue
            pris = _parse_number(price_match.group(1))
            if pris <= 0:
                continue

            excluded, reason = await check_listing_body_and_tax_status(page, full_url, expected_body)
            results.append(RawListing(
                kilde="Bilbasen",
                beskrivelse=text.replace("\n", " ")[:120],
                pris=pris,
                dato=date_match.group(1) if date_match else "",
                km=_parse_number(km_match.group(1)) if km_match else 0,
                link=full_url,
                body_type_text=text,
                excluded=excluded,
                exclude_reason=reason,
            ))

        await browser.close()
        return results


async def search_dba(query: str, expected_body: Optional[str] = None, max_candidates: int = 15) -> list[RawListing]:
    url = f"https://www.dba.dk/mobility/search/car?q={quote(query)}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        page = await _new_page(browser)
        await _goto_and_settle(page, url, wait_selector="a[href*='/mobility/item/']", timeout=25000)

        cards = await page.query_selector_all("a[href*='/mobility/item/']")
        seen_hrefs = set()
        candidates = []
        for card in cards:
            href = await card.get_attribute("href")
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            text = await card.inner_text()
            candidates.append((href, text))
            if len(candidates) >= max_candidates:
                break

        results = []
        for href, text in candidates:
            full_url = href if href.startswith("http") else f"https://www.dba.dk{href}"
            price_match = re.search(r"([\d.]+)\s*kr", text)
            date_match = re.search(r"(\d{4})", text)
            km_match = re.search(r"([\d.]+)\s*km", text)
            if not price_match:
                continue
            pris = _parse_number(price_match.group(1))
            if pris <= 0:
                continue

            excluded, reason = await check_listing_body_and_tax_status(page, full_url, expected_body)
            results.append(RawListing(
                kilde="DBA",
                beskrivelse=text.replace("\n", " ")[:120],
                pris=pris,
                dato=date_match.group(1) if date_match else "",
                km=_parse_number(km_match.group(1)) if km_match else 0,
                link=full_url,
                body_type_text=text,
                excluded=excluded,
                exclude_reason=reason,
            ))

        await browser.close()
        return results


async def bilopslag_registreringsafgift(regnr_or_stelnummer: str) -> Optional[dict]:
    """
    Slår ét nummerplade/stelnummer op på bilopslag.nu og henter SENESTE række i
    Registreringsafgift-sektionen (Handelspris, Værdi u. afgift, Registreringsafgift,
    Opkrævet afgift, Andel %) samt km-stand fra Overblik-sektionen.

    Returnerer None hvis bilen ikke har en udfyldt Registreringsafgift-sektion
    (kun et mindretal af biler har det, jf. instruktionerne — helt normalt, ikke en fejl).
    """
    url = f"https://bilopslag.nu/nummerplade/{quote(regnr_or_stelnummer)}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        page = await _new_page(browser)
        await _goto_and_settle(page, url, timeout=25000, settle_ms=1200)

        full_text = await page.inner_text("body")

        km_match = re.search(r"KM-STAND\s*\n?([\d.]+)\s*km", full_text, re.IGNORECASE)
        km = _parse_number(km_match.group(1)) if km_match else 0

        idx = full_text.find("Registreringsafgift", full_text.find("Registreringsafgift") + 5)
        if idx == -1:
            await browser.close()
            return None

        section = full_text[idx: idx + 800]
        row_match = re.search(
            r"(\d{2}-\d{2}-\d{4}).*?\n([\d.]+)\s*kr\.\s*\n([\d.]+)\s*kr\.\s*\n([\d.]+)\s*kr\.\s*\n([\d.]+)\s*kr\.\s*\n([\d.]+)\s*kr\.\s*\n([\d,]+)\s*%",
            section, re.DOTALL,
        )
        # NB: dette regex-mønster er et BEDSTE BUD baseret på tekst-layoutet observeret manuelt
        # (dato / vurderingstype / handelspris / nypris / værdi u. afgift / registreringsafgift /
        # opkrævet / andel%) — skal verificeres når scraperen faktisk kan køre mod den rigtige side.
        await browser.close()
        if not row_match:
            return {"raw_section": section, "km": km, "parsed": False}

        return {
            "dato": row_match.group(1),
            "handelspris": _parse_number(row_match.group(2)),
            "vaerdi_u_afgift": _parse_number(row_match.group(4)),
            "regafgift": _parse_number(row_match.group(5)),
            "opkraevet": _parse_number(row_match.group(6)),
            "andel_pct": float(row_match.group(7).replace(",", ".")),
            "km": km,
            "parsed": True,
        }


async def search_bilopslag_nu(maerke: str, model: str, max_candidates: int = 10) -> list[RawListing]:
    """
    Backup-søgning på bilopslag.nu efter mærke/model, brugt KUN når Bilbasen+DBA tilsammen
    giver færre end 4 gode matches (samme regel som "Fast metode" i index.html).

    USIKKERT/UBEKRÆFTET: Jeg har ikke en verificeret søge-URL for bilopslag.nu's "avanceret
    søgning" fra denne session — kun at enkelt-bil-opslag (nummerplade/stelnummer) virker
    pålideligt (det er det, `bilopslag_registreringsafgift()` bruger, og det ER testet manuelt
    flere gange tidligere i denne sags-serie). Denne funktion forsøger et par sandsynlige
    URL-mønstre og falder tilbage til at give et tomt resultat + en warning, hvis ingen af dem
    finder gyldige nummerplade-/stelnummer-links. Ret `candidate_urls` herunder, når den
    rigtige søge-URL er bekræftet (fx ved at åbne "avanceret søgning" på siden manuelt og
    kigge på adresselinjen).
    """
    candidate_urls = [
        f"https://bilopslag.nu/avanceret-sogning?fabrikat={quote(maerke.upper())}&model_in[]={quote(model)}",
        f"https://bilopslag.nu/soegning?fabrikat={quote(maerke.upper())}&model={quote(model)}",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        page = await _new_page(browser)

        plate_links: list[str] = []
        for url in candidate_urls:
            try:
                await _goto_and_settle(
                    page, url,
                    wait_selector="a[href*='/nummerplade/'], a[href*='/stelnummer/']",
                    timeout=20000,
                )
            except Exception:
                continue
            links = await page.query_selector_all("a[href*='/nummerplade/'], a[href*='/stelnummer/']")
            for l in links:
                href = await l.get_attribute("href")
                if href and href not in plate_links:
                    plate_links.append(href)
            if plate_links:
                break  # første URL-mønster der gav resultater bruges

        await browser.close()

    results: list[RawListing] = []
    for href in plate_links[:max_candidates]:
        plate = href.rstrip("/").split("/")[-1]
        data = await bilopslag_registreringsafgift(plate)
        if not data or not data.get("parsed"):
            continue
        results.append(RawListing(
            kilde="bilopslag.nu",
            beskrivelse=f"{maerke} {model} ({plate})",
            pris=data["handelspris"],
            dato=data["dato"],
            km=data["km"],
            link=f"https://bilopslag.nu/nummerplade/{plate}",
            vaerdi_u_afgift=data["vaerdi_u_afgift"],
            regafgift=data["regafgift"],
            opkraevet=data["opkraevet"],
            andel_pct=data["andel_pct"],
        ))
    return results


async def fetch_foreign_listing(url: str) -> dict:
    """
    Best-effort udtræk af mærke/model/km/år/CO2 fra en udenlandsk annonce (fx mobile.de).
    Kan IKKE garantere at finde alle felter — bruges kun som udgangspunkt, brugeren/frontend
    skal kunne rette felterne manuelt bagefter.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        page = await _new_page(browser)
        await _goto_and_settle(page, url, timeout=20000, settle_ms=1200)
        text = await page.inner_text("body")
        title = ""
        try:
            title = await page.inner_text("h1")
        except Exception:
            pass
        km_match = re.search(r"([\d.]+)\s*km", text)
        co2_match = re.search(r"CO2[^\d]{0,15}([\d]+)\s*g/km", text, re.IGNORECASE)
        await browser.close()
        return {
            "title": title.strip(),
            "km": _parse_number(km_match.group(1)) if km_match else None,
            "co2": int(co2_match.group(1)) if co2_match else None,
            "raw_text_snippet": text[:1000],
        }
