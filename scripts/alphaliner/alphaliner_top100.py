import re
import time
from pathlib import Path
from datetime import datetime

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


URL = "https://alphaliner.axsmarine.com/PublicTop100/"

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = REPO_ROOT / "data" / "processed" / "alphaliner"
BASE_DIR.mkdir(parents=True, exist_ok=True)

SNAPSHOT_CSV = BASE_DIR / "alphaliner_top100_snapshot.csv"
COMPARISON_CSV = BASE_DIR / "alphaliner_top100_comparison.csv"


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def parse_int(value):
    if value is None:
        return pd.NA
    text = clean_text(value).replace(",", "")
    text = re.sub(r"[^\d\-]", "", text)
    if text in ("", "-"):
        return pd.NA
    try:
        return int(text)
    except Exception:
        return pd.NA


def get_monday_date(dt: datetime):
    return (pd.Timestamp(dt) - pd.Timedelta(days=pd.Timestamp(dt).weekday())).date()


def get_iso_week_no(d):
    return int(pd.Timestamp(d).isocalendar().week)


def assign_alliance(operator: str) -> str:
    op = clean_text(operator)

    if op in {
        "CMA CGM Group",
        "COSCO Group",
        "Evergreen Line",
        "Evergreen Marine Corp.",
        "OOCL",
    }:
        return "Ocean Alliance"

    if op in {
        "ONE (Ocean Network Express)",
        "HMM Co Ltd",
        "Yang Ming Marine Transport Corp.",
    }:
        return "Premier Alliance"

    return ""


def start_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1800,2500")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=options)


def extract_global_figures_from_body_text(body_text: str) -> tuple[int, int]:
    text = body_text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    ships_match = re.search(r"([\d,]+)\s+active ships", text, flags=re.I)
    teu_matches = re.findall(r"([\d,]+)\s+TEU\b", text, flags=re.I)

    if not ships_match:
        raise ValueError("Could not detect global figure: active ships")

    if not teu_matches:
        raise ValueError("Could not detect global figure: TEU")

    active_ships = parse_int(ships_match.group(1))
    teu_values = [parse_int(x) for x in teu_matches]
    teu_values = [x for x in teu_values if pd.notna(x)]

    if not teu_values:
        raise ValueError("Could not detect global figure: TEU")

    total_teu = max(teu_values)
    return int(total_teu), int(active_ships)


def extract_second_table_with_selenium(driver) -> pd.DataFrame:
    # Wait for tables to appear
    WebDriverWait(driver, 20).until(
        lambda d: len(d.find_elements(By.TAG_NAME, "table")) >= 2
    )

    tables = driver.find_elements(By.TAG_NAME, "table")
    if len(tables) < 2:
        raise ValueError("Less than 2 tables found after rendering")

    table = tables[1]   # second table only
    rows = table.find_elements(By.TAG_NAME, "tr")

    extracted = []
    for row in rows:
        cells = row.find_elements(By.XPATH, "./th|./td")
        values = [clean_text(c.text) for c in cells]
        if values:
            extracted.append(values)

    if not extracted:
        raise ValueError("Second table is empty")

    # Build rows from the visible body, not from fragile HTML headers
    data_rows = []
    for vals in extracted:
        # Expected useful body row:
        # Rank, Operator,
        # Total TEU, Total Ships,
        # Owned TEU, Owned Ships,
        # Chartered TEU, Chartered Ships, %Chart,
        # Orderbook TEU, Orderbook Ships, %existing
        if len(vals) >= 12 and re.fullmatch(r"\d+", vals[0]):
            data_rows.append(vals[:12])

    if not data_rows:
        raise ValueError("Could not detect operator rows in second table")

    df = pd.DataFrame(
        data_rows,
        columns=[
            "Rank",
            "Operator",
            "Total_TEU",
            "Total_Ships",
            "Owned_TEU",
            "Owned_Ships",
            "Chartered_TEU",
            "Chartered_Ships",
            "Pct_Chart",
            "Orderbook_TEU",
            "Orderbook_Ships",
            "Pct_Existing",
        ],
    )

    df = df.drop(columns=["Pct_Chart", "Pct_Existing"])

    for col in [
        "Rank",
        "Total_TEU",
        "Total_Ships",
        "Owned_TEU",
        "Owned_Ships",
        "Chartered_TEU",
        "Chartered_Ships",
        "Orderbook_TEU",
        "Orderbook_Ships",
    ]:
        df[col] = df[col].apply(parse_int)

    df["Operator"] = df["Operator"].apply(clean_text)

    return df.reset_index(drop=True)


