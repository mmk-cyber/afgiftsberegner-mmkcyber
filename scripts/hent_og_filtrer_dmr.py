"""
Hent nyeste DMR-statistikudtraek og byg en soegbar SQLite-database med
praktisk talt alle relevante felter pr. koeretoej.

Koerer automatisk via GitHub Actions (dagligt), men tjekker FOeRST om der
overhovedet er en ny fil siden sidste koersel - hvis ikke, springes den
tunge download+parsing over. Det betyder vaerktoejet reelt opdaterer sig
selv, lige naar DMR laegger en ny fil op, uden at spilde tid paa dage uden
nye data.

Virker ogsaa uaendret lokalt: python3 scripts/hent_og_filtrer_dmr.py
Miljoevariabel FORCE_RUN=1 tvinger en koersel selvom filen er uaendret.
Miljoevariabel TEST_LIMIT begraenser antal parsede koeretoejer til hurtig test.
"""

import zipfile
import xml.etree.ElementTree as ET
import sqlite3
import os
import sys
import time
from ftplib import FTP

HOST = "5.44.137.84"
USER = "dmr-ftp-user"
PASS = "dmrpassword"
REMOTE_DIR = "ESStatistikListeModtag"
LOCAL_ZIP = "udtraek.zip"
XML_ENTRY = "ESStatistikListeModtag.xml"

OUTPUT_DB = "data/koeretoejer.db"
LAST_PROCESSED_FILE = "data/last_processed.txt"

NS = "{http://skat.dk/dmr/2007/05/31/}"

TEST_LIMIT = int(os.environ.get("TEST_LIMIT") or 0) or None
FORCE_RUN = os.environ.get("FORCE_RUN", "") in ("1", "true", "True")

def connect():
    ftp = FTP(HOST, timeout=60)
    ftp.login(USER, PASS)
    return ftp


def pick_newest_file(ftp):
    lines = []
    ftp.retrlines(f"LIST {REMOTE_DIR}", lines.append)
    names = [line.split()[-1] for line in lines if line.strip().endswith(".zip")]
    names.sort()
    return names[-1] if names else None


def get_last_processed():
    if os.path.exists(LAST_PROCESSED_FILE):
        with open(LAST_PROCESSED_FILE, encoding="utf-8") as f:
            return f.read().strip()
    return None


def set_last_processed(name):
    os.makedirs(os.path.dirname(LAST_PROCESSED_FILE), exist_ok=True)
    with open(LAST_PROCESSED_FILE, "w", encoding="utf-8") as f:
        f.write(name)


def download_with_resume(remote_name):
    remote_path = f"{REMOTE_DIR}/{remote_name}"
    attempts = 0
    while True:
        attempts += 1
        try:
            ftp = connect()
            total_size = ftp.size(remote_path)
            local_size = os.path.getsize(LOCAL_ZIP) if os.path.exists(LOCAL_ZIP) else 0
            if local_size >= total_size:
                ftp.quit()
                return
            mode = "ab" if local_size > 0 else "wb"
            print(f"[Forsoeg {attempts}] Downloader fra byte {local_size:,} af {total_size:,} ...", flush=True)
            with open(LOCAL_ZIP, mode) as f:
                def callback(chunk):
                    f.write(chunk)
                ftp.retrbinary(f"RETR {remote_path}", callback, blocksize=1024 * 1024,
                                rest=local_size if local_size > 0 else None)
            ftp.quit()
            if os.path.getsize(LOCAL_ZIP) >= total_size:
                return
        except Exception as e:
            print(f"Afbrudt: {e}. Venter 15 sek og proever igen ...", flush=True)
            time.sleep(15)


def txt(elem, tagname):
    found = elem.find(f".//{NS}{tagname}")
    return found.text if found is not None and found.text else None


def direct_txt(elem, tagname):
    found = elem.find(f"{NS}{tagname}")
    return found.text if found is not None and found.text else None


def scoped_txt(elem, struktur_tag, field_tag):
    struktur = elem.find(f".//{NS}{struktur_tag}")
    if struktur is None:
        return None
    found = struktur.find(f"{NS}{field_tag}")
    return found.text if found is not None and found.text else None


def udstyr_liste(elem):
    navne = [n.text for n in elem.iter(f"{NS}KoeretoejUdstyrTypeNavn") if n.text]
    return "; ".join(navne) if navne else None


