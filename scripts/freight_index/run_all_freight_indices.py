import subprocess
import sys
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(r"C:\Data\ocean_market_intelligence\scripts\freight_index")
LOG_DIR = BASE_DIR / "data" / "logs" / "freight_index"
LOG_DIR.mkdir(exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = LOG_DIR / f"freight_index_update_{timestamp}.log"

scripts = [
    "TNSC_SCFI.py",
    "Drewry_WCI.py",
    "Freightos_FBX.py",
]

def write_log(message: str):
    print(message)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(message + "\n")

def run_script(script_name: str):
    script_path = BASE_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")

    write_log("=" * 80)
    write_log(f"START RUNNING: {script_name}")
    write_log(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    write_log("=" * 80)

    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True
    )

    if result.stdout:
        write_log("STDOUT:")
        write_log(result.stdout)

    if result.stderr:
        write_log("STDERR:")
        write_log(result.stderr)

    if result.returncode != 0:
        raise RuntimeError(f"{script_name} failed with exit code {result.returncode}")

    write_log(f"SUCCESS: {script_name}\n")

def main():
    write_log(f"Freight index update started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    write_log(f"Base directory: {BASE_DIR}")
    write_log(f"Python executable: {sys.executable}\n")

    failed_scripts = []

    for script in scripts:
        try:
            run_script(script)
        except Exception as e:
            write_log(f"FAILED: {script}")
            write_log(f"ERROR: {e}\n")
            failed_scripts.append(script)

    write_log("=" * 80)
    if failed_scripts:
        write_log("UPDATE COMPLETED WITH FAILURES")
        write_log("Failed scripts:")
        for s in failed_scripts:
            write_log(f"- {s}")
    else:
        write_log("UPDATE COMPLETED SUCCESSFULLY")
    write_log("=" * 80)

if __name__ == "__main__":
    main()