def append_others_row(df: pd.DataFrame, global_teu: int, global_ships: int, snapshot_date, week_no: int):
    top100_teu = int(df["Total_TEU"].fillna(0).sum())
    top100_ships = int(df["Total_Ships"].fillna(0).sum())

    others = {
        "Snapshot_Date": snapshot_date,
        "Week No": week_no,
        "Rank": pd.NA,
        "Operator": "Others / Outside Top 100",
        "Alliance": "",
        "Total_TEU": global_teu - top100_teu,
        "Total_Ships": global_ships - top100_ships,
        "Owned_TEU": pd.NA,
        "Owned_Ships": pd.NA,
        "Chartered_TEU": pd.NA,
        "Chartered_Ships": pd.NA,
        "Orderbook_TEU": pd.NA,
        "Orderbook_Ships": pd.NA,
    }

    return pd.concat([df, pd.DataFrame([others])], ignore_index=True)


def save_snapshot(snapshot_df: pd.DataFrame, snapshot_csv: Path) -> pd.DataFrame:
    if snapshot_csv.exists():
        existing = pd.read_csv(snapshot_csv)
        existing["Snapshot_Date"] = pd.to_datetime(existing["Snapshot_Date"]).dt.date
    else:
        existing = pd.DataFrame(columns=snapshot_df.columns)

    snap_date = snapshot_df["Snapshot_Date"].iloc[0]
    if not existing.empty:
        existing = existing[existing["Snapshot_Date"] != snap_date]

    combined = pd.concat([existing, snapshot_df], ignore_index=True)
    combined.to_csv(snapshot_csv, index=False)
    return combined


def build_comparison_df(all_snapshots: pd.DataFrame) -> pd.DataFrame:
    all_snapshots = all_snapshots.copy()
    all_snapshots["Snapshot_Date"] = pd.to_datetime(all_snapshots["Snapshot_Date"]).dt.date

    snapshot_dates = sorted(all_snapshots["Snapshot_Date"].unique())
    if not snapshot_dates:
        return pd.DataFrame()

    curr_date = snapshot_dates[-1]
    prev_date = snapshot_dates[-2] if len(snapshot_dates) >= 2 else None

    curr = all_snapshots[all_snapshots["Snapshot_Date"] == curr_date].copy()
    prev = (
        all_snapshots[all_snapshots["Snapshot_Date"] == prev_date].copy()
        if prev_date
        else pd.DataFrame(columns=all_snapshots.columns)
    )

    curr = curr.rename(columns={
        "Rank": "Curr_Rank",
        "Total_TEU": "Curr_Total_TEU",
        "Total_Ships": "Curr_Total_Ships",
        "Owned_TEU": "Curr_Owned_TEU",
        "Owned_Ships": "Curr_Owned_Ships",
        "Chartered_TEU": "Curr_Chartered_TEU",
        "Chartered_Ships": "Curr_Chartered_Ships",
        "Orderbook_TEU": "Curr_Orderbook_TEU",
        "Orderbook_Ships": "Curr_Orderbook_Ships",
    })

    prev = prev.rename(columns={
        "Rank": "Prev_Rank",
        "Total_TEU": "Prev_Total_TEU",
        "Total_Ships": "Prev_Total_Ships",
        "Owned_TEU": "Prev_Owned_TEU",
        "Owned_Ships": "Prev_Owned_Ships",
        "Chartered_TEU": "Prev_Chartered_TEU",
        "Chartered_Ships": "Prev_Chartered_Ships",
        "Orderbook_TEU": "Prev_Orderbook_TEU",
        "Orderbook_Ships": "Prev_Orderbook_Ships",
    })

    keep_curr = [
        "Week No",
        "Curr_Rank",
        "Operator",
        "Alliance",
        "Curr_Total_TEU",
        "Curr_Total_Ships",
        "Curr_Owned_TEU",
        "Curr_Owned_Ships",
        "Curr_Chartered_TEU",
        "Curr_Chartered_Ships",
        "Curr_Orderbook_TEU",
        "Curr_Orderbook_Ships",
    ]

    keep_prev = [
        "Operator",
        "Prev_Rank",
        "Prev_Total_TEU",
        "Prev_Total_Ships",
        "Prev_Owned_TEU",
        "Prev_Owned_Ships",
        "Prev_Chartered_TEU",
        "Prev_Chartered_Ships",
        "Prev_Orderbook_TEU",
        "Prev_Orderbook_Ships",
    ]

    out = curr[keep_curr].merge(prev[keep_prev], on="Operator", how="outer")

    # fill descriptive fields for rows only found in previous snapshot
    curr_week_no = curr["Week No"].dropna().iloc[0] if not curr["Week No"].dropna().empty else pd.NA
    out["Week No"] = out["Week No"].fillna(curr_week_no)
    out["Alliance"] = out["Alliance"].fillna("")

    # rank comparison
    out["Curr_Rank"] = pd.to_numeric(out["Curr_Rank"], errors="coerce")
    out["Prev_Rank"] = pd.to_numeric(out["Prev_Rank"], errors="coerce")

    def calc_rank_var(row):
        curr_rank = row["Curr_Rank"]
        prev_rank = row["Prev_Rank"]

        if pd.isna(curr_rank) or pd.isna(prev_rank):
            return pd.NA
        return int(prev_rank - curr_rank)

    def calc_rank_move(row):
        operator = row["Operator"]
        curr_rank = row["Curr_Rank"]
        prev_rank = row["Prev_Rank"]

        if operator == "Others / Outside Top 100":
            return ""

        if pd.isna(curr_rank) and pd.isna(prev_rank):
            return ""
        if pd.isna(prev_rank) and pd.notna(curr_rank):
            return "New"
        if pd.notna(prev_rank) and pd.isna(curr_rank):
            return "Dropped Out"

        diff = int(prev_rank - curr_rank)

        if diff > 0:
            return f"Up {diff}"
        elif diff < 0:
            return f"Down {abs(diff)}"
        else:
            return "No Change"

    out["Var_Rank"] = out.apply(calc_rank_var, axis=1)
    out["Rank_Move"] = out.apply(calc_rank_move, axis=1)

    # numeric current/previous columns except rank
    for c in out.columns:
        if (c.startswith("Curr_") or c.startswith("Prev_")) and c not in {"Curr_Rank", "Prev_Rank"}:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).astype(int)

    pairs = [
        ("Total_TEU", "Var_Total_TEU"),
        ("Total_Ships", "Var_Total_Ships"),
        ("Owned_TEU", "Var_Owned_TEU"),
        ("Owned_Ships", "Var_Owned_Ships"),
        ("Chartered_TEU", "Var_Chartered_TEU"),
        ("Chartered_Ships", "Var_Chartered_Ships"),
        ("Orderbook_TEU", "Var_Orderbook_TEU"),
        ("Orderbook_Ships", "Var_Orderbook_Ships"),
    ]

    for base, var_col in pairs:
        out[var_col] = out[f"Curr_{base}"] - out[f"Prev_{base}"]

    # keep Others at bottom, sort everyone else by current rank
    out["_others"] = out["Operator"].eq("Others / Outside Top 100").astype(int)
    out["_rank"] = pd.to_numeric(out["Curr_Rank"], errors="coerce")
    out = out.sort_values(
        by=["_others", "_rank", "Operator"],
        ascending=[True, True, True],
        na_position="last"
    ).drop(columns=["_others", "_rank"])

    final_cols = [
        "Week No",
        "Curr_Rank",
        "Prev_Rank",
        # "Var_Rank",
        # "Rank_Move",
        "Operator",
        "Alliance",
        "Curr_Total_TEU",
        "Prev_Total_TEU",
        "Var_Total_TEU",
        "Curr_Total_Ships",
        "Prev_Total_Ships",
        "Var_Total_Ships",
        "Curr_Owned_TEU",
        "Prev_Owned_TEU",
        "Var_Owned_TEU",
        "Curr_Owned_Ships",
        "Prev_Owned_Ships",
        "Var_Owned_Ships",
        "Curr_Chartered_TEU",
        "Prev_Chartered_TEU",
        "Var_Chartered_TEU",
        "Curr_Chartered_Ships",
        "Prev_Chartered_Ships",
        "Var_Chartered_Ships",
        "Curr_Orderbook_TEU",
        "Prev_Orderbook_TEU",
        "Var_Orderbook_TEU",
        "Curr_Orderbook_Ships",
        "Prev_Orderbook_Ships",
        "Var_Orderbook_Ships",
    ]

    return out[final_cols].copy()


