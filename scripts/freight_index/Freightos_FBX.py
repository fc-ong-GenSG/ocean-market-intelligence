import os
import re
import csv
from pathlib import Path
from decimal import Decimal
from datetime import datetime
from typing import List, Dict, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# =========================
# CONFIG
# =========================
BASE_DIR = Path(r"C:\Data\ocean_market_intelligence")
load_dotenv(BASE_DIR / "config" / ".env")

LOGIN_URL = "https://app.terminal.freightos.com/login"

OUTPUT_DIR = BASE_DIR / "output"
RAW_DIR = BASE_DIR / r"data\processed\freight_index"
MASTER_CSV_PATH = RAW_DIR / "Freight_Index_Master.csv"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

ACCOUNT_CONFIGS = {
    "ACCTNAM_FBX": {
        "indices": ["FBX01", "FBX03"],
    },
    "ACCTEURMED_FBX": {
        "indices": ["FBX11", "FBX13"],
    },
}

ROUTE_METADATA = {
    "FBX01": {"Route Code": "FBX01", "Source": "Freightos", "Group": "FBX"},
    "FBX03": {"Route Code": "FBX03", "Source": "Freightos", "Group": "FBX"},
    "FBX11": {"Route Code": "FBX11", "Source": "Freightos", "Group": "FBX"},
    "FBX13": {"Route Code": "FBX13", "Source": "Freightos", "Group": "FBX"},
}

MASTER_COLUMNS = [
    "Date",
    "Route Code",
    "Rate",
    "Source",
    "Group",
    "POL",
    "POD",
    "Bound",
    # "index_code",
    "Upload_TS",
]

# =========================
# HELPERS
# =========================
def get_account_credentials(account_code: str) -> Tuple[str, str]:
    email = os.getenv(f"{account_code}_USER_EMAIL")
    password = os.getenv(f"{account_code}_USER_PASS")

    if not email or not password:
        raise ValueError(
            f"Missing credentials for {account_code}. "
            f"Expected: {account_code}_USER_EMAIL and {account_code}_USER_PASS"
        )
    return email, password


def extract_route_title(page, index_code: str) -> str:
    """
    Read the main route title shown beside the index badge.
    Example:
    CHINA/EAST ASIA TO NORTH AMERICA WEST COAST
    """
    body_text = page.locator("body").inner_text()

    # Try to find the title from visible text near the selected index
    patterns = [
        rf"{index_code}\s+([A-Z/\s\-]+TO[A-Z/\s\-]+)",
        rf"{index_code}\s+([A-Za-z/\s\-]+to[A-Za-z/\s\-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, body_text, re.IGNORECASE)
        if match:
            title = re.sub(r"\s+", " ", match.group(1)).strip()
            return title.upper()

    # Fallback: look for a large heading-like block containing TO
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    for line in lines:
        upper_line = line.upper()
        if " TO " in upper_line and len(upper_line) > 15:
            return upper_line

    raise ValueError(f"Unable to extract route title for {index_code}")


def derive_bound(pol: str, pod: str) -> str:
    """
    Derive trade direction from POL/POD.
    This is a practical rule-based approach and can be refined later.
    """
    pol_u = pol.upper()
    pod_u = pod.upper()

    if any(x in pol_u for x in ["CHINA", "EAST ASIA", "ASIA"]) and any(x in pod_u for x in ["NORTH AMERICA", "AMERICA"]):
        return "Eastbound"

    if any(x in pol_u for x in ["CHINA", "EAST ASIA", "ASIA"]) and any(x in pod_u for x in ["EUROPE", "MEDITERRANEAN"]):
        return "Westbound"

    return "Unknown"


def parse_route_title(route_title: str) -> Dict[str, str]:
    """
    Parse route title into POL / POD / Bound.
    Cleans trailing UI text accidentally captured from the page.
    Example:
    CHINA/EAST ASIA TO NORTH AMERICA WEST COAST
    """
    title = re.sub(r"\s+", " ", route_title.strip()).upper()

    # Remove trailing UI text if accidentally captured
    noise_patterns = [
        r"\s+ADD INDEX-LINKED PRICE.*$",
        r"\s+CHART TYPE.*$",
        r"\s+LINE.*$",
        r"\s+BAR.*$",
    ]

    for pattern in noise_patterns:
        title = re.sub(pattern, "", title, flags=re.IGNORECASE).strip()

    if " TO " not in title:
        raise ValueError(f"Unexpected route title format: {route_title}")

    pol, pod = title.split(" TO ", 1)
    pol = pol.strip().title()
    pod = pod.strip().title()

    bound = derive_bound(pol, pod)

    return {
        "POL": pol,
        "POD": pod,
        "Bound": bound,
    }


def get_upload_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_price(price_text: str) -> Decimal:
    cleaned = re.sub(r"[^\d.]", "", price_text)
    if not cleaned:
        raise ValueError(f"Unable to parse price from: {price_text}")
    return Decimal(cleaned)


def normalize_date(date_text: str) -> str:
    raw = date_text.strip()

    patterns = [
        ("%Y-%m-%d", raw),
        ("%d-%b-%y", raw.title()),
        ("%d-%b-%Y", raw.title()),
        ("%d %b %Y", raw.title()),
        ("%d %m %Y", raw),
        ("%d %m %y", raw),
    ]

    for fmt, candidate in patterns:
        try:
            return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    raise ValueError(f"Unable to normalize date: {date_text}")


def format_date_for_master_csv(iso_date: str) -> str:
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d %b %Y")


def save_raw_csv(records: List[Dict], output_file: Path) -> None:
    if not records:
        return

    preferred_order = [
        "account_code",
        "index_code",
        "price_date",
        "price_value",
        "POL",
        "POD",
        "Bound",
        "raw_tooltip",
    ]
    
    discovered_keys = set()
    for row in records:
        discovered_keys.update(row.keys())

    remaining_keys = [k for k in sorted(discovered_keys) if k not in preferred_order]
    fieldnames = [k for k in preferred_order if k in discovered_keys] + remaining_keys

    with output_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    print(f"Saved raw scrape CSV: {output_file}")


def parse_tooltip_text(raw_text: str, expected_index_code: str) -> Optional[Dict]:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

    found_date = None
    found_index = None
    found_price = None

    for line in lines:
        upper_line = line.upper()

        if upper_line == expected_index_code.upper():
            found_index = expected_index_code.upper()
            continue

        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line):
            found_date = normalize_date(line)
            continue

        if re.fullmatch(r"\d{2}-[A-Za-z]{3}-\d{2,4}", line):
            found_date = normalize_date(line)
            continue

        if re.fullmatch(r"\d{2}\s+[A-Za-z]{3}\s+\d{4}", line):
            found_date = normalize_date(line)
            continue

        if re.fullmatch(r"\d{2}\s+\d{2}\s+\d{4}", line):
            found_date = normalize_date(line)
            continue

        if "$" in line or re.fullmatch(r"\d+(?:\.\d+)?", line.replace(",", "")):
            try:
                found_price = str(parse_price(line))
            except Exception:
                pass

    if found_date and found_index and found_price:
        return {
            "index_code": found_index,
            "price_date": found_date,
            "price_value": found_price,
            "raw_tooltip": raw_text
        }

    return None