FIELDS = [
    "RegNr", "StelNr", "KoeretoejIdent", "KoeretoejArt", "Anvendelse",
    "Maerke", "Model", "Variant", "TypeBetegnelse",
    "OprettetUdFra", "Status", "StatusDato", "FoersteRegDato",
    "TotalVaegt", "KoereklarVaegtMinimum", "TekniskTotalVaegt", "VogntogVaegt",
    "AkselAntal", "TraekkendeAksler", "SiddepladserMinimum", "AntalDoere",
    "TilkoblingMulighed", "TilkoblingsvaegtUdenBremser", "TilkoblingsvaegtMedBremser",
    "FaelgDaek", "NCAPTest", "Koeretoejstand", "EgnetTilTaxi",
    "TypeGodkendelseNummer", "Trafikskade",
    "KarrosseriType", "Farve", "NormType",
    "CylinderAntal", "SlagVolumen", "StoersteEffekt", "MotorMaerkning",
    "StandStoej", "KoerselStoej", "InnovativTeknik", "MotorKilometerstand",
    "DrivkraftType", "MaaleNorm", "CO2Udslip", "KmPerLiter", "ElForbrug",
    "PartikelFilter", "EmissionCO", "EmissionHCPlusNOX", "EmissionNOX",
    "SynsType", "SynsDato", "SynsResultat", "SynStatus", "SynStatusDato",
    "SynsKilometerstand",
    "RegistreringStatus", "RegistreringStatusDato",
    "Udstyr",
]


def extract_row(elem):
    return {
        "RegNr": direct_txt(elem, "RegistreringNummerNummer"),
        "StelNr": txt(elem, "KoeretoejOplysningStelNummer"),
        "KoeretoejIdent": direct_txt(elem, "KoeretoejIdent"),
        "KoeretoejArt": direct_txt(elem, "KoeretoejArtNavn"),
        "Anvendelse": txt(elem, "KoeretoejAnvendelseNavn"),
        "Maerke": txt(elem, "KoeretoejMaerkeTypeNavn"),
        "Model": txt(elem, "KoeretoejModelTypeNavn"),
        "Variant": txt(elem, "KoeretoejVariantTypeNavn"),
        "TypeBetegnelse": txt(elem, "KoeretoejTypeTypeNavn"),
        "OprettetUdFra": txt(elem, "KoeretoejOplysningOprettetUdFra"),
        "Status": txt(elem, "KoeretoejOplysningStatus"),
        "StatusDato": txt(elem, "KoeretoejOplysningStatusDato"),
        "FoersteRegDato": txt(elem, "KoeretoejOplysningFoersteRegistreringDato"),
        "TotalVaegt": txt(elem, "KoeretoejOplysningTotalVaegt"),
        "KoereklarVaegtMinimum": txt(elem, "KoeretoejOplysningKoereklarVaegtMinimum"),
        "TekniskTotalVaegt": txt(elem, "KoeretoejOplysningTekniskTotalVaegt"),
        "VogntogVaegt": txt(elem, "KoeretoejOplysningVogntogVaegt"),
        "AkselAntal": txt(elem, "KoeretoejOplysningAkselAntal"),
        "TraekkendeAksler": txt(elem, "KoeretoejOplysningTraekkendeAksler"),
        "SiddepladserMinimum": txt(elem, "KoeretoejOplysningSiddepladserMinimum"),
        "AntalDoere": txt(elem, "KoeretoejOplysningAntalDoere"),
        "TilkoblingMulighed": txt(elem, "KoeretoejOplysningTilkoblingMulighed"),
        "TilkoblingsvaegtUdenBremser": txt(elem, "KoeretoejOplysningTilkoblingsvaegtUdenBremser"),
        "TilkoblingsvaegtMedBremser": txt(elem, "KoeretoejOplysningTilkoblingsvaegtMedBremser"),
        "FaelgDaek": txt(elem, "KoeretoejOplysningFaelgDaek"),
        "NCAPTest": txt(elem, "KoeretoejOplysningNCAPTest"),
        "Koeretoejstand": txt(elem, "KoeretoejOplysningKoeretoejstand"),
        "EgnetTilTaxi": txt(elem, "KoeretoejOplysningEgnetTilTaxi"),
        "TypeGodkendelseNummer": txt(elem, "KoeretoejOplysningTypeGodkendelseNummer"),
        "Trafikskade": txt(elem, "KoeretoejOplysningTrafikskade"),
        "KarrosseriType": txt(elem, "KarrosseriTypeNavn"),
        "Farve": txt(elem, "FarveTypeNavn"),
        "NormType": txt(elem, "NormTypeNavn"),
        "CylinderAntal": txt(elem, "KoeretoejMotorCylinderAntal"),
        "SlagVolumen": txt(elem, "KoeretoejMotorSlagVolumen"),
        "StoersteEffekt": txt(elem, "KoeretoejMotorStoersteEffekt"),
        "MotorMaerkning": txt(elem, "KoeretoejMotorMaerkning"),
        "StandStoej": txt(elem, "KoeretoejMotorStandStoej"),
        "KoerselStoej": txt(elem, "KoeretoejMotorKoerselStoej"),
        "InnovativTeknik": txt(elem, "KoeretoejMotorInnovativTeknik"),
        "MotorKilometerstand": scoped_txt(elem, "KoeretoejMotorStruktur", "KoeretoejMotorKilometerstand"),
        "DrivkraftType": txt(elem, "DrivkraftTypeNavn"),
        "MaaleNorm": txt(elem, "KoeretoejMotorMaaleNormTypeNavn"),
        "CO2Udslip": txt(elem, "KoeretoejMiljoeOplysningCO2Udslip"),
        "KmPerLiter": txt(elem, "KoeretoejMotorKmPerLiter"),
        "ElForbrug": txt(elem, "KoeretoejMotorElektriskForbrug"),
        "PartikelFilter": txt(elem, "KoeretoejMiljoeOplysningPartikelFilter"),
        "EmissionCO": txt(elem, "KoeretoejMiljoeOplysningEmissionCO"),
        "EmissionHCPlusNOX": txt(elem, "KoeretoejMiljoeOplysningEmissionHCPlusNOX"),
        "EmissionNOX": txt(elem, "KoeretoejMiljoeOplysningEmissionNOX"),
        "SynsType": txt(elem, "SynResultatSynsType"),
        "SynsDato": txt(elem, "SynResultatSynsDato"),
        "SynsResultat": txt(elem, "SynResultatSynsResultat"),
        "SynStatus": txt(elem, "SynResultatSynStatus"),
        "SynStatusDato": txt(elem, "SynResultatSynStatusDato"),
        "SynsKilometerstand": scoped_txt(elem, "SynResultatStruktur", "KoeretoejMotorKilometerstand"),
        "RegistreringStatus": direct_txt(elem, "KoeretoejRegistreringStatus"),
        "RegistreringStatusDato": direct_txt(elem, "KoeretoejRegistreringStatusDato"),
        "Udstyr": udstyr_liste(elem),
    }


