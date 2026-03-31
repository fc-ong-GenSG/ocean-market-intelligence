import cloudscraper
import pandas as pd
from bs4 import BeautifulSoup
from io import StringIO
import os
import re
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = REPO_ROOT / "data" / "processed" / "bunker"
BASE_DIR.mkdir(parents=True, exist_ok=True)


def clean_dates(df):
    """
    Identifies the year by detecting month rollovers in descending lists.
    Formats dates to 'dd-mmm-yyyy'.
    """
    if 'Date' not in df.columns:
        return df

    # 1. Clean the day prefixes (M, T, W, etc.)
    df['Date'] = df['Date'].astype(str).str.replace(r'^[MTWTFSS]\s+', '', regex=True)

    # 2. Logic to determine the year
    current_year = 2026  # Per instructions, we are in 2026
    years = []
    last_month_num = 13  # Start higher than December (12)

    month_map = {
        'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
        'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
    }

    for date_str in df['Date']:
        # Extract the month abbreviation (e.g., "Mar")
        match = re.search(r'([A-Za-z]+)', date_str)
        if match:
            month_str = match.group(1)
            month_num = month_map.get(month_str, 1)

            # If month number increases (e.g., from 1/Jan to 12/Dec) 
            # while moving DOWN the list, we've hit the previous year.
            if month_num > last_month_num:
                current_year -= 1
            
            years.append(current_year)
            last_month_num = month_num
        else:
            years.append(current_year)

    # 3. Create the final date string: dd-mmm-yyyy
    new_dates = []
    for i, row in df.iterrows():
        # Extract the day (e.g., "18")
        day = re.search(r'(\d+)', row['Date']).group(1).zfill(2)
        month = re.search(r'([A-Za-z]+)', row['Date']).group(1)
        year = years[i]
        new_dates.append(f"{day}-{month}-{year}")

    df['Date'] = new_dates
    return df

def scrape_and_update():
    output_file = BASE_DIR / "all_bunker_prices.csv"

    scraper = cloudscraper.create_scraper()
    
    tasks = [
        {"location": "Global", "url": "https://shipandbunker.com/prices/av/global/av-glb-global-average-bunker-price", "fuels": ['VLSFO', 'MGO', 'IFO380', 'SS']},
        {"location": "Global 4 Ports", "url": "https://shipandbunker.com/prices/av/global/av-g04-global-4-ports-average", "fuels": ['VLSFO', 'MGO', 'BIO', 'IFO380', 'SS']},
        {"location": "Americas", "url": "https://shipandbunker.com/prices/av/region/av-am-americas-average", "fuels": ['VLSFO', 'MGO', 'IFO380', 'SS']},
        {"location": "APAC", "url": "https://shipandbunker.com/prices/av/region/av-apa-apac-average", "fuels": ['VLSFO', 'MGO', 'IFO380', 'SS']},
        {"location": "EMEA", "url": "https://shipandbunker.com/prices/av/region/av-eme-emea-average", "fuels": ['VLSFO', 'MGO', 'IFO380', 'SS']},
        {"location": "Rotterdam", "url": "https://shipandbunker.com/prices/emea/nwe/nl-rtm-rotterdam", "fuels": ['VLSFO', 'MGO', 'LSMGO', 'BIO', 'IFO380', 'SS', 'MEOH', 'MEOHVLS', 'MEOHMGOe', 'LNG', 'LNG-380e', 'LNG-MGOe']},
        {"location": "Singapore", "url": "https://shipandbunker.com/prices/apac/sea/sg-sin-singapore", "fuels": ['VLSFO', 'MGO', 'LSMGO', 'BIO', 'IFO380', 'SS', 'MEOH', 'MEOHVLSF', 'MEOHMGOe']},
        {"location": "Houston", "url": "https://shipandbunker.com/prices/am/usgac/us-hou-houston", "fuels": ['VLSFO', 'MGO', 'BIO', 'IFO380', 'SS', 'MEOH', 'MEOHVLSF', 'MEOHMGOe']},
        {"location": "Fujairah", "url": "https://shipandbunker.com/prices/emea/me/ae-fjr-fujairah", "fuels": ['VLSFO', 'MGO', 'BIO', 'IFO380', 'SS', 'MEOH', 'MEOHVLSF', 'MEOHMGOe']}
    ]

    all_dfs = []
    for task in tasks:
        res = scraper.get(task['url'])
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            for fuel in task['fuels']:
                table = soup.select_one(f"table.price-table.{fuel}")
                if table:
                    df = pd.read_html(StringIO(str(table)))[0]
                    df['Location'], df['Fuel_Type'] = task['location'], fuel
                    df = clean_dates(df) # Apply the new date logic
                    all_dfs.append(df)

    if all_dfs:
        combined = pd.concat(all_dfs, ignore_index=True)
        if os.path.exists(output_file):
            old = pd.read_csv(output_file)
            combined = pd.concat([old, combined], ignore_index=True)
        
        combined.drop_duplicates(subset=['Location', 'Fuel_Type', 'Date'], keep='last', inplace=True)
        combined.to_csv(output_file, index=False)

if __name__ == "__main__":
    scrape_and_update()