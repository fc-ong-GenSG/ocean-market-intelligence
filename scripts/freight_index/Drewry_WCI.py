import csv
import re
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional

import pandas as pd
from dateutil import parser as dtparser
from PIL import Image, ImageOps, ImageFilter
import pytesseract
from playwright.sync_api import sync_playwright


# =========================================================
# CONFIG
# =========================================================
CANVA_TABLE_URL = (
    "https://www.canva.com/design/DAGyvxw2Quo/tzawOfen77xRO0uFQF1zUQ/view"
    "?utm_content=DAGyvxw2Quo&utm_campaign=designshare&utm_medium=link2"
    "&utm_source=uniquelinks&utlId=h808c0e40c0"
)

MASTER_CSV = Path(r"C:\Data\ocean_market_intelligence\data\processed\freight_index\Freight_Index_Master.csv")
TEMP_DIR = Path(r"C:\Data\ocean_market_intelligence\data\temp\freight_index")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

SOURCE = "Drewry"
GROUP = "WCI"

TESSERACT_PATH = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")

EXPECTED_COLUMNS = [
    "Date", "Route Code", "Rate", "Source", "Group",
    "POL", "POD", "Bound", "Upload_TS"
]

ROUTE_NAME_TO_CODE = {
    "WCI Composite Index": "WCI-COMPOSITE",
    "Shanghai - Rotterdam": "WCI-SHA-RTM",
    "Rotterdam - Shanghai": "WCI-RTM-SHA",
    "Shanghai - Genoa": "WCI-SHA-GOA",
    "Shanghai - Los Angeles": "WCI-SHA-LAX",
    "Los Angeles - Shanghai": "WCI-LAX-SHA",
    "Shanghai - New York": "WCI-SHA-NYC",
    "New York - Rotterdam": "WCI-NYC-RTM",
    "Rotterdam - New York": "WCI-RTM-NYC",
}

KNOWN_ROUTES = list(ROUTE_NAME_TO_CODE.keys())


# =========================================================
# BASIC HELPERS
# =========================================================
def now_upload_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalise_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_money_to_int(value: str) -> Optional[int]:
    if value is None:
        return None
    v = str(value).replace("$", "").replace(",", "").strip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return None


def parse_date_label_to_display(label: str) -> Optional[str]:
    """
    Example:
      05 Mar 2026 -> 05 Mar 2026
      5 Mar 2026  -> 05 Mar 2026
      12 Mar 2026 -> 12 Mar 2026
    """
    label = normalise_spaces(label)
    try:
        dt = dtparser.parse(label, dayfirst=True, fuzzy=False)
        return dt.strftime("%d %b %Y")
    except Exception:
        return None


def most_recent_thursday(ref_dt: datetime) -> str:
    """
    Drewry WCI table in your process is expected to show the latest Thursday week.
    Thursday weekday = 3
    """
    days_back = (ref_dt.weekday() - 3) % 7
    dt = ref_dt - timedelta(days=days_back)
    return dt.strftime("%d %b %Y")


def derive_pol_pod_bound(route_name: str) -> Tuple[str, str, str]:
    if route_name == "WCI Composite Index":
        return "", "", ""

    if " - " not in route_name:
        return "", "", "Unknown"

    pol, pod = [x.strip() for x in route_name.split(" - ", 1)]

    if pol == "Shanghai":
        bound = "Eastbound"
    elif pod == "Shanghai":
        bound = "Westbound"
    elif pol == "New York" and pod == "Rotterdam":
        bound = "Eastbound"
    elif pol == "Rotterdam" and pod == "New York":
        bound = "Westbound"
    else:
        bound = "Unknown"

    return pol, pod, bound


