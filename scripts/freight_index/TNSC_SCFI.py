import re
import io
import csv
import sys
import traceback
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import pdfplumber


# =========================================================
# CONFIG
# =========================================================
BASE_URL = "https://tnsc.com"
LISTING_URL = "https://tnsc.com/blog/freightrate2026"

MASTER_CSV_PATH = r"C:\Data\ocean_market_intelligence\data\processed\freight_index\Freight_Index_Master.csv"
RAW_DUMP_DIR = r"C:\Data\ocean_market_intelligence\data\temp\freight_index"
LOG_DIR = r"C:\Data\ocean_market_intelligence\data\logs"

REQUEST_TIMEOUT = 60
SOURCE_NAME = "TNSC"
GROUP_NAME = "SCFI"

# Keep this aligned with the shared master file contract
MASTER_HEADERS = [
    "Date",
    "Route Code",
    "Rate",
    "Source",
    "Group",
    "POL",
    "POD",
    "Bound",
    "Upload_TS",
]

# =========================================================
# ROUTE MAP
# =========================================================
# NOTE:
# - Comprehensive Index is intentionally omitted.
# - Europe & Mediterranean are multiplied by 2 (USD/TEU -> aligned handling).
SCFI_ROUTE_MAP = {
    "Europe (Base port)": {
        "route_code": "SCFI-SHA-RTM",
        "pol": "Shanghai",
        "pod": "Rotterdam",
        "bound": "Eastbound",
        "multiplier": 2,
    },
    "Mediterranean (Base port)": {
        "route_code": "SCFI-SHA-GOA",
        "pol": "Shanghai",
        "pod": "Genoa",
        "bound": "Eastbound",
        "multiplier": 2,
    },
    "USWC (Base port)": {
        "route_code": "SCFI-SHA-LAX",
        "pol": "Shanghai",
        "pod": "Los Angeles",
        "bound": "Eastbound",
        "multiplier": 1,
    },
    "USEC (Base port)": {
        "route_code": "SCFI-SHA-NYC",
        "pol": "Shanghai",
        "pod": "New York",
        "bound": "Eastbound",
        "multiplier": 1,
    },
    "Persian Gulf & Red Sea (Dubai)": {
        "route_code": "SCFI-SHA-DXB",
        "pol": "Shanghai",
        "pod": "Dubai",
        "bound": "Westbound",
        "multiplier": 1,
    },
    "Australia/New Zealand (Melbourne)": {
        "route_code": "SCFI-SHA-MEL",
        "pol": "Shanghai",
        "pod": "Melbourne",
        "bound": "Southbound",
        "multiplier": 1,
    },
    "East/West Africa (Lagos)": {
        "route_code": "SCFI-SHA-LOS",
        "pol": "Shanghai",
        "pod": "Lagos",
        "bound": "Westbound",
        "multiplier": 1,
    },
    "South Africa (Durban)": {
        "route_code": "SCFI-SHA-DUR",
        "pol": "Shanghai",
        "pod": "Durban",
        "bound": "Westbound",
        "multiplier": 1,
    },
    "South America (Santos)": {
        "route_code": "SCFI-SHA-SSZ",
        "pol": "Shanghai",
        "pod": "Santos",
        "bound": "Westbound",
        "multiplier": 1,
    },
    "West Japan (Base port)": {
        "route_code": "SCFI-SHA-WJP",
        "pol": "Shanghai",
        "pod": "West Japan",
        "bound": "Eastbound",
        "multiplier": 1,
    },
    "East Japan (Base port)": {
        "route_code": "SCFI-SHA-EJP",
        "pol": "Shanghai",
        "pod": "East Japan",
        "bound": "Eastbound",
        "multiplier": 1,
    },
    "Southeast Asia (Singapore)": {
        "route_code": "SCFI-SHA-SIN",
        "pol": "Shanghai",
        "pod": "Singapore",
        "bound": "Southbound",
        "multiplier": 1,
    },
    "Korea (Pusan)": {
        "route_code": "SCFI-SHA-BUS",
        "pol": "Shanghai",
        "pod": "Busan",
        "bound": "Eastbound",
        "multiplier": 1,
    },
    "America West Coast (Manzanillo)": {
        "route_code": "SCFI-SHA-ZLO",
        "pol": "Shanghai",
        "pod": "Manzanillo",
        "bound": "Eastbound",
        "multiplier": 1,
    },
    "East Africa (Mombasa)": {
        "route_code": "SCFI-SHA-MBA",
        "pol": "Shanghai",
        "pod": "Mombasa",
        "bound": "Westbound",
        "multiplier": 1,
    },
}


