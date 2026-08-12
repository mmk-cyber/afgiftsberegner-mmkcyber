# Afgiftsberegner — automatisk backend (udkast)

Automatiserer det, som Claude ellers har gjort manuelt via browser hele denne sags-serie:
søger Bilbasen + DBA efter totalpriser, sorterer "Uden afgift"/"Engros/CVR"-annoncer fra,
supplerer med bilopslag.nu, og beregner registreringsafgift + §3b-flexleasing med samme
matematik som selve værktøjet (`calc.py` er en 1:1-portering af index.html's JS, verificeret
mod to tidligere rigtige sager i denne samtale — begge matcher nu på kronen).

## Vigtigt at vide, før du stoler på tallene

Dette er skrevet **uden mulighed for at teste mod de rigtige sider** — mit udviklingsmiljø har
ikke netværksadgang til bilbasen.dk, dba.dk eller bilopslag.nu. Det betyder konkret:

- **Scraper-selectorerne i `scraper.py` er bedste bud**, baseret på hvad der er observeret
  manuelt via browser i tidligere dele af denne samtale. De kan sagtens være forkerte eller
  gå i stykker, og skal testes/rettes, når koden faktisk kører et sted med netværksadgang.
- **Automatiseringen kan IKKE fuldt ud erstatte de judgment-calls**, jeg har lavet manuelt
  denne session (fx at opdage "Uden afgift" kun nævnt i brødteksten, ikke i søgekortet, eller
  at afgøre om en "Cabriolet"-mærket bil faktisk er en stationcar). Koden forsøger at
  genskabe reglerne, men er ikke lige så pålidelig som et menneske (eller Claude) der læser
  hver annonce igennem.
- **bilopslag.nu-koblingen til konkrete Bilbasen/DBA-annoncer findes stadig ikke** — der er
  ingen offentlig sti fra en Bilbasen-annonce til dens stelnummer, så "Værdi u. afgift" på de
  Bilbasen/DBA-hentede rækker vil ofte stå tomt. Til gengæld søger `search_bilopslag_nu()` nu
  automatisk på mærke/model som BACKUP, når Bilbasen+DBA giver færre end 4 gode matches —
  disse rækker har Værdi u. afgift direkte fra kilden, så de styrker restværdi-modellen. **Men:**
  søge-URL'en til bilopslag.nu's "avanceret søgning" i `scraper.py` er et ubekræftet gæt (jeg
  har ikke kunnet teste den) — kun enkelt-bil-opslaget (`/nummerplade/<plade>`) er testet og
  virker pålideligt. Forvent at skulle rette `candidate_urls`-listen i `search_bilopslag_nu()`.
- Konklusion: **vis altid sammenligningerne og lad brugeren tjekke dem**, før tallet bruges til
  en rigtig beslutning. Se det som et førsteudkast, ikke et facit.

## Lokal test

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --reload
# POST http://localhost:8000/beregn  { "input": "BMW 335i Cabriolet 2010, kørt 175000 km" }
```

## Deployment (Render.com, gratis/billig tier)

Valgt fordi det er den simpleste vej fra "kode i et repo" til "kørende server" uden at
skulle administrere en VPS selv. Cloudflare Workers/Vercel er fravalgt — deres IP-ranges er
kendte scraper-IP'er og blokeres oftere af sider som Bilbasen (se tidligere diskussion).

1. Opret en gratis konto på **render.com** (kan logges ind med GitHub).
2. Push denne mappe (`afgiftsberegner-backend/`) til et nyt GitHub-repo — gerne privat, ligesom
   selve afgiftsberegneren.
3. På Render: **New + → Web Service** → vælg dit repo.
4. Render finder automatisk `Dockerfile` og bygger derudfra — ingen yderligere opsætning nødvendig.
5. Vælg gratis/"Starter"-planen. **Bemærk:** headless Chromium er RAM-tung — gratis-planens
   512 MB kan vise sig for lidt i praksis. Hvis serveren crasher/timer ud under scraping, er
   næste skridt at opgradere til en betalt Render-plan (~7 USD/md for mere RAM), ikke at
   ændre koden.
6. Når den er deployet, får du en URL som `https://afgiftsberegner-backend.onrender.com`.
   Denne skal indsættes i `index.html`'s nye "Automatisk opslag"-felt (se separat opdatering).

## Kendte begrænsninger / næste skridt

- Selector-verificering mod de rigtige sider (kræver nogen med netværksadgang til at teste —
  kør lokalt på din egen maskine, eller bed mig teste via Claude i Chrome og rapportere fejl).
- bilopslag.nu-matching til specifikke Bilbasen/DBA-biler (kræver enten at annoncerne selv
  viser reg.nr/stelnummer, eller et løsere km/årgang-baseret gæt).
- Rate-limiting/pauser mellem opslag, så Bilbasen/DBA ikke oplever unormalt mange hurtige
  opslag fra samme IP — ikke implementeret endnu, bør tilføjes før reel brug.
- Body-type-filteret (`WRONG_BODY_HINTS` i `scraper.py`) er en grov heuristik — de tidligere
  fejl i denne sags-serie (fx "DX91 = Cabriolet, ikke Coupé" for BMW M3) viste at kun at kigge
  på ord i overskriften ikke altid er nok; en mere robust løsning ville læse et egentligt
  "Stamdata → TYPE"-felt, hvis Bilbasen/DBA viser det strukturet et sted i DOM'en.