def save_debug_image(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def ocr_with_configs(img: Image.Image, configs: list[str]) -> str:
    texts = []
    for cfg in configs:
        try:
            text = pytesseract.image_to_string(img, config=cfg)
            if text and text.strip():
                texts.append(text)
        except Exception:
            pass
    return "\n".join(texts)


def extract_top_header_image(image_path: Path) -> Image.Image:
    """
    Crop upper part of screenshot where date headers usually appear.
    """
    img = Image.open(image_path)
    w, h = img.size
    crop = img.crop((0, 0, w, int(h * 0.28)))
    return crop


def extract_full_preprocessed_image(image_path: Path) -> Image.Image:
    img = Image.open(image_path)
    return preprocess_image_for_ocr(img)


# =========================================================
# MASTER CSV FUNCTIONS
# =========================================================
def ensure_master_file_exists(csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    if not csv_path.exists():
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=EXPECTED_COLUMNS)
            writer.writeheader()


def load_existing_records(csv_path: Path) -> List[Dict[str, str]]:
    if not csv_path.exists():
        return []

    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    for row in rows:
        for col in EXPECTED_COLUMNS:
            if col not in row:
                row[col] = ""

    return rows


def deduplicate_keep_latest(records: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Business key:
      Date + Route Code + Source

    Keep latest Upload_TS.
    """
    best: Dict[Tuple[str, str, str], Dict[str, str]] = {}

    for row in records:
        key = (row["Date"], row["Route Code"], row["Source"])

        ts_raw = (row.get("Upload_TS") or "").strip()
        try:
            ts_val = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            ts_val = datetime.min

        if key not in best:
            best[key] = row
        else:
            old_ts_raw = (best[key].get("Upload_TS") or "").strip()
            try:
                old_ts_val = datetime.strptime(old_ts_raw, "%Y-%m-%d %H:%M:%S")
            except Exception:
                old_ts_val = datetime.min

            if ts_val >= old_ts_val:
                best[key] = row

    final_records = list(best.values())
    final_records.sort(key=lambda x: (x["Date"], x["Route Code"], x["Source"]))
    return final_records


def write_master_file(csv_path: Path, records: List[Dict[str, str]]) -> None:
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=EXPECTED_COLUMNS)
        writer.writeheader()
        writer.writerows(records)


# =========================================================
# PLAYWRIGHT SCREENSHOT
# =========================================================
def screenshot_canva_table(canva_url: str, out_path: Path, max_retries: int = 3) -> None:
    """
    Opens Canva public view in real Chrome and captures a screenshot.
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    channel="chrome",
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )

                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1800, "height": 1400},
                    locale="en-GB",
                    ignore_https_errors=True,
                )

                page = context.new_page()
                page.set_default_navigation_timeout(120000)
                page.set_default_timeout(30000)

                page.goto(canva_url, wait_until="commit", timeout=120000)
                page.wait_for_timeout(10000)

                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    pass

                for sel in [
                    "button:has-text('Accept')",
                    "button:has-text('Allow all')",
                    "button:has-text('Got it')",
                    "button:has-text('Close')",
                    "button:has-text('Dismiss')",
                ]:
                    try:
                        locator = page.locator(sel)
                        if locator.count() > 0:
                            locator.first.click(timeout=1500)
                            page.wait_for_timeout(1000)
                    except Exception:
                        pass

                try:
                    page.keyboard.press("Control+-")
                    page.wait_for_timeout(1000)
                    page.keyboard.press("Control+-")
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

                page.wait_for_timeout(5000)
                page.screenshot(path=str(out_path), full_page=True)

                context.close()
                browser.close()
                return

        except Exception as e:
            last_error = e
            print(f"[Attempt {attempt}/{max_retries}] Canva screenshot failed: {e}")

            try:
                fail_path = TEMP_DIR / f"canva_fail_attempt_{attempt}.png"
                if 'page' in locals():
                    page.screenshot(path=str(fail_path), full_page=True)
                    print(f"Failure screenshot saved: {fail_path}")
            except Exception:
                pass

            if attempt < max_retries:
                time.sleep(8)

    raise RuntimeError(f"Failed to capture Canva table after {max_retries} attempts: {last_error}")


