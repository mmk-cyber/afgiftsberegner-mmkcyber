"""
Afgiftsberegner — automatisk backend.

POST /beregn
  Body: { "input": "<mobile.de-link ELLER fritekst, fx 'BMW 335i Cabriolet 2010, kørt 175000 km'>",
          "co2": <valgfri, override>, "months": 12, "downpct": 10, "rate": 6, "restRente": 3.8 }

  Kører "Fast metode": Bilbasen+DBA først, bilopslag.nu kun som backup/supplement for
  "Værdi u. afgift"-feltet. Returnerer beregnet resultat + de sammenligninger der blev brugt,
  så resultatet altid kan efterprøves — appen skal ALDRIG bare vise et tal uden at vise
  grundlaget bag.

Kør lokalt: uvicorn main:app --reload
Se README.md for deployment (kræver headless Chromium — se scraper.py's docstring).
"""
import asyncio
import re
from datetime import date
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import calc
import scraper

app = FastAPI(title="Afgiftsberegner backend")

# Tillad kald fra selve GitHub Pages-værktøjet. Udskift med jeres faktiske domæne for at
# undgå at hvem-som-helst kan bruge jeres server som gratis scraping-proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://mmk-cyber.github.io", "http://localhost:8000", "*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


class BeregnRequest(BaseModel):
    # "input": link ELLER fritekst — bagudkompatibel fritekst-vej (bruges bl.a. når Claude i en
    # chat-samtale indsætter et link manuelt som backup). Tom streng hvis de strukturerede felter
    # (maerke/model/year/km) bruges i stedet fra den nye UI — se note ved is_link/elif nedenfor.
    input: str = ""
    maerke: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    km: Optional[float] = None
    co2: Optional[float] = None
    fuel_type: str = "konventionel"
    months: float = 12
    downpct: float = 10
    rate: float = 6
    restRente: float = 3.8


def parse_free_text(text: str) -> dict:
    """
    Meget simpelt best-effort parse af fritekst som 'BMW 335i Cabriolet 2010, kørt 175000 km'.
    Finder: årstal (4 cifre, 1950-2029), km-tal (efterfulgt af 'km'), og bruger resten som
    mærke/model/variant-gæt. IKKE robust — frontend skal altid vise felterne til godkendelse/
    rettelse, aldrig regne blindt videre på et uverificeret gæt.
    """
    year_match = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", text)
    km_match = re.search(r"([\d.,]+)\s*km", text, re.IGNORECASE)
    year = int(year_match.group(1)) if year_match else None
    km_val = None
    if km_match:
        km_val = float(re.sub(r"[^\d]", "", km_match.group(1)))
    # mærke/model = alt før årstal/km-omtale, rimeligt grov heuristik
    name_part = text
    if year_match:
        name_part = name_part[: year_match.start()]
    # RETTET, fejl fundet i praksis: en bruger skrev "BMW X5 M50i, 1. reg. 05/2022" — årstals-
    # afskæringen ovenfor fjernede kun "2022" og efterlod "1. reg. 05/" hængende på bilnavnet,
    # hvilket ødelagde Bilbasen/DBA-søgningen fuldstændig (søgte reelt efter "bmw x5 m50i, 1.
    # reg. 05/", 0 resultater). Fjern derfor eksplicit "reg."-datoangivelser og efterladte
    # tal/skråstreger for enden, FØR vi bruger teksten som søgestreng.
    name_part = re.sub(r"\b\d{1,2}\.?\s*reg(?:istrering)?\.?", "", name_part, flags=re.IGNORECASE)
    # RETTET, ny fejl fundet i praksis: når teksten IKKE indeholder et årstal (fx "BMW X5 45e,
    # kørt 90.000 km fra ny"), blev intet afskåret ovenfor, så hele "kørt 90.000 km fra ny" blev
    # stående i bilnavnet og indgik i selve Bilbasen/DBA-søgestrengen — søgningen ledte reelt
    # efter en bil hvis beskrivelse indeholder ordene "kørt", "90000", "fra" og "ny", hvilket intet
    # rigtigt opslag matcher, og gav 0 resultater for en helt almindelig, findbar bil. "kørt"/
    # "kørt"-tastefejlen "kært" markerer på dansk ALTID starten på kilometer-/tilstandsangivelsen
    # i en annoncetekst og er aldrig en del af mærke/model-navnet — afskær derfor hårdt her, uanset
    # om der findes et årstal i teksten eller ej.
    kort_match = re.search(r"\bk[øæ]rt\b", name_part, re.IGNORECASE)
    if kort_match:
        name_part = name_part[: kort_match.start()]
    name_part = re.sub(r"[\d./]+\s*$", "", name_part)
    name_part = name_part.strip(" ,.-/")
    return {"carName": name_part or text.strip(), "year": year, "km": km_val}


