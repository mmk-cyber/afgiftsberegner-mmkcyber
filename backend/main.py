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
    input: str
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
    name_part = name_part.strip(" ,.-")
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

    if is_link:
        foreign = await scraper.fetch_foreign_listing(req.input.strip())
        car_name = foreign["title"] or req.input
        km = foreign["km"] or 0
        co2 = req.co2 or foreign["co2"] or 0
        if not foreign["co2"]:
            warnings.append("Kunne ikke finde CO2 på annoncen — brug dansk sammenligningsbils CO2 i stedet, jf. fast praksis.")
        age_months = None  # kendes typisk ikke præcist fra en udenlandsk annonce alene
    else:
        parsed = parse_free_text(req.input)
        car_name = parsed["carName"]
        km = parsed["km"] or 0
        co2 = req.co2 or 0
        age_months = months_since(parsed["year"])
        if parsed["year"] is None:
            warnings.append("Kunne ikke finde et årstal i teksten — angiv fx 'BMW 335i 2010, kørt 175000 km'.")
        if co2 == 0:
            warnings.append("Intet CO2-tal angivet eller fundet — udfyld manuelt, ellers bliver afgiften forkert.")

    if age_months is None:
        age_months = 0
        warnings.append("Alder (måneder) kunne ikke bestemmes automatisk — ret feltet manuelt.")

    # Meget grov mærke/model-splitting til søgning — brugeren bør kunne rette dette i UI'et,
    # før scraperen faktisk kaldes, i en rigtig version. Her et simpelt best-effort-gæt.
    parts = car_name.split()
    maerke = parts[0] if parts else ""
    model = parts[1] if len(parts) > 1 else ""
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
        mismatched = [r for r in usable if maerke.lower() not in r.beskrivelse.lower()]
        if mismatched:
            usable = [r for r in usable if maerke.lower() in r.beskrivelse.lower()]
            warnings.append(
                f"{len(mismatched)} fundne biler matchede ikke mærket '{maerke}' og blev kasseret "
                "som sikkerhedsforanstaltning — søgningen kan være gået forkert. Overvej at søge manuelt."
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
        fuel_type=req.fuel_type, months=req.months, downpct=req.downpct,
        rate=req.rate, rest_rente=req.restRente,
    )

    return {
        "car": {"carName": car_name, "co2": co2, "ageMonths": age_months, "kmStand": km},
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