# =========================================================
# OCR
# =========================================================
def validate_tesseract() -> None:
    if not TESSERACT_PATH.exists():
        raise FileNotFoundError(f"Tesseract executable not found at: {TESSERACT_PATH}")
    pytesseract.pytesseract.tesseract_cmd = str(TESSERACT_PATH)


def preprocess_image_for_ocr(img: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray)
    gray = gray.filter(ImageFilter.SHARPEN)
    scale = 3
    resized = gray.resize((gray.width * scale, gray.height * scale))
    return resized


def ocr_text_from_image(image_path: Path) -> Tuple[str, str]:
    """
    Returns:
      header_text: OCR from header crop only
      full_text: OCR from full screenshot
    """
    validate_tesseract()

    full_img = extract_full_preprocessed_image(image_path)
    full_debug_path = TEMP_DIR / "drewry_wci_preprocessed_full.png"
    save_debug_image(full_img, full_debug_path)

    full_text = ocr_with_configs(
        full_img,
        configs=[
            "--oem 3 --psm 6",
            "--oem 3 --psm 11",
            "--oem 3 --psm 12",
        ]
    )

    header_img = extract_top_header_image(image_path)
    header_img = preprocess_image_for_ocr(header_img)
    header_debug_path = TEMP_DIR / "drewry_wci_preprocessed_header.png"
    save_debug_image(header_img, header_debug_path)

    header_text = ocr_with_configs(
        header_img,
        configs=[
            "--oem 3 --psm 6",
            "--oem 3 --psm 11",
            "--oem 3 --psm 12",
            "--oem 3 --psm 7",
        ]
    )

    return header_text, full_text


# =========================================================
# OCR PARSING
# =========================================================
def extract_date_headers(header_ocr_text: str) -> List[str]:
    """
    Extract dates from HEADER OCR ONLY.

    Accept variants:
      05 Mar 2026
      5 Mar 2026
      05 Mar
      5 Mar
    """
    text = normalise_spaces(header_ocr_text)

    patterns = [
        r"\b(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})\b",
        r"\b(\d{1,2}\s+[A-Z][a-z]{2})\b",
    ]

    found = []
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            m = normalise_spaces(m)
            if m not in found:
                found.append(m)

    current_year = datetime.now().year
    fixed = []
    for item in found:
        if re.fullmatch(r"\d{1,2}\s+[A-Z][a-z]{2}", item):
            item = f"{item} {current_year}"
        fixed.append(item)

    cleaned = []
    for item in fixed:
        display = parse_date_label_to_display(item)
        if display:
            cleaned.append(display)

    # dedupe preserving order
    result = []
    seen = set()
    for x in cleaned:
        if x not in seen:
            seen.add(x)
            result.append(x)

    return result


def validate_latest_expected_date(iso_dates: List[str], ref_dt: Optional[datetime] = None) -> None:
    ref_dt = ref_dt or datetime.now()
    expected_latest = most_recent_thursday(ref_dt)

    if expected_latest not in iso_dates:
        raise RuntimeError(
            "Latest Drewry week not detected from HEADER OCR. "
            f"Expected latest visible date: {expected_latest}. "
            f"Detected header dates: {iso_dates}"
        )


def split_lines(ocr_text: str) -> List[str]:
    return [normalise_spaces(x) for x in ocr_text.splitlines() if normalise_spaces(x)]


def canonicalize_route_text(text: str) -> str:
    """
    Normalize OCR route text so variants like:
      Shanghai- Rotterdam
      Shanghai -Rotterdam
      shanghai - Rotterdam
      Rotterdam -New York
    all become comparable.
    """
    text = normalise_spaces(text).lower()

    # normalize dashes/spaces around dashes
    text = re.sub(r"\s*-\s*", " - ", text)

    # collapse repeated spaces again
    text = normalise_spaces(text)
    return text


