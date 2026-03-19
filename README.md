<!-- Projeto de pipelines de dados para NYC TLC Trip Records - mantido por MathEiji -->

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
├── .github/
│   └── workflows/
│       └── deploy.yml              # CI/CD: build → ECR → ECS task definition
├── app/
│   ├── Dockerfile                  # Container image for download job
│   └── src/
│       ├── download_trip_data.py   # Download FHVHV parquets from the TLC CDN
│       ├── build_raw_layer.py      # Build raw tables from staging data
│       └── requirements.txt        # App dependencies (requests, duckdb)
├── infra/                          # Terraform (ECS Fargate + ECR + S3 + IAM)
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── ecr.tf
│   ├── ecs.tf
│   ├── iam.tf
│   ├── s3.tf
│   └── cloudwatch.tf
├── data/
│   ├── staging/                    # Downloaded parquet files (git-ignored)
│   └── raw/                        # Context-based raw tables (git-ignored)
├── reference/                      # Dimension CSVs (version-controlled)
│   ├── dim_hvfhs_license.csv       # HVFHS license → company mapping
│   └── dim_base.csv                # TLC base number → company mapping
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

## Building the raw layer

Once staging data is downloaded, build the raw layer:

```bash
python app/src/build_raw_layer.py
```

The script:
1. Scans all `data/staging/fhvhv_tripdata_*.parquet` files
2. Runs a data quality analysis (nulls, distributions, statistics)
3. Assigns a deterministic `trip_id` and `processed_date` to every row
4. Splits the monolithic schema into 4 context-based raw tables in `data/raw/`
5. Converts dimension CSVs from `reference/` into parquet dimension tables
6. Validates row counts and join consistency

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

### Dimension tables

Reference/lookup tables stored as CSVs in `reference/` and converted to parquet during build:

| Dimension table | Purpose | Columns |
|-----------------|---------|---------|
| **dim_hvfhs_license** | HVFHS license → company | `hvfhs_license_num`, `company_name`, `dispatching_base_num`, `status` |
| **dim_base** | TLC base number → company | `base_number`, `base_name`, `parent_company`, `base_type` |

See `notebooks/raw_tables_exploration.ipynb` for schema inspection, sampling, and optional code to write these tables to Parquet.

---

## Infrastructure & Deployment

The project uses **ECS Fargate** (ARM64/Graviton) to run the download job in AWS, with **GitHub Actions** for CI/CD. All infrastructure is defined as Terraform in `infra/`.

### AWS resources (free-tier optimised)

| Resource | Purpose | Free tier note |
|----------|---------|----------------|
| **ECR** | Docker image registry | 500 MB storage |
| **ECS Fargate** | Run download task (0.25 vCPU / 0.5 GB, ARM64) | 50 vCPU-hrs + 100 GB-hrs/month (12 months) |
| **S3** | Store trip record parquets | 5 GB standard storage |
| **CloudWatch Logs** | Task logs (7-day retention) | 5 GB ingestion |

### First-time setup

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # edit with your values
terraform init
terraform plan
terraform apply
```

After `terraform apply`, set the following GitHub repository secret:

| Secret | Value (from Terraform output) |
|--------|-------------------------------|
| `AWS_ROLE_ARN` | `github_actions_role_arn` |

### CI/CD pipeline

The GitHub Actions workflow (`.github/workflows/deploy.yml`) runs on every push to `main` that touches `app/`:

1. **build-and-push** — Builds the ARM64 Docker image and pushes to ECR
2. **deploy** — Registers a new ECS task definition revision with the updated image
3. **run-task** *(manual only)* — Triggers the Fargate task via `workflow_dispatch` with configurable `start_month` / `end_month`

---

## Setup

- **Python:** 3.x
- **App dependencies:** `pip install -r app/src/requirements.txt` (includes `duckdb` for raw layer build)
- **Notebook dependencies:** `pip install -r notebooks/requirements.txt`