# =========================================================
# HELPERS
# =========================================================
def ensure_dirs():
    Path(RAW_DUMP_DIR).mkdir(parents=True, exist_ok=True)
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    Path(MASTER_CSV_PATH).parent.mkdir(parents=True, exist_ok=True)


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def log_message(message: str):
    log_path = Path(LOG_DIR) / "scfi_tnsc_log.txt"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{now_ts()}] {message}\n")


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def normalize_date(date_str: str) -> str:
    """
    Convert:
      13 March 2026 -> 13 Mar 2026
      6 March 2026  -> 06 Mar 2026
    """
    date_str = normalize_whitespace(date_str)
    dt = datetime.strptime(date_str, "%d %B %Y")
    return dt.strftime("%d %b %Y")


def parse_upload_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def safe_get(url: str, timeout: int = REQUEST_TIMEOUT) -> requests.Response:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


# =========================================================
# STEP 1: DETECT LATEST WEEKLY PDF
# =========================================================
def get_latest_week_article_or_pdf():
    """
    Detect latest weekly freight movement link from the listing page.

    Accepts patterns like:
    - Weekly freight rate movements for week 11/2026.
    - ประจำสัปดาห์ที่ 11/2569
    """
    resp = safe_get(LISTING_URL)
    soup = BeautifulSoup(resp.text, "html.parser")

    candidates = []

    for a in soup.find_all("a", href=True):
        text = normalize_whitespace(a.get_text(" ", strip=True))
        href = a["href"].strip()

        # English
        m_en = re.search(
            r"weekly freight rate movements for week\s*(\d+)\s*/\s*2026",
            text,
            flags=re.IGNORECASE,
        )

        # Thai Buddhist year equivalent often used on page
        m_th = re.search(
            r"สัปดาห์ที่\s*(\d+)\s*/\s*2569",
            text,
            flags=re.IGNORECASE,
        )

        match = m_en or m_th
        if not match:
            continue

        week_no = int(match.group(1))
        full_url = href if href.startswith("http") else BASE_URL + href
        candidates.append(
            {
                "week_no": week_no,
                "link_text": text,
                "url": full_url,
            }
        )

    if not candidates:
        raise RuntimeError("No weekly freight-rate links found on TNSC listing page.")

    latest = max(candidates, key=lambda x: x["week_no"])
    return latest


def resolve_pdf_url(item: dict) -> str:
    """
    The listing may link directly to a PDF or to an article page containing the PDF.
    """
    url = item["url"]

    if url.lower().endswith(".pdf"):
        return url

    resp = safe_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")

    pdf_candidates = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ".pdf" in href.lower():
            pdf_url = href if href.startswith("http") else BASE_URL + href
            pdf_candidates.append(pdf_url)

    if not pdf_candidates:
        raise RuntimeError(f"No PDF link found inside latest article page: {url}")

    return pdf_candidates[0]


