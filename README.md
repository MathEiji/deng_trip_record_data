# NYC TLC Trip Record Data Pipelines

This repository contains data pipelines and exploration using **New York City Taxi and Limousine Commission (TLC) Trip Record Data**, in particular the **High Volume For-Hire Vehicle (FHVHV)** trip records (e.g. Uber, Lyft).

**Data source:** [TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)

---

## Data

- **Format:** Parquet (e.g. `fhvhv_tripdata_YYYY-MM.parquet`)
- **Typical size:** Tens of millions of rows per month (~21M for Jan 2026 sample)
- **Location:** Downloaded files live in `data/staging/` (git-ignored). Run the download script to fetch them.

---

## Repo structure

```
deng_trip_record_data/
├── app/
│   └── src/
│       ├── download_trip_data.py   # Download FHVHV parquets from the TLC CDN
│       └── requirements.txt        # App dependencies (requests)
├── data/
│   └── staging/                    # Downloaded parquet files (git-ignored)
├── notebooks/
│   ├── data_check.ipynb            # Ad-hoc checks and counts (DuckDB + pandas)
│   ├── raw_tables_exploration.ipynb# Raw tables design by context
│   └── requirements.txt           # Notebook dependencies (pandas, duckdb, pyarrow, requests)
├── .gitignore
└── README.md
```

---

## Downloading data

Use `download_trip_data.py` to fetch FHVHV parquet files for a range of months:

```bash
python app/src/download_trip_data.py 2025-01 2025-06
```

Files are saved to `data/staging/`. Already-downloaded files are skipped on re-run.

---

## Raw tables (by context)

The raw layer splits the single FHVHV parquet into logical tables. Each table includes:
- **trip_id** — join key across raw tables
- **processed_date** — partition column in `yyyyMMdd` format (e.g. `20260308`)

| Raw table | Context | Main columns |
|-----------|---------|--------------|
| **raw_dispatch_base** | Dispatch / base | `hvfhs_license_num`, `dispatching_base_num`, `originating_base_num` |
| **raw_trip_time_location** | Trip timing & locations | `request_datetime`, `on_scene_datetime`, `pickup_datetime`, `dropoff_datetime`, `PULocationID`, `DOLocationID`, `trip_miles`, `trip_time` |
| **raw_fare_payment** | Fare and payment | `base_passenger_fare`, `tolls`, `bcf`, `sales_tax`, `congestion_surcharge`, `airport_fee`, `tips`, `driver_pay`, `cbd_congestion_fee` |
| **raw_request_flags** | Request flags | `shared_request_flag`, `shared_match_flag`, `access_a_ride_flag`, `wav_request_flag`, `wav_match_flag` |

See `notebooks/raw_tables_exploration.ipynb` for schema inspection, sampling, and optional code to write these tables to Parquet.

---

## Setup

- **Python:** 3.x
- **App dependencies:** `pip install -r app/src/requirements.txt`
- **Notebook dependencies:** `pip install -r notebooks/requirements.txt`