def months_since(year: Optional[int]) -> Optional[float]:
    if not year:
        return None
    from datetime import date
    today = date.today()
    # Antager midt på året (juni), medmindre en mere præcis dato kendes — flag altid denne
    # antagelse til brugeren, jf. tidligere praksis i denne sags-serie.
    return (today.year - year) * 12 + (today.month - 6)


@app.post("/beregn")
async def beregn(req: BeregnRequest):
    is_link = req.input.strip().startswith("http")
    warnings: list[str] = []
    fuel_type = req.fuel_type  # kan overskrives nedenfor, hvis vi kan auto-detektere den fra annoncen
    fuel_display: Optional[str] = None

    if is_link:
        foreign = await scraper.fetch_foreign_listing(req.input.strip())
        if not foreign["title"]:
            # Siden blokerede hentningen (bot-beskyttelse/captcha) eller havde ingen læsbar
            # titel — fundet i praksis (mobile.de's "Zugriff verweigert"). Fejl tydeligt HER i
            # stedet for at søge videre på en meningsløs "bilnavn"-streng fra en blokeret side.
            detail_msg = (
                "Kunne ikke hente annoncen — sitet blokerede automatisk hentning (bot-beskyttelse) "
                "eller siden kunne ikke læses korrekt."
                if foreign.get("blocked")
                else "Kunne ikke finde en titel/bilnavn på siden — er linket korrekt, og er annoncen stadig aktiv?"
            )
            raise HTTPException(status_code=422, detail={
                "message": detail_msg,
                "warnings": [
                    "Prøv i stedet at indtaste bilens data manuelt (mærke, model, år, km, CO2), "
                    "eller find et link til samme bil på en anden annonce-side.",
                ],
            })
        car_name = foreign["title"] or req.input
        km = foreign["km"] or 0
        co2 = req.co2 or foreign["co2"] or 0
        if not foreign["co2"]:
            warnings.append("Kunne ikke finde CO2 på annoncen — brug dansk sammenligningsbils CO2 i stedet, jf. fast praksis.")
        # DRIVMIDDEL, tilføjet efter fund i praksis: en elbil-annonce fik korrekt fundet CO2 (0),
        # men "Drivmiddel"-feltet i UI'en stod stadig på standardværdien "Benzin", da intet
        # automatisk satte det om — hvilket giver en HELT forkert afgift (andet CO2-tillæg,
        # indfasningsprocent og ekstra bundfradrag for el/plugin-hybrid, se RATES i index.html).
        # Detekteres samme sted og på samme måde som CO2 (fra annoncens sidetekst).
        if foreign.get("fuelType"):
            fuel_type = foreign["fuelType"]
            fuel_display = foreign["fuelDisplay"]
        else:
            warnings.append(
                "Kunne ikke bestemme drivmiddel (benzin/diesel/plugin-hybrid/el) automatisk fra "
                "annoncen — tjek 'Drivmiddel' i Køretøj-sektionen er sat korrekt, ellers bliver "
                "afgiften forkert (især for el/plugin-hybrid)."
            )
        age_months = None  # kendes typisk ikke præcist fra en udenlandsk annonce alene
        target_year = None  # kendes ikke fra en udenlandsk annonce alene — årstals-sikkerhedsnettet springes derfor over
    elif req.maerke:
        # STRUKTURERET INPUT — ny, foretrukken vej fra UI'en (mærke/model/km/årgang som separate
        # felter i stedet for fritekst). Tilføjet efter gentagne fejl i praksis, hvor fritekst-
        # parseren (parse_free_text nedenfor) fejlagtigt lod ord som "kørt", løsrevne km-tal og
        # "fra"/"ny" indgå i selve søgestrengen til Bilbasen/DBA og gav falske 0-resultater, selv
        # for helt almindelige, findbare biler. Strukturerede felter fjerner hele denne fejlklasse,
        # da vi her ALDRIG skal gætte hvad der er mærke/model og hvad der er fyldord.
        car_name = f"{req.maerke.strip()} {(req.model or '').strip()}".strip()
        km = req.km or 0
        co2 = req.co2 or 0
        age_months = months_since(req.year)
        target_year = req.year
        if not req.year:
            warnings.append("Ingen årgang angivet — alder kunne ikke bestemmes automatisk, ret feltet manuelt.")
        if co2 == 0:
            warnings.append("Intet CO2-tal angivet — udfyld manuelt, ellers bliver afgiften forkert.")
    else:
        parsed = parse_free_text(req.input)
        car_name = parsed["carName"]
        km = parsed["km"] or 0
        co2 = req.co2 or 0
        age_months = months_since(parsed["year"])
        target_year = parsed["year"]
        if parsed["year"] is None:
            warnings.append("Kunne ikke finde et årstal i teksten — angiv fx 'BMW 335i 2010, kørt 175000 km'.")
        if co2 == 0:
            warnings.append("Intet CO2-tal angivet eller fundet — udfyld manuelt, ellers bliver afgiften forkert.")

    if age_months is None:
        age_months = 0
        warnings.append("Alder (måneder) kunne ikke bestemmes automatisk — ret feltet manuelt.")

    # Mærke/model-splitting til søgning. VIGTIGT, rettet efter fejl i praksis: en tidligere
    # version brugte KUN andet ord som "model" (fx kun "CLA" af "Mercedes-Benz CLA 45 AMG"),
    # hvilket mistede performance-variant-navnet og fik søgningen til at finde helt forkerte
    # (og langt billigere/dyrere) varianter af den rigtige model — fx nye el-drevne CLA250/350
    # i stedet for den benzindrevne CLA 45 AMG. Brug derfor ALLE ord efter mærket som model/
    # variant i søgningen (både Bilbasen og DBA søger nu på fritekst, ikke faste URL-stier, så
    # en længere, mere præcis søgestreng er kun en fordel — giver den for få/ingen trænger,
    # falder værktøjet automatisk tilbage til bilopslag.nu, jf. Fast metode).
    parts = car_name.split()
    maerke = parts[0] if parts else ""
    model = " ".join(parts[1:8])
    expected_body = None
    for hint in ("cabriolet", "coupe", "coupé", "touring", "stationcar", "sedan"):
        if hint in car_name.lower():
            expected_body = hint
            break

    # Kører SEKVENTIELT, ikke parallelt (asyncio.gather) — to samtidige headless Chromium-
    # instanser overbelastede Render's Free-plan (0.1 CPU / 512MB) og forårsagede timeouts/
    # genstarter i praksis. Tager længere tid samlet, men er markant mere stabilt.
    bilbasen_raw = await scraper.search_bilbasen(maerke, model, expected_body)
    dba_raw = await scraper.search_dba(f"{maerke} {model}", expected_body)

    usable = [r for r in (bilbasen_raw + dba_raw) if not r.excluded]
    excluded_count = len(bilbasen_raw) + len(dba_raw) - len(usable)
    if excluded_count:
        warnings.append(f"{excluded_count} annoncer sorteret fra (forkert karrosseri, Uden afgift, eller Engros/CVR).")

    # SIKKERHEDSNET, tilføjet efter en fejl i praksis: hvis søge-URL'en af en eller anden grund
    # rammer forkert (fx et ugyldigt mærke-slug, der får siden til at redirecte til en generel
    # "alle biler"-liste), skal vi ALDRIG stille aflevere fuldstændig urelaterede biler som
    # "sammenligninger" — det er værre end at finde for få. Kræv derfor at mærkenavnet rent
    # faktisk indgår i beskrivelsen, før en sammenligning bruges.
    if maerke:
        # Brug kun kernenavnet af mærket (fx "mercedes-benz" -> "mercedes"), da Bilbasen/DBA ofte
        # forkorter/staver mærket lidt anderledes i selve annonceteksten end det fulde navn — en
        # for striks fuld-streng-sammenligning ville ellers selv kassere ellers korrekte fund.
        maerke_core = re.split(r"[-\s]", maerke.lower())[0]
        mismatched = [r for r in usable if maerke_core not in r.beskrivelse.lower()]
        if mismatched:
            usable = [r for r in usable if maerke_core in r.beskrivelse.lower()]
            warnings.append(
                f"{len(mismatched)} fundne biler matchede ikke mærket '{maerke}' og blev kasseret "
                "som sikkerhedsforanstaltning — søgningen kan være gået forkert. Overvej at søge manuelt."
            )

    # VARIANT-sikkerhedsnet, tilføjet efter fund i praksis: mærke-matchet alene er ikke nok —
    # en søgning på "Mercedes-Benz CLA 45 AMG" gav Bilbasen-resultater der ganske rigtigt var
    # Mercedes CLA'er, men helt forkerte varianter (CLA200, CLA220d, nye el-drevne CLA250+),
    # ikke performance-udgaven "45 AMG". Hvis modellen indeholder et rent talord (typisk en
    # effekt-/variant-betegnelse som "45"), så kræv at annoncen faktisk nævner det tal — ellers
    # kasseres den. Bruger KUN rene tal (ikke fx "335i", som allerede er præcist nok i sig selv
    # og ikke skal risikere at blive kasseret pga. formateringsforskelle som "335 i").
    numeric_variant_tokens = [t for t in model.split() if t.isdigit()]
    if numeric_variant_tokens:
        variant_mismatched = [
            r for r in usable
            if not all(t in r.beskrivelse.lower() for t in numeric_variant_tokens)
        ]
        if variant_mismatched:
            usable = [
                r for r in usable
                if all(t in r.beskrivelse.lower() for t in numeric_variant_tokens)
            ]
            warnings.append(
                f"{len(variant_mismatched)} fundne biler matchede mærket, men ikke den specifikke "
                f"variant ('{' '.join(numeric_variant_tokens)}') og blev kasseret — det var en anden "
                "udgave af modellen (fx en billigere/dyrere variant). Overvej at søge manuelt hvis "
                "der er for få resultater tilbage."
            )

    # ÅRSTALS-sikkerhedsnet, tilføjet efter fund i praksis (X3-sagen): mærke/variant-match alene
    # er ikke nok til at sikre en retvisende sammenligning — DBA gav fund fra så forskellige år som
    # 2006, 2011 og 2012 for en bil af en helt anden årgang, hvilket gjorde resultatet misvisende.
    # Sammenligninger SKAL findes med årgang inden for ±1 år af brugerens bil, og op til ±2 år hvis
    # bilen er over 10 år gammel (ældre biler har færre annoncer at vælge blandt, så en lidt bredere
    # margin er nødvendig der). Springes kun over hvis vi slet ikke kender bilens egen årgang (fx et
    # udenlandsk link uden årstal) — der er intet at sammenligne op imod i så fald.
    def _listing_year(r):
        m = re.search(r"(\d{4})", r.dato or "")
        return int(m.group(1)) if m else None

    if target_year:
        year_window = 2 if (date.today().year - target_year) > 10 else 1
        year_mismatched = [
            r for r in usable
            if _listing_year(r) is None or abs(_listing_year(r) - target_year) > year_window
        ]
        if year_mismatched:
            usable = [r for r in usable if r not in year_mismatched]
            warnings.append(
                f"{len(year_mismatched)} fundne biler havde en årgang for langt fra bilens egen "
                f"({target_year}, ±{year_window} år tilladt) og blev kasseret som sikkerhedsforanstaltning — "
                "ellers ville sammenligningsgrundlaget blive misvisende. Overvej at søge manuelt hvis der "
                "er for få resultater tilbage."
            )

    # DRIVMIDDEL ved STRUKTURERET/fritekst-opslag, tilføjet efter fund i praksis: en elbil (BMW
    # iX3) blev søgt via mærke/model-felterne (ikke link), og her findes ingen enkelt annonce-
    # side at hente et "Kraftstoffart"-felt fra, som ved link-opslag ovenfor. Bilbasen/DBA's egne
    # søgeresultat-kort viser dog selv en drivmiddel-mærkat i den rå korttekst ("Plug-in", "El",
    # "Diesel", "Benzin") — brug flertallet blandt de fundne (allerede år/mærke/variant-
    # filtrerede) sammenligninger som bedste gæt, og advar hvis intet entydigt kunne bestemmes.
    if not is_link:
        counts: dict[str, int] = {}
        displays: dict[str, str] = {}
        for r in usable:
            fi = scraper.detect_fuel_from_bilbasen_dba_text(r.body_type_text or r.beskrivelse)
            if fi:
                counts[fi["fuel"]] = counts.get(fi["fuel"], 0) + 1
                displays[fi["fuel"]] = fi["display"]
        if counts:
            top_fuel = max(counts, key=counts.get)
            fuel_type = top_fuel
            fuel_display = displays[top_fuel]
            if len(counts) > 1:
                warnings.append(
                    f"Sammenligningerne pegede på flere forskellige drivmidler — brugte flertallet "
                    f"('{fuel_display}'). Tjek 'Drivmiddel' i Køretøj-sektionen er korrekt."
                )
        else:
            warnings.append(
                "Kunne ikke bestemme drivmiddel (benzin/diesel/plugin-hybrid/el) automatisk ud fra "
                "søgeresultaterne — tjek 'Drivmiddel' i Køretøj-sektionen er sat korrekt, ellers "
                "bliver afgiften forkert (især for el/plugin-hybrid)."
            )

    comparisons: list[calc.Comparison] = []
    for r in usable:
        comparisons.append(calc.Comparison(
            kilde=r.kilde, beskrivelse=r.beskrivelse, pris=r.pris, dato=r.dato, km=r.km, link=r.link,
        ))

    # Backup, jf. "Fast metode": kun hvis Bilbasen+DBA tilsammen giver færre end 4 gode matches,
    # søg supplerende på bilopslag.nu (mærke/model). Disse rækker kommer med Værdi u. afgift
    # direkte fra kilden, så de også styrker 3-metode-restværdimodellen (peer/regression), ikke
    # kun gennemsnitsprisen. Se scraper.py: søge-URL'en her er UBEKRÆFTET og kan kræve rettelse.
    if len(comparisons) < 4:
        try:
            bilopslag_extra = await scraper.search_bilopslag_nu(maerke, model)
        except Exception as e:
            bilopslag_extra = []
            warnings.append(f"bilopslag.nu-backup-søgning fejlede ({e}) — se scraper.py, søge-URL'en er ubekræftet.")
        if bilopslag_extra and target_year:
            # Samme årstals-sikkerhedsnet som for Bilbasen/DBA ovenfor — bilopslag.nu-fund skal
            # ikke undtages fra ±1/±2-års-reglen, bare fordi de kun bruges som backup.
            year_window = 2 if (date.today().year - target_year) > 10 else 1
            bilopslag_mismatched = [
                r for r in bilopslag_extra
                if _listing_year(r) is None or abs(_listing_year(r) - target_year) > year_window
            ]
            if bilopslag_mismatched:
                bilopslag_extra = [r for r in bilopslag_extra if r not in bilopslag_mismatched]
                warnings.append(
                    f"{len(bilopslag_mismatched)} bilopslag.nu-fund havde en årgang for langt fra bilens "
                    f"egen ({target_year}, ±{year_window} år tilladt) og blev kasseret."
                )
        if bilopslag_extra:
            for r in bilopslag_extra:
                comparisons.append(calc.Comparison(
                    kilde=r.kilde, beskrivelse=r.beskrivelse, pris=r.pris, dato=r.dato, km=r.km,
                    link=r.link, vaerdi_u_afgift=r.vaerdi_u_afgift, regafgift=r.regafgift,
                    opkraevet=r.opkraevet, andel_pct=r.andel_pct,
                ))
            warnings.append(f"Supplerede med {len(bilopslag_extra)} biler fra bilopslag.nu, da Bilbasen+DBA gav for få matches.")
        else:
            warnings.append(
                f"Kun {len(comparisons)} gode Bilbasen/DBA-matches fundet, og bilopslag.nu-backup gav intet "
                "brugbart — resultatet er derfor mindre sikkert. Overvej at supplere manuelt."
            )

    if not comparisons:
        raise HTTPException(status_code=422, detail={
            "message": "Ingen brugbare sammenligninger fundet — kan ikke beregne et resultat.",
            "warnings": warnings,
        })

    result = calc.full_calculation(
        comparisons, co2=co2, age_months=age_months, km_stand=km,
        fuel_type=fuel_type, months=req.months, downpct=req.downpct,
        rate=req.rate, rest_rente=req.restRente,
    )

    return {
        "car": {"carName": car_name, "co2": co2, "ageMonths": age_months, "kmStand": km, "fuelDisplay": fuel_display},
        "comparisons": [
            {
                "kilde": c.kilde, "beskrivelse": c.beskrivelse, "pris": c.pris, "dato": c.dato,
                "km": c.km, "link": c.link, "vaerdiUAfgift": c.vaerdi_u_afgift,
                "regafgift": c.regafgift, "opkraevet": c.opkraevet, "andelPct": c.andel_pct,
            }
            for c in comparisons
        ],
        "market": result["market"],
        "exAfgiftValue": round(result["exAfgiftValue"]),
        "kontantTotal": round(result["kontantTotal"]),
        "leasing": {k: (round(v) if isinstance(v, (int, float)) else v) for k, v in result["leasing"].items()},
        "warnings": warnings,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