def get_route_aliases() -> Dict[str, List[str]]:
    """
    Route aliases to tolerate OCR spacing/case quirks.
    """
    return {
        "WCI Composite Index": [
            "wci composite index",
        ],
        "Shanghai - Rotterdam": [
            "shanghai - rotterdam",
            "shanghai- rotterdam",
            "shanghai -rotterdam",
            "shanghai-rotterdam",
        ],
        "Rotterdam - Shanghai": [
            "rotterdam - shanghai",
            "rotterdam- shanghai",
            "rotterdam -shanghai",
            "rotterdam-shanghai",
        ],
        "Shanghai - Genoa": [
            "shanghai - genoa",
            "shanghai- genoa",
            "shanghai -genoa",
            "shanghai-genoa",
        ],
        "Shanghai - Los Angeles": [
            "shanghai - los angeles",
            "shanghai- los angeles",
            "shanghai -los angeles",
            "shanghai-los angeles",
            "shanghai-losangeles",
        ],
        "Los Angeles - Shanghai": [
            "los angeles - shanghai",
            "los angeles- shanghai",
            "los angeles -shanghai",
            "los angeles-shanghai",
        ],
        "Shanghai - New York": [
            "shanghai - new york",
            "shanghai- new york",
            "shanghai -new york",
            "shanghai-new york",
            "shanghai-newyork",
        ],
        "New York - Rotterdam": [
            "new york - rotterdam",
            "new york- rotterdam",
            "new york -rotterdam",
            "new york-rotterdam",
        ],
        "Rotterdam - New York": [
            "rotterdam - new york",
            "rotterdam- new york",
            "rotterdam -new york",
            "rotterdam-new york",
        ],
    }


def fuzzy_find_route_line_blocks(lines: List[str]) -> List[Tuple[str, List[str]]]:
    """
    Find each route in FULL OCR using normalized aliases and grab a wider block.
    """
    aliases_map = get_route_aliases()
    norm_lines = [canonicalize_route_text(x) for x in lines]

    blocks = []

    for route in KNOWN_ROUTES:
        aliases = aliases_map.get(route, [canonicalize_route_text(route)])
        found = False

        for i, norm_line in enumerate(norm_lines):
            if any(alias in norm_line for alias in aliases):
                # wider capture window to include values even if OCR split oddly
                start = max(0, i)
                end = min(len(lines), i + 6)
                block = lines[start:end]
                blocks.append((route, block))
                found = True
                break

        if not found:
            print(f"Route not found in OCR: {route}")

    return blocks


def extract_numbers_from_block(block_lines: List[str]) -> List[int]:
    text = " ".join(block_lines)
    nums = re.findall(r"\$?\s?(\d{1,3}(?:,\d{3})+|\d+)", text)

    values = []
    for n in nums:
        n2 = clean_money_to_int(n)
        if n2 is not None:
            values.append(n2)
    return values


def build_records_from_ocr(header_ocr_text: str, full_ocr_text: str) -> List[Dict[str, str]]:
    lines = split_lines(full_ocr_text)

    print("\n===== HEADER OCR PREVIEW START =====")
    print(header_ocr_text[:1500])
    print("===== HEADER OCR PREVIEW END =====\n")

    print("\n===== FULL OCR PREVIEW START =====")
    print(full_ocr_text[:3000])
    print("===== FULL OCR PREVIEW END =====\n")

    iso_dates = extract_date_headers(header_ocr_text)
    print("Detected header dates:", iso_dates)

    if not iso_dates:
        raise RuntimeError("Could not detect any week date headers from HEADER OCR.")

    validate_latest_expected_date(iso_dates)

    route_blocks = fuzzy_find_route_line_blocks(lines)
    if not route_blocks:
        raise RuntimeError("Could not identify route rows from FULL OCR text.")

    print("\nDetected route blocks:")
    for route_name, block in route_blocks:
        print(f"\n--- {route_name} ---")
        for b in block:
            print(b)

    upload_ts = now_upload_ts()
    records: List[Dict[str, str]] = []

    for route_name, block in route_blocks:
        if route_name == "WCI Composite Index":
            continue

        route_code = ROUTE_NAME_TO_CODE[route_name]
        pol, pod, bound = derive_pol_pod_bound(route_name)

        nums = extract_numbers_from_block(block)
        money_like = [n for n in nums if n >= 100]

        print(f"\nRoute: {route_name}")
        print(f"Block numbers: {nums}")
        print(f"Money-like values: {money_like}")

        if len(money_like) < len(iso_dates):
            print(f"Skipped route due to insufficient values: {route_name} -> {money_like}")
            continue

        # take first 3 visible date values: 12 Mar / 19 Mar / 26 Mar
        rates = money_like[:len(iso_dates)]

        for dt_iso, rate in zip(iso_dates, rates):
            records.append({
                "Date": dt_iso,
                "Route Code": route_code,
                "Rate": str(rate),
                "Source": SOURCE,
                "Group": GROUP,
                "POL": pol,
                "POD": pod,
                "Bound": bound,
                "Upload_TS": upload_ts,
            })

    return records