def build_database(count_limit=None):
    os.makedirs(os.path.dirname(OUTPUT_DB), exist_ok=True)
    if os.path.exists(OUTPUT_DB):
        os.remove(OUTPUT_DB)

    conn = sqlite3.connect(OUTPUT_DB)
    cur = conn.cursor()
    cols_sql = ", ".join(f'"{c}" TEXT' for c in FIELDS)
    cur.execute(f'CREATE TABLE koeretoejer ({cols_sql})')

    insert_sql = f'INSERT INTO koeretoejer ({", ".join(FIELDS)}) VALUES ({", ".join("?" for _ in FIELDS)})'

    count = 0
    start = time.time()
    batch = []

    with zipfile.ZipFile(LOCAL_ZIP) as z:
        with z.open(XML_ENTRY) as f:
            context = ET.iterparse(f, events=("end",))
            for event, elem in context:
                if elem.tag == NS + "Statistik":
                    row = extract_row(elem)
                    batch.append(tuple(row[c] for c in FIELDS))
                    count += 1
                    elem.clear()

                    if len(batch) >= 5000:
                        cur.executemany(insert_sql, batch)
                        batch.clear()

                    if count % 100_000 == 0:
                        print(f"{count:,} koeretoejer behandlet ({(time.time()-start)/60:.1f} min) ...", flush=True)

                    if count_limit and count >= count_limit:
                        break

    if batch:
        cur.executemany(insert_sql, batch)

    print("Bygger indekser ...", flush=True)
    cur.execute('CREATE INDEX idx_regnr ON koeretoejer(RegNr)')
    cur.execute('CREATE INDEX idx_stelnr ON koeretoejer(StelNr)')
    cur.execute('CREATE INDEX idx_maerke_model ON koeretoejer(Maerke, Model)')
    conn.commit()
    conn.close()

    return count


def main():
    print("Finder nyeste fil paa FTP-serveren ...", flush=True)
    ftp = connect()
    newest = pick_newest_file(ftp)
    ftp.quit()
    if not newest:
        print("Fandt ingen zip-fil - stopper.", flush=True)
        sys.exit(1)
    print(f"Nyeste fil paa serveren: {newest}", flush=True)

    last = get_last_processed()
    if last == newest and not FORCE_RUN:
        print(f"Ingen ny fil siden sidst ({last}) - springer download og parsing over.", flush=True)
        return

    if TEST_LIMIT:
        print(f"*** TEST-KOeRSEL: stopper efter {TEST_LIMIT:,} koeretoejer ***", flush=True)

    download_with_resume(newest)
    print("Download faerdig. Bygger SQLite-database (streaming, ingen fuld udpakning) ...", flush=True)

    count = build_database(count_limit=TEST_LIMIT)

    if os.path.exists(LOCAL_ZIP):
        os.remove(LOCAL_ZIP)

    set_last_processed(newest)

    print(f"\nFaerdig! {count:,} koeretoejer skrevet til {OUTPUT_DB}.", flush=True)


if __name__ == "__main__":
    main()