def main():
    capture_dt = datetime.now()
    snapshot_date = get_monday_date(capture_dt)
    week_no = get_iso_week_no(snapshot_date)

    print("Step 1: Open rendered Alphaliner page via Selenium...")
    driver = start_driver()

    try:
        driver.get(URL)

        # give the page a moment even after DOM load
        time.sleep(6)

        body_text = driver.find_element(By.TAG_NAME, "body").text
        global_teu, global_ships = extract_global_figures_from_body_text(body_text)
        print(f"Global TEU: {global_teu:,}")
        print(f"Global active ships: {global_ships:,}")

        print("Step 2: Extract second table only...")
        df = extract_second_table_with_selenium(driver)

    finally:
        driver.quit()

    print(f"Operator rows extracted: {len(df)}")

    df.insert(0, "Snapshot_Date", snapshot_date)
    df.insert(1, "Week No", week_no)
    df["Alliance"] = df["Operator"].apply(assign_alliance)

    df = df[
        [
            "Snapshot_Date",
            "Week No",
            "Rank",
            "Operator",
            "Alliance",
            "Total_TEU",
            "Total_Ships",
            "Owned_TEU",
            "Owned_Ships",
            "Chartered_TEU",
            "Chartered_Ships",
            "Orderbook_TEU",
            "Orderbook_Ships",
        ]
    ].copy()

    df = append_others_row(df, global_teu, global_ships, snapshot_date, week_no)

    print("Step 3: Save snapshot history...")
    all_snapshots = save_snapshot(df, SNAPSHOT_CSV)

    print("Step 4: Build comparison...")
    comparison = build_comparison_df(all_snapshots)
    comparison.to_csv(COMPARISON_CSV, index=False)

    print("Done.")
    print(f"Snapshot file: {SNAPSHOT_CSV}")
    print(f"Comparison file: {COMPARISON_CSV}")


if __name__ == "__main__":
    main()