def transform_scraped_records_for_master(scraped_records: List[Dict]) -> List[Dict]:
    transformed = []
    upload_ts = get_upload_timestamp()

    for rec in scraped_records:
        index_code = rec["index_code"]
        meta = ROUTE_METADATA.get(index_code, {
            "Route Code": index_code,
            "Source": "Freightos Terminal",
            "Group": "FBX",
        })

        transformed.append({
            "Date": format_date_for_master_csv(rec["price_date"]),
            "Route Code": meta["Route Code"],
            "Rate": float(rec["price_value"]),
            "Source": meta["Source"],
            "Group": meta["Group"],
            "POL": rec.get("POL"),
            "POD": rec.get("POD"),
            "Bound": rec.get("Bound"),
            # "index_code": index_code,
            "Upload_TS": upload_ts,
        })

    return transformed


def upsert_to_master_csv(new_records: List[Dict], master_csv_path: Path) -> None:
    new_df = pd.DataFrame(new_records)

    if new_df.empty:
        print("No new records to upsert into master CSV.")
        return

    for col in MASTER_COLUMNS:
        if col not in new_df.columns:
            new_df[col] = None
    new_df = new_df[MASTER_COLUMNS]

    if master_csv_path.exists():
        existing_df = pd.read_csv(master_csv_path, dtype=str)
        existing_df.columns = [str(c).strip() for c in existing_df.columns]

        missing_cols = [c for c in MASTER_COLUMNS if c not in existing_df.columns]
        if missing_cols:
            raise ValueError(
                f"Master CSV structure invalid. Missing columns: {missing_cols}. "
                f"Found: {list(existing_df.columns)}"
            )

        existing_df = existing_df[MASTER_COLUMNS]
        print(f"Loaded existing master CSV: {master_csv_path}")
        print(f"Existing rows: {len(existing_df)}")
    else:
        existing_df = pd.DataFrame(columns=MASTER_COLUMNS)
        print(f"Master CSV not found. Creating new file at: {master_csv_path}")

    existing_df["Rate"] = pd.to_numeric(existing_df["Rate"], errors="coerce")
    new_df["Rate"] = pd.to_numeric(new_df["Rate"], errors="coerce")

    combined_df = pd.concat([existing_df, new_df], ignore_index=True)
    print(f"Combined rows before dedupe: {len(combined_df)}")

    combined_df["_upload_ts_sort"] = pd.to_datetime(
        combined_df["Upload_TS"],
        format="%Y-%m-%d %H:%M:%S",
        errors="coerce"
    )

    combined_df["_date_sort"] = pd.to_datetime(
        combined_df["Date"],
        format="%d %b %y",
        errors="coerce"
    )



    combined_df = combined_df.sort_values(
        # by=["Date", "Route Code"],
        # ascending=[True, True]
        by=["Date", "Route Code", "_upload_ts_sort"],
        ascending=[True, True, True]
    )

    combined_df = combined_df.drop_duplicates(
        subset=["Date", "Route Code"],
        keep="last"
    )

    combined_df = combined_df.sort_values(
        by=["_date_sort", "Route Code"],
        ascending=[True, True]
    )

    # combined_df = combined_df.drop(columns=["_date_sort"])
    combined_df = combined_df.drop(columns=["_upload_ts_sort", "_date_sort"])
    combined_df = combined_df[MASTER_COLUMNS]

    combined_df.to_csv(master_csv_path, index=False)
    print(f"Master CSV updated successfully: {master_csv_path}")
    print(f"Final row count: {len(combined_df)}")