# =========================================================
# VALIDATION
# =========================================================
def validate_records(records: List[Dict[str, str]]) -> List[Dict[str, str]]:
    clean = []
    seen = set()

    for r in records:
        key = (r["Date"], r["Route Code"])
        rate = clean_money_to_int(r["Rate"])

        if not r["Date"] or not r["Route Code"] or rate is None:
            continue

        if key in seen:
            continue

        seen.add(key)
        clean.append(r)

    return clean


def assert_minimum_expected_coverage(records: List[Dict[str, str]], min_rows_per_date: int = 7) -> None:
    counts: Dict[str, int] = {}

    for r in records:
        counts[r["Date"]] = counts.get(r["Date"], 0) + 1

    bad_dates = {k: v for k, v in counts.items() if v < min_rows_per_date}
    if bad_dates:
        raise RuntimeError(f"Extraction incomplete. Low row coverage by date: {bad_dates}")

    print("Coverage by date:", counts)


# =========================================================
# MAIN
# =========================================================
def main():
    print("Step 1: Capture Canva table screenshot...")
    screenshot_path = TEMP_DIR / f"drewry_wci_table_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    screenshot_canva_table(CANVA_TABLE_URL, screenshot_path)
    print(f"Screenshot saved: {screenshot_path}")

    print("Step 2: OCR screenshot...")
    header_text, full_text = ocr_text_from_image(screenshot_path)

    header_dump = TEMP_DIR / "drewry_wci_header_ocr_dump.txt"
    full_dump = TEMP_DIR / "drewry_wci_full_ocr_dump.txt"
    header_dump.write_text(header_text, encoding="utf-8")
    full_dump.write_text(full_text, encoding="utf-8")
    print(f"Header OCR dump saved: {header_dump}")
    print(f"Full OCR dump saved: {full_dump}")

    print("Step 3: Build records from OCR...")
    new_records = build_records_from_ocr(header_text, full_text)
    new_records = validate_records(new_records)

    if not new_records:
        raise RuntimeError("No valid Drewry records extracted.")

    assert_minimum_expected_coverage(new_records, min_rows_per_date=8)

    print(f"New records extracted: {len(new_records)}")
    print(pd.DataFrame(new_records))

    print("Step 4: Merge into master CSV...")
    ensure_master_file_exists(MASTER_CSV)
    existing_records = load_existing_records(MASTER_CSV)

    combined = existing_records + new_records
    final_records = deduplicate_keep_latest(combined)

    write_master_file(MASTER_CSV, final_records)

    print(f"Existing records: {len(existing_records)}")
    print(f"Final records written: {len(final_records)}")
    print(f"Master updated: {MASTER_CSV}")

    print("Saved debug images:")
    print(TEMP_DIR / "drewry_wci_preprocessed_full.png")
    print(TEMP_DIR / "drewry_wci_preprocessed_header.png")


if __name__ == "__main__":
    main()