"""
Hent nyeste DMR-statistikudtræk og filtrer det ned til et kompakt CSV.
Beregnet til at køre automatisk via GitHub Actions - men virker også
uændret lokalt (kør bare: python3 scripts/hent_og_filtrer_dmr.py).

Miljøvariabel TEST_LIMIT (valgfri) begrænser antal køretøjer for hurtig test,
fx TEST_LIMIT=1000. Sæt den ikke (eller til 0) for en fuld kørsel.
"""

import zipfile
import xml.etree.ElementTree as ET
import csv
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
OUTPUT_CSV = "data/koeretoejer_filtreret.csv"
NS = "{http://skat.dk/dmr/2007/05/31/}"

TEST_LIMIT = int(os.environ.get("TEST_LIMIT", "0")) or None

FIELDS = [
    "RegNr", "StelNr", "KoeretoejArt", "Maerke", "Model", "Variant",
    "FoersteRegDato", "KarrosseriType", "DrivkraftType", "MaaleNorm",
    "CO2Udslip", "KmPerLiter", "ElForbrug", "Kilometerstand", "SynsDato",
]


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
            print(f"[Forsøg {attempts}] Downloader fra byte {local_size:,} af {total_size:,} ...")
            with open(LOCAL_ZIP, mode) as f:
                def callback(chunk):
                    f.write(chunk)
                ftp.retrbinary(f"RETR {remote_path}", callback, blocksize=1024 * 1024,
                                rest=local_size if local_size > 0 else None)
            ftp.quit()
            if os.path.getsize(LOCAL_ZIP) >= total_size:
                return
        except Exception as e:
            print(f"Afbrudt: {e}. Venter 15 sek og prøver igen ...")
            time.sleep(15)


def txt(elem, tagname):
    found = elem.find(f".//{NS}{tagname}")
    return found.text if found is not None and found.text else ""


def direct_txt(elem, tagname):
    found = elem.find(f"{NS}{tagname}")
    return found.text if found is not None and found.text else ""


def extract_row(elem):
    return {
        "RegNr": direct_txt(elem, "RegistreringNummerNummer"),
        "StelNr": txt(elem, "KoeretoejOplysningStelNummer"),
        "KoeretoejArt": direct_txt(elem, "KoeretoejArtNavn"),
        "Maerke": txt(elem, "KoeretoejMaerkeTypeNavn"),
        "Model": txt(elem, "KoeretoejModelTypeNavn"),
        "Variant": txt(elem, "KoeretoejVariantTypeNavn"),
        "FoersteRegDato": txt(elem, "KoeretoejOplysningFoersteRegistreringDato"),
        "KarrosseriType": txt(elem, "KarrosseriTypeNavn"),
        "DrivkraftType": txt(elem, "DrivkraftTypeNavn"),
        "MaaleNorm": txt(elem, "KoeretoejMotorMaaleNormTypeNavn"),
        "CO2Udslip": txt(elem, "KoeretoejMiljoeOplysningCO2Udslip"),
        "KmPerLiter": txt(elem, "KoeretoejMotorKmPerLiter"),
        "ElForbrug": txt(elem, "KoeretoejMotorElektriskForbrug"),
        "Kilometerstand": txt(elem, "KoeretoejMotorKilometerstand"),
        "SynsDato": txt(elem, "SynResultatSynsDato"),
    }


def main():
    if TEST_LIMIT:
        print(f"*** TEST-KØRSEL: stopper efter {TEST_LIMIT:,} køretøjer ***")

    print("Finder nyeste fil på FTP-serveren ...")
    ftp = connect()
    newest = pick_newest_file(ftp)
    ftp.quit()
    if not newest:
        print("Fandt ingen zip-fil - stopper.")
        sys.exit(1)
    print(f"Nyeste fil: {newest}")

    download_with_resume(newest)
    print("Download færdig. Starter parsing (streaming, ingen fuld udpakning) ...")

    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)

    count = 0
    start = time.time()
    with zipfile.ZipFile(LOCAL_ZIP) as z:
        with z.open(XML_ENTRY) as f:
            with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as out:
                writer = csv.DictWriter(out, fieldnames=FIELDS)
                writer.writeheader()
                context = ET.iterparse(f, events=("end",))
                for event, elem in context:
                    if elem.tag == NS + "Statistik":
                        writer.writerow(extract_row(elem))
                        count += 1
                        elem.clear()
                        if count % 100_000 == 0:
                            print(f"{count:,} køretøjer behandlet ({(time.time()-start)/60:.1f} min) ...")
                        if TEST_LIMIT and count >= TEST_LIMIT:
                            break

    # Fjern den store zip - den skal IKKE committes til repoet
    if os.path.exists(LOCAL_ZIP):
        os.remove(LOCAL_ZIP)

    print(f"\nFærdig! {count:,} køretøjer skrevet til {OUTPUT_CSV}.")


if __name__ == "__main__":
    main()
