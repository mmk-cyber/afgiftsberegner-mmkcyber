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


async def _card_text(card) -> str:
    """
    Bilbasen og DBA bruger begge et mønster hvor selve <a>-tagget er et usynligt overlay-link
    (fx class="...absolute inset-0...") uden egen tekst — den rigtige kort-tekst (pris, km, titel)
    sidder i den omsluttende <article>. Bekræftet ved manuel DOM-inspektion af begge sider (denne
    session): a.innerText er ALTID 0 tegn, mens a.closest('article').innerText har det rigtige
    indhold. Falder tilbage til parentElement, hvis der ikke findes en <article>-forfader.
    """
    try:
        return await card.evaluate(
            "el => (el.closest('article') || el.parentElement || el).innerText || ''"
        )
    except Exception:
        return await card.inner_text()


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
        # Kun ekskludér på karosseri, hvis en tydelig MODSAT betegnelse findes i sidens <title>
        # (ikke i hele body_text, som kan nævne "Touring" i fx udstyrslisten/tilbehør eller
        # "lignende annoncer"). VIGTIGT, fundet i praksis: DBA's <h1> indeholder KUN mærke/model
        # (fx "BMW 335i"), ALDRIG karosseri — <title> derimod har det pålideligt for begge sider
        # (fx "... - Coupé | DBA.dk" / "Brugt BMW 335i 3,0 Cabriolet DKG 2d - Bilbasen"). Brug
        # derfor <title>, ikke <h1>, som første forsøg.
        try:
            title_text = (await page.title()).lower()
        except Exception:
            title_text = ""
        if not title_text:
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
    # KRITISK FIX, fundet i praksis: den tidligere URL brugte mærke/model direkte som sti-segmenter
    # (/brugt/bil/<maerke>/<model>), hvilket kræver at man kender Bilbasens PRÆCISE interne slug
    # for hvert mærke (fx er "mercedes-benz" IKKE gyldig — det er ukendt hvad den rigtige er uden
    # at teste hvert mærke). Er slugget forkert, REDIRECTER Bilbasen stille til den generelle
    # "alle biler i Danmark"-liste (40.000+ biler) i stedet for at fejle — hvilket førte til at
    # scraperen leverede fuldstændig urelaterede biler (Citroën, Porsche, Renault m.fl.) som
    # "sammenligninger" for en Mercedes-forespørgsel. Løsning: brug Bilbasens egen fritekstsøgning
    # (samme felt som en bruger selv ville skrive i), som er markant mere robust og ikke kræver
    # at kende exakte slugs. Bekræftet manuelt: ?free=bmw+335i giver 13 korrekte BMW 335i-resultater,
    # ?free=Mercedes-Benz+CLA giver 61 korrekte Mercedes CLA-resultater.
    url = (
        f"https://www.bilbasen.dk/brugt/bil?free={quote(f'{maerke} {model}'.strip())}"
        f"&includeengroscvr=true&includeleasing=false"
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        page = await _new_page(browser)
        await _goto_and_settle(page, url, wait_selector="a[href*='/brugt/bil/']", timeout=25000)

        cards = await page.query_selector_all("a[href*='/brugt/bil/']")
        print(f"[DEBUG bilbasen] url={url} title={await page.title()!r} cards_found={len(cards)}", flush=True)
        if len(cards) == 0:
            body_snip = (await page.inner_text("body"))[:500]
            print(f"[DEBUG bilbasen] body_snippet={body_snip!r}", flush=True)
        seen_hrefs = set()
        candidates = []
        for card in cards:
            href = await card.get_attribute("href")
            if not href or href in seen_hrefs or not re.search(r"/\d+$", href):
                continue
            seen_hrefs.add(href)
            text = await _card_text(card)
            candidates.append((href, text))
            if len(candidates) >= max_candidates:
                break

        print(f"[DEBUG bilbasen] candidates_after_filter={len(candidates)}", flush=True)
        for i, (h, t) in enumerate(candidates[:3]):
            print(f"[DEBUG bilbasen] candidate{i} href={h!r} text_len={len(t)} text_repr={t[:200]!r}", flush=True)

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
        print(f"[DEBUG dba] url={url} title={await page.title()!r} cards_found={len(cards)}", flush=True)
        if len(cards) == 0:
            body_snip = (await page.inner_text("body"))[:500]
            print(f"[DEBUG dba] body_snippet={body_snip!r}", flush=True)
        seen_hrefs = set()
        candidates = []
        for card in cards:
            href = await card.get_attribute("href")
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            text = await _card_text(card)
            candidates.append((href, text))
            if len(candidates) >= max_candidates:
                break

        print(f"[DEBUG dba] candidates_after_filter={len(candidates)}", flush=True)
        for i, (h, t) in enumerate(candidates[:3]):
            print(f"[DEBUG dba] candidate{i} href={h!r} text_len={len(t)} text_repr={t[:200]!r}", flush=True)

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

    RETTET, VERIFICERET MANUELT (tidligere UBEKRÆFTET og forkert — brugte "/avanceret-sogning",
    som er en 404, den rigtige sti staves "/avanceret-soegning"): søge-URL'en er bekræftet ved
    manuelt at bruge "Avanceret søgning" på bilopslag.nu og observere adresselinjen. Query-
    parametrene er brand_in[]/model_in[]/variant_in[] (ikke fabrikat=/model=, som tidligere
    gættet). Modellen og eventuel variant er SEPARATE felter i den rigtige søgning (fx
    model_in[]=X5 og variant_in[]=M50i, ikke model_in[]="X5 M50i" som ét felt) — split derfor
    `model` på ordgrænser: første ord er model, resten (hvis noget) er variant.
    """
    # KRITISK FIX, fundet i praksis (BMW M3-sagen): bilopslag.nu's brand_in[]-filter matcher kun
    # PRÆCIS versaliseret mærkenavn (fx "BMW"), som er sådan Motorregistret-data er lagret. Sendes
    # mærket som brugeren selv tastede det (typisk småt, fx "bmw" fra Bilmærke-feltet), matcher
    # filteret intet som helst — Model-dropdownen viser "No options", og søgningen giver stille og
    # roligt 0 resultater, selvom mærket i virkeligheden findes med masser af biler. Bekræftet
    # manuelt: brand_in[]=bmw -> 0 resultater / ingen modelvalg; brand_in[]=BMW -> 282.716
    # resultater og "M3" som gyldig model. Versalisér derfor ALTID mærket her, uanset hvad
    # brugeren tastede — påvirker kun dette ene kald, ikke Bilbasen/DBA (som er case-insensitive).
    maerke_upper = maerke.strip().upper()
    model_words = model.split()
    model_only = model_words[0] if model_words else ""
    variant_only = " ".join(model_words[1:]) if len(model_words) > 1 else ""

    # SAMME FIX, udvidet til model/variant: bekræftet manuelt at model_in[]/variant_in[] også kun
    # matcher PRÆCIS versaliseret tekst (fx "M3", "M340d xDrive", "M3 Competition M Xdrive") —
    # model_in[]=m3 (småt, som brugeren typisk taster) blev ligeså stille droppet af siden som
    # brand_in[]=bmw gjorde. Modellernes rigtige forbogstavs-mønster er ikke fuldt ensartet (kan
    # ikke bare .upper()'es som mærket), men .title() rammer det korrekt i alle observerede
    # eksempler ("m3"->"M3", "m340d xdrive"->"M340d Xdrive"), så prøv den kasus FØRST, med original
    # og fuld upper-case som fallback, hvis title() alligevel ikke rammer den præcise stavning.
    def _casings(s: str) -> list[str]:
        seen, out = set(), []
        for v in (s.title(), s, s.upper()):
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    candidate_urls = []
    for model_case in (_casings(model_only) if model_only else [""]):
        for variant_case in (_casings(variant_only) if variant_only else [""]):
            params = f"brand_in[]={quote(maerke_upper)}"
            if model_case:
                params += f"&model_in[]={quote(model_case)}"
            if variant_case:
                params += f"&variant_in[]={quote(variant_case)}"
            candidate_urls.append(f"https://bilopslag.nu/avanceret-soegning?{params}")
    if variant_only:
        # Fallback uden variant-filter, i tilfælde af at variant-strengen slet ikke matcher en
        # dropdown-værdi (fx forskellig stavning/mellemrum) — bedre for mange resultater end ingen.
        for model_case in _casings(model_only):
            candidate_urls.append(f"https://bilopslag.nu/avanceret-soegning?brand_in[]={quote(maerke_upper)}&model_in[]={quote(model_case)}")
    # BEVIDST ingen sidste "kun mærke"-fallback uden modelfilter: ville give hundredtusindvis af
    # urelaterede biler (alle modeller af mærket), og main.py's mærke/variant-sikkerhedsnet
    # tjekker IKKE bilopslag.nu-backup'ets fund — hellere 0 resultater end forkerte sammenligninger.

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
            except Exception as e:
                print(f"[DEBUG bilopslag.nu] url={url} goto failed: {e}")
                continue
            links = await page.query_selector_all("a[href*='/nummerplade/'], a[href*='/stelnummer/']")
            for l in links:
                href = await l.get_attribute("href")
                if href and href not in plate_links:
                    plate_links.append(href)
            print(f"[DEBUG bilopslag.nu] url={url} plate_links_found={len(plate_links)}")
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



# Kendte tegn på at siden har blokeret vores hentning (bot-beskyttelse/captcha), i stedet for
# at vise selve annoncen. Fundet i praksis: et rigtigt Marco-forsøg ramte mobile.de's tyske
# "Zugriff verweigert" (adgang nægtet)-side, og vores kode brugte dengang blot DEN sides titel
# som "bilens navn" og søgte videre på det — hvilket gav meningsløse søgeresultater og en
# forvirrende fejl uden at fortælle hvad der reelt gik galt. Tjek derfor eksplicit for dette.
_BLOCKED_PAGE_HINTS = (
    "zugriff verweigert", "access denied", "attention required", "pardon our interruption",
    "verifying you are human", "checking your browser", "are you a robot", "unusual traffic",
    "cloudflare", "just a moment...", "blocked", "forbidden", "captcha",
)


def _detect_fuel_type(text: str) -> Optional[dict]:
    """
    Best-effort udtræk af drivmiddel fra en udenlandsk annonces sidetekst (typisk mobile.de's
    tyske "Kraftstoffart"-felt: Benzin/Diesel/Elektro/Plug-in-Hybrid). Tilføjet efter fund i
    praksis: værktøjet fandt korrekt CO2 for en elbil-annonce, men lod drivmiddel-feltet i UI'en
    stå på standardværdien "Benzin" — hvilket giver en helt forkert afgiftsberegning for el/
    plugin-hybrid (andet CO2-tillæg, indfasningsprocent og ekstra bundfradrag, se RATES i
    index.html). Returnerer None hvis intet sikkert kunne bestemmes, så frontend beholder
    brugerens egen manuelle valg i stedet for at gætte forkert.
    """
    t = text.lower()
    # Plug-in-hybrid TJEKKES FØRST, da almindelige "elektro"/"benzin"/"diesel"-ord ofte også
    # optræder i samme annoncetekst for en hybrid (fx "Benzin/Elektro Plug-in-Hybrid") og ellers
    # fejlagtigt ville blive matchet som en ren el- eller benzinbil.
    if re.search(r"plug-?in.?hybrid", t):
        return {"fuel": "phev", "display": "Plugin-hybrid"}
    if re.search(r"kraftstoffart[^a-zæøå]{0,20}elektro", t) or re.search(r"\belektro(?:antrieb|auto|fahrzeug)?\b", t):
        return {"fuel": "ev", "display": "Elbil"}
    if re.search(r"kraftstoffart[^a-zæøå]{0,20}diesel", t) or re.search(r"\bdiesel\b", t):
        # Afgiftsmæssigt identisk med benzin (begge "konventionel", jf. UI-note) — kun display-
        # teksten er forskellig.
        return {"fuel": "konventionel", "display": "Diesel"}
    if re.search(r"kraftstoffart[^a-zæøå]{0,20}benzin", t) or "benzin" in t:
        return {"fuel": "konventionel", "display": "Benzin"}
    return None


def detect_fuel_from_bilbasen_dba_text(text: str) -> Optional[dict]:
    """
    Best-effort udtræk af drivmiddel fra et Bilbasen/DBA-søgeresultat-korts RÅ tekst (fx
    RawListing.body_type_text), som allerede indeholder en dansk drivmiddel-mærkat i selve
    korttekstet (set i praksis: "...km rækkevidde\\n47,6 km/l\\nPlug-in\\n...", "...\\nEl\\n...",
    "...2998 cc ∙ Diesel ∙...", "...Benzin ∙...").

    Bruges til at gætte drivmiddel ved STRUKTURERET/fritekst-opslag (mærke/model-søgning), hvor
    der — modsat link-opslag på en enkelt udenlandsk annonce — ikke findes én bestemt annonce-
    side at hente et "Kraftstoffart"-felt fra (se _detect_fuel_type ovenfor). I stedet bruges
    Bilbasen/DBA's egne søgeresultat-kort som kilde; main.py tager flertallet blandt de fundne
    (allerede år/mærke/variant-filtrerede) sammenligninger som bedste gæt.
    """
    t = text.lower()
    if re.search(r"\bplug-?in\b", t):
        return {"fuel": "phev", "display": "Plugin-hybrid"}
    if re.search(r"\bel\b", t):
        return {"fuel": "ev", "display": "Elbil"}
    if re.search(r"\bdiesel\b", t):
        return {"fuel": "konventionel", "display": "Diesel"}
    if re.search(r"\bbenzin\b", t):
        return {"fuel": "konventionel", "display": "Benzin"}
    return None


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
        page_title = ""
        try:
            page_title = await page.title()
        except Exception:
            pass
        await browser.close()

        check_text = f"{title} {page_title} {text[:500]}".lower()
        blocked = any(hint in check_text for hint in _BLOCKED_PAGE_HINTS)
        if blocked or not title.strip():
            return {
                "title": "",
                "km": None,
                "co2": None,
                "raw_text_snippet": text[:1000],
                "blocked": blocked,
            }

        km_match = re.search(r"([\d.]+)\s*km", text)
        co2_match = re.search(r"CO2[^\d]{0,15}([\d]+)\s*g/km", text, re.IGNORECASE)
        fuel_info = _detect_fuel_type(text)
        return {
            "title": title.strip(),
            "km": _parse_number(km_match.group(1)) if km_match else None,
            "co2": int(co2_match.group(1)) if co2_match else None,
            "fuelType": fuel_info["fuel"] if fuel_info else None,
            "fuelDisplay": fuel_info["display"] if fuel_info else None,
            "raw_text_snippet": text[:1000],
            "blocked": False,
        }
