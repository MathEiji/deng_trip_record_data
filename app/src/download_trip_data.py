"""Download FHVHV trip data parquet files from the NYC TLC CloudFront CDN."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import requests

BASE_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data"
OUTPUT_DIR = Path(__file__).parent / "../../data/staging"


def month_range(start: str, end: str) -> list[str]:
    """Return a list of 'YYYY-MM' strings from *start* to *end* inclusive."""
    start_dt = datetime.strptime(start, "%Y-%m")
    end_dt = datetime.strptime(end, "%Y-%m")
    if start_dt > end_dt:
        raise ValueError(f"Start {start} is after end {end}")

    months: list[str] = []
    current = start_dt
    while current <= end_dt:
        months.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def download_file(url: str, dest: Path) -> None:
    """Stream-download *url* to *dest*, printing progress."""
    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(f"\r  {downloaded / 1e6:.1f} / {total / 1e6:.1f} MB ({pct:.0f}%)", end="", flush=True)
        print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start", help="Start month in YYYY-MM format (e.g. 2025-01)")
    parser.add_argument("end", help="End month in YYYY-MM format (e.g. 2025-06)")
    args = parser.parse_args(argv)

    months = month_range(args.start, args.end)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {len(months)} file(s) to {OUTPUT_DIR}/\n")

    for ym in months:
        filename = f"fhvhv_tripdata_{ym}.parquet"
        dest = OUTPUT_DIR / filename

        if dest.exists():
            print(f"[skip] {filename} (already exists)")
            continue

        url = f"{BASE_URL}/{filename}"
        print(f"[download] {filename}")
        try:
            download_file(url, dest)
            print(f"  -> saved {dest}")
        except requests.HTTPError as exc:
            print(f"  [error] {exc}", file=sys.stderr)
            if dest.exists():
                dest.unlink()


if __name__ == "__main__":
    main()