# =========================
# LOGIN / NAVIGATION
# =========================
def login(page, context, email: str, password: str, session_file: Path) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    body_text = page.locator("body").inner_text()

    if "LOGIN WITH YOUR" not in body_text:
        print("Login screen not detected. Navigating to login page again.")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

    email_input = page.locator(
        'input[type="email"], input[name="email"], input[autocomplete="username"]'
    ).first
    email_input.wait_for(timeout=30000)
    email_input.fill(str(email))

    continue_button = page.locator('button:has-text("CONTINUE")').first
    continue_button.wait_for(timeout=30000)
    continue_button.click()

    password_input = page.locator(
        'input[type="password"], input[name="password"], input[autocomplete="current-password"]'
    ).first
    password_input.wait_for(timeout=30000)
    password_input.fill(str(password))

    login_button = page.locator(
        'button:has-text("LOGIN"), button:has-text("LOG IN"), button:has-text("SIGN IN"), button[type="submit"]'
    ).first
    login_button.wait_for(timeout=30000)
    login_button.click()

    page.wait_for_timeout(6000)
    context.storage_state(path=str(session_file))
    print(f"Logged in. Current URL: {page.url}")
    print(f"Session saved: {session_file}")


def ensure_dashboard_loaded(page) -> None:
    page.wait_for_timeout(4000)
    body_text = page.locator("body").inner_text()

    if "FREIGHTOS TERMINAL - FBX" in body_text or "FAVORITES" in body_text or "FAVOURITES" in body_text:
        print("Dashboard appears loaded.")
    else:
        print("Warning: dashboard text not fully confirmed yet.")


def click_index_card(page, index_code: str) -> Dict[str, str]:
    print(f"Selecting {index_code}...")
    page.locator(f"text={index_code}").first.wait_for(timeout=30000)
    page.locator(f"text={index_code}").first.click()
    page.wait_for_timeout(2500)

    route_title = extract_route_title(page, index_code)
    route_meta = parse_route_title(route_title)

    print(f"{index_code} route title: {route_title}")
    print(f"{index_code} parsed route metadata: {route_meta}")

    return route_meta


def set_chart_type_to_line(page) -> None:
    if page.locator("text=Bar").count() > 0:
        page.locator("text=Bar").first.click()
        page.wait_for_timeout(700)

    line_option = page.locator("text=Line").last
    line_option.wait_for(timeout=10000)
    line_option.click()
    page.wait_for_timeout(2500)

# =========================
# EXTRACTION
# =========================
def extract_tooltip_text(page, expected_index_code: str) -> Optional[str]:
    tooltip_selectors = [
        "[role='tooltip']",
        ".tooltip",
        ".recharts-tooltip-wrapper",
        ".highcharts-tooltip",
        "div"
    ]

    for selector in tooltip_selectors:
        locator = page.locator(selector)
        count = locator.count()

        for i in range(count):
            try:
                text = locator.nth(i).inner_text(timeout=500).strip()
                if expected_index_code in text and (
                    re.search(r"\d{4}-\d{2}-\d{2}", text)
                    or re.search(r"\d{2}-[A-Za-z]{3}-\d{2,4}", text)
                    or re.search(r"\d{2}\s+[A-Za-z]{3}\s+\d{4}", text)
                    or re.search(r"\d{2}\s+\d{2}\s+\d{4}", text)
                ):
                    return text
            except Exception:
                pass

    return None