# =========================================================
# STEP 2: FIND SCFI PAGE IN PDF
# =========================================================
def extract_scfi_page_text(pdf_bytes: bytes) -> tuple[int, str]:
    """
    Return (page_number_1_based, page_text)
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if "Shanghai Containerized Freight Index (SCFI)" in text:
                return i + 1, text

    raise RuntimeError("SCFI section not found in PDF.")


def dump_raw_text(text: str, page_number: int):
    dump_path = Path(RAW_DUMP_DIR) / f"scfi_tnsc_raw_page_{page_number}_{run_stamp()}.txt"
    with open(dump_path, "w", encoding="utf-8") as f:
        f.write(text)
    return str(dump_path)


# =========================================================
# STEP 3: PARSE SCFI TABLE
# =========================================================
def find_scfi_dates(page_text: str, lines: list[str]) -> tuple[str, str]:
    """
    Robustly detect SCFI Previous Index / Current Index dates from PDF text.

    Handles layouts like:
    1) Previous Index
       6 March 2026
       Current Index
       13 March 2026

    2) Previous Index Current Index
       6 March 2026 13 March 2026

    3) Previous Index 6 March 2026 Current Index 13 March 2026
    """

    # Normalize whitespace for whole-page regex checks
    flat_text = " ".join(page_text.split())

    date_pattern = r"\d{1,2}\s+[A-Za-z]+\s+\d{4}"

    # -------------------------------------------------
    # Pattern A:
    # Previous Index 6 March 2026 Current Index 13 March 2026
    # -------------------------------------------------
    m = re.search(
        rf"Previous\s+Index\s+({date_pattern})\s+Current\s+Index\s+({date_pattern})",
        flat_text,
        flags=re.IGNORECASE,
    )
    if m:
        return normalize_date(m.group(1)), normalize_date(m.group(2))

    # -------------------------------------------------
    # Pattern B:
    # Previous Index Current Index 6 March 2026 13 March 2026
    # -------------------------------------------------
    m = re.search(
        rf"Previous\s+Index\s+Current\s+Index\s+({date_pattern})\s+({date_pattern})",
        flat_text,
        flags=re.IGNORECASE,
    )
    if m:
        return normalize_date(m.group(1)), normalize_date(m.group(2))

    # -------------------------------------------------
    # Pattern C:
    # line-by-line fallback
    # -------------------------------------------------
    prev_date_raw = None
    curr_date_raw = None

    for i, line in enumerate(lines):
        line_norm = " ".join(line.split())

        if re.fullmatch(r"Previous\s+Index", line_norm, flags=re.IGNORECASE):
            if i + 1 < len(lines):
                next_line = " ".join(lines[i + 1].split())
                if re.fullmatch(date_pattern, next_line, flags=re.IGNORECASE):
                    prev_date_raw = next_line

        if re.fullmatch(r"Current\s+Index", line_norm, flags=re.IGNORECASE):
            if i + 1 < len(lines):
                next_line = " ".join(lines[i + 1].split())
                if re.fullmatch(date_pattern, next_line, flags=re.IGNORECASE):
                    curr_date_raw = next_line

    if prev_date_raw and curr_date_raw:
        return normalize_date(prev_date_raw), normalize_date(curr_date_raw)

    # -------------------------------------------------
    # Pattern D:
    # find first 2 dates after both labels appear
    # -------------------------------------------------
    if re.search(r"Previous\s+Index", flat_text, flags=re.IGNORECASE) and re.search(
        r"Current\s+Index", flat_text, flags=re.IGNORECASE
    ):
        all_dates = re.findall(date_pattern, flat_text, flags=re.IGNORECASE)
        if len(all_dates) >= 2:
            return normalize_date(all_dates[0]), normalize_date(all_dates[1])

    raise RuntimeError("Could not detect Previous Index / Current Index dates.")


def parse_scfi_rows(page_text: str) -> list[dict]:
    """
    Build master CSV rows:
    - one row for Previous Index
    - one row for Current Index
    """
    lines = [normalize_whitespace(x) for x in page_text.splitlines() if x.strip()]
    previous_date, current_date = find_scfi_dates(page_text, lines)
    upload_ts = now_ts()

    # Matches:
    # Europe (Base port) 26.1% 1452 1618
    # Korea (Pusan) 13 14
    row_pattern = re.compile(
        r"^(?P<desc>.+?)\s+(?:(?P<weight>\d+(?:\.\d+)?%)\s+)?(?P<prev>\d+(?:\.\d+)?)\s+(?P<curr>\d+(?:\.\d+)?)$"
    )

    output_rows = []
    matched_descriptions = set()

    for line in lines:
        m = row_pattern.match(line)
        if not m:
            continue

        desc = m.group("desc").strip()

        if desc not in SCFI_ROUTE_MAP:
            continue

        matched_descriptions.add(desc)
        cfg = SCFI_ROUTE_MAP[desc]

        previous_rate = float(m.group("prev")) * cfg["multiplier"]
        current_rate = float(m.group("curr")) * cfg["multiplier"]

        # Previous Index row
        output_rows.append({
            "Date": previous_date,
            "Route Code": cfg["route_code"],
            "Rate": f"{previous_rate:.2f}",
            "Source": SOURCE_NAME,
            "Group": GROUP_NAME,
            "POL": cfg["pol"],
            "POD": cfg["pod"],
            "Bound": cfg["bound"],
            "Upload_TS": upload_ts,
        })

        # Current Index row
        output_rows.append({
            "Date": current_date,
            "Route Code": cfg["route_code"],
            "Rate": f"{current_rate:.2f}",
            "Source": SOURCE_NAME,
            "Group": GROUP_NAME,
            "POL": cfg["pol"],
            "POD": cfg["pod"],
            "Bound": cfg["bound"],
            "Upload_TS": upload_ts,
        })

    if not output_rows:
        raise RuntimeError("No SCFI route rows were parsed from the PDF page.")

    missing = sorted(set(SCFI_ROUTE_MAP.keys()) - matched_descriptions)
    if missing:
        log_message(f"WARNING: Missing expected SCFI rows: {missing}")

    return output_rows


# =========================================================
# STEP 4: UPSERT INTO MASTER CSV
# =========================================================
def read_existing_master_rows(master_csv_path: str) -> list[dict]:
    path = Path(master_csv_path)
    if not path.exists():
        return []

    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # Light header sanity check
    existing_headers = reader.fieldnames or []
    missing_headers = [h for h in MASTER_HEADERS if h not in existing_headers]
    if missing_headers:
        raise RuntimeError(
            f"Master CSV header mismatch. Missing expected headers: {missing_headers}"
        )

    return rows


def business_key(row: dict) -> tuple[str, str, str, str]:
    return (
        row["Date"],
        row["Route Code"],
        row["Source"],
        row["Group"],
    )


def upsert_master_csv(master_csv_path: str, new_rows: list[dict]) -> dict:
    """
    Rule:
    - new record -> append
    - duplicate key -> overwrite existing if Upload_TS is newer

    Key:
    (Date, Route Code, Source, Group)
    """
    existing_rows = read_existing_master_rows(master_csv_path)
    old_count = len(existing_rows)

    best_by_key = {}

    # Load old rows first
    for row in existing_rows:
        # Backward safety if Upload_TS blank
        if not row.get("Upload_TS"):
            row["Upload_TS"] = "1900-01-01 00:00:00"
        best_by_key[business_key(row)] = row

    inserted = 0
    overwritten = 0
    skipped_older = 0

    # Merge incoming rows
    for row in new_rows:
        key = business_key(row)

        if key not in best_by_key:
            best_by_key[key] = row
            inserted += 1
        else:
            old_row = best_by_key[key]
            old_ts = parse_upload_ts(old_row["Upload_TS"])
            new_ts = parse_upload_ts(row["Upload_TS"])

            if new_ts >= old_ts:
                best_by_key[key] = row
                overwritten += 1
            else:
                skipped_older += 1

    final_rows = list(best_by_key.values())

    # Clean sort
    def sort_key(r):
        try:
            dt = datetime.strptime(r["Date"], "%d %b %Y")
        except Exception:
            dt = datetime(1900, 1, 1)
        return (dt, r["Source"], r["Group"], r["Route Code"])

    final_rows.sort(key=sort_key)

    with open(master_csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=MASTER_HEADERS)
        writer.writeheader()
        writer.writerows(final_rows)

    return {
        "old_count": old_count,
        "incoming_count": len(new_rows),
        "final_count": len(final_rows),
        "inserted": inserted,
        "overwritten": overwritten,
        "skipped_older": skipped_older,
    }


# =========================================================
# MAIN
# =========================================================
def main():
    ensure_dirs()
    log_message("========== SCFI TNSC RUN START ==========")

    latest_item = get_latest_week_article_or_pdf()
    log_message(
        f"Latest weekly link detected | week={latest_item['week_no']} | "
        f"text={latest_item['link_text']} | url={latest_item['url']}"
    )

    pdf_url = resolve_pdf_url(latest_item)
    log_message(f"Resolved PDF URL: {pdf_url}")

    pdf_resp = safe_get(pdf_url)
    pdf_bytes = pdf_resp.content
    log_message(f"PDF downloaded successfully | size_bytes={len(pdf_bytes)}")

    scfi_page_number, scfi_page_text = extract_scfi_page_text(pdf_bytes)
    log_message(f"SCFI section found on PDF page {scfi_page_number}")

    raw_dump_path = dump_raw_text(scfi_page_text, scfi_page_number)
    log_message(f"Raw SCFI page dump saved to: {raw_dump_path}")

    new_rows = parse_scfi_rows(scfi_page_text)
    log_message(f"Parsed SCFI output rows: {len(new_rows)}")

    result = upsert_master_csv(MASTER_CSV_PATH, new_rows)
    log_message(
        "Master CSV merge complete | "
        f"old_count={result['old_count']} | "
        f"incoming_count={result['incoming_count']} | "
        f"final_count={result['final_count']} | "
        f"inserted={result['inserted']} | "
        f"overwritten={result['overwritten']} | "
        f"skipped_older={result['skipped_older']}"
    )

    print("SCFI TNSC extraction completed successfully.")
    print(f"Latest week detected : {latest_item['week_no']}")
    print(f"Resolved PDF URL     : {pdf_url}")
    print(f"SCFI page number     : {scfi_page_number}")
    print(f"Rows parsed          : {len(new_rows)}")
    print(f"Rows inserted        : {result['inserted']}")
    print(f"Rows overwritten     : {result['overwritten']}")
    print(f"Final master rows    : {result['final_count']}")
    print(f"Raw dump             : {raw_dump_path}")

    log_message("========== SCFI TNSC RUN END ==========")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_message("ERROR: " + str(e))
        log_message(traceback.format_exc())
        print("SCFI TNSC extraction failed.")
        print(str(e))
        traceback.print_exc()
        sys.exit(1)