def find_line_points(page):
    selectors = [
        "svg circle",
        ".recharts-dot",
        ".highcharts-point",
        "svg [class*='dot']",
        "svg [class*='point']"
    ]

    for selector in selectors:
        locator = page.locator(selector)
        count = locator.count()
        print(f"Point selector '{selector}' -> {count}")
        if count > 0:
            return locator

    raise RuntimeError("No line-chart point candidates found.")


def extract_index_data_from_line_chart(page, index_code: str, account_code: str, route_meta: Dict[str, str]) -> List[Dict]:
    records = []
    seen = set()

    point_locator = find_line_points(page)
    total = point_locator.count()
    print(f"Total point candidates for {index_code}: {total}")

    for i in range(total):
        try:
            point = point_locator.nth(i)
            box = point.bounding_box()

            if not box:
                continue
            if box["width"] < 4 or box["height"] < 4:
                continue

            point.hover(force=True)
            page.wait_for_timeout(900)

            tooltip_text = extract_tooltip_text(page, index_code)
            if not tooltip_text:
                continue

            parsed = parse_tooltip_text(tooltip_text, index_code)
            if not parsed:
                continue

            parsed["account_code"] = account_code
            parsed["POL"] = route_meta["POL"]
            parsed["POD"] = route_meta["POD"]
            parsed["Bound"] = route_meta["Bound"]

            key = (
                parsed["account_code"],
                parsed["index_code"],
                parsed["price_date"],
                parsed["price_value"]
            )
            if key not in seen:
                seen.add(key)
                records.append(parsed)
                print(f"Captured: {parsed}")

        except Exception as e:
            print(f"{index_code}: skipped point #{i} due to error: {e}")

    records.sort(key=lambda x: x["price_date"])
    return records

# =========================
# ACCOUNT RUNNER
# =========================
def run_account(account_code: str, indices: List[str]) -> List[Dict]:
    print(f"\n{'=' * 20} RUNNING {account_code} {'=' * 20}")

    email, password = get_account_credentials(account_code)

    # Separate profile/session per account to avoid cross-account contamination
    user_data_dir = BASE_DIR / f"playwright_profile_{account_code}"
    session_file = BASE_DIR / f"freightos_session_{account_code}.json"
    user_data_dir.mkdir(parents=True, exist_ok=True)

    account_records: List[Dict] = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(user_data_dir),
            channel="chrome",
            headless=False,
            slow_mo=500,
            viewport={"width": 1600, "height": 900},
        )

        page = context.pages[0] if context.pages else context.new_page()

        try:
            login(page, context, email, password, session_file)
            ensure_dashboard_loaded(page)

            for index_code in indices:
                print(f"\n--- Extracting {index_code} for {account_code} ---")
                route_meta = click_index_card(page, index_code)
                set_chart_type_to_line(page)

                records = extract_index_data_from_line_chart(page, index_code, account_code, route_meta)

                if not records:
                    print(f"Warning: no records captured for {index_code} under {account_code}")
                else:
                    print(f"{index_code}: captured {len(records)} rows under {account_code}")
                    account_records.extend(records)

        except PlaywrightTimeoutError as e:
            print(f"Timeout in {account_code}: {e}")
            try:
                page.screenshot(path=str(OUTPUT_DIR / f"timeout_error_{account_code}.png"), full_page=True)
            except Exception:
                pass
        except Exception as e:
            print(f"Error in {account_code}: {e}")
            try:
                page.screenshot(path=str(OUTPUT_DIR / f"general_error_{account_code}.png"), full_page=True)
            except Exception:
                pass
        finally:
            # This closes the account session, effectively ending account 1 before account 2 starts
            context.close()
            print(f"{account_code} browser/session closed.")

    return account_records

# =========================
# MAIN
# =========================
def main():
    all_scraped_records: List[Dict] = []

    for account_code, cfg in ACCOUNT_CONFIGS.items():
        account_records = run_account(account_code, cfg["indices"])

        if account_records:
            raw_csv_path = OUTPUT_DIR / f"freightos_raw_{account_code}.csv"
            save_raw_csv(account_records, raw_csv_path)
            all_scraped_records.extend(account_records)

    if all_scraped_records:
        raw_all_path = OUTPUT_DIR / "freightos_raw_all_accounts.csv"
        save_raw_csv(all_scraped_records, raw_all_path)

        master_records = transform_scraped_records_for_master(all_scraped_records)
        upsert_to_master_csv(master_records, MASTER_CSV_PATH)
    else:
        print("No records scraped from any account. Master CSV not updated.")

    print("Task completed. All browsers closed.")

if __name__ == "__main__":
    main()