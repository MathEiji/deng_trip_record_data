<!-- Projeto de pipelines de dados para NYC TLC Trip Records - mantido por MathEiji -->

# NYC TLC Trip Record Data Pipelines

Data engineering project that ingests, transforms, and serves **New York City Taxi and Limousine Commission (TLC) High Volume For-Hire Vehicle (FHVHV)** trip records (Uber, Lyft) through a multi-layer data pipeline running on AWS.

**Data source:** [TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)

---

## Architecture

```
┌────────────┐     ┌───────────┐     ┌─────────────┐     ┌──────────────────────────┐
│  Download   │────▶│  Raw      │────▶│  Trusted    │────▶│  Specialized (×4)        │
│  (staging)  │     │  Layer    │     │  Layer      │     │  (parallel)              │
└────────────┘     └───────────┘     └─────────────┘     └──────────────────────────┘
```

Orchestrated by **AWS Step Functions**, each stage runs as an **ECS Fargate** task (ARM64/Graviton). Data is stored in **S3** as Hive-partitioned Parquet and cataloged in **AWS Glue Data Catalog** for querying via Athena.

All data is partitioned by `year_month` (integer, `yyyyMM` format).

---

## Repo structure

```
deng_trip_record_data/
├── .github/workflows/
│   └── deploy.yml                    # CI/CD: build → ECR → ECS task definitions
├── app/
│   ├── Dockerfile
│   └── src/
│       ├── common/                   # Shared pipeline infrastructure
│       │   ├── __init__.py
│       │   ├── pipeline.py           # DuckDB lifecycle, Glue helpers, constants
│       │   └── specialized.py        # Specialized-layer orchestrator (run())
│       ├── download_trip_data.py     # Stream FHVHV parquets from TLC CDN to S3
│       ├── build_raw_layer.py        # Split staging into context-based raw tables
│       ├── build_trusted_layer.py    # Denormalize, clean, enrich into trusted_trips
│       ├── build_spec_hourly_volume.py   # Q1: peak hours
│       ├── build_spec_daily_volume.py    # Q2: peak weekdays
│       ├── build_spec_trip_distance.py   # Q3: distance distribution
│       ├── build_spec_distance_fare.py   # Q4: distance vs fare
│       └── requirements.txt
├── infra/                            # Terraform: ECS, ECR, S3, IAM, Glue, SFN, etc.
├── reference/                        # Dimension CSVs (version-controlled)
│   ├── dim_hvfhs_license.csv
│   └── dim_base.csv
├── notebooks/                        # Ad-hoc exploration and checks
└── data/                             # Local data (git-ignored)
```

---

## Pipeline stages

### 1. Download (`download_trip_data.py`)

Streams FHVHV parquet files from the NYC TLC CDN directly to S3 using multipart uploads. Skips files that already exist in the bucket.

**Input:** NYC TLC CDN  
**Output:** `s3://<bucket>/staging/fhvhv_tripdata_YYYY-MM.parquet`

### 2. Raw layer (`build_raw_layer.py`)

Reads staging parquets, runs data quality analysis, assigns a `trip_id` and `processed_date`, then splits into 4 context-based tables plus dimension tables. Registers all tables and partitions in Glue.

**Input:** `s3://<bucket>/staging/`  
**Output:** `s3://<bucket>/raw/<table_name>/year_month=YYYYMM/`

| Raw table | Context | Key columns |
|---|---|---|
| **raw_dispatch_base** | Dispatch / base | `hvfhs_license_num`, `dispatching_base_num`, `originating_base_num` |
| **raw_trip_time_location** | Timing & locations | `request_datetime`, `pickup_datetime`, `dropoff_datetime`, `PULocationID`, `DOLocationID`, `trip_miles`, `trip_time` |
| **raw_fare_payment** | Fare & payment | `base_passenger_fare`, `tolls`, `bcf`, `sales_tax`, `congestion_surcharge`, `airport_fee`, `tips`, `driver_pay` |
| **raw_request_flags** | Request flags | `shared_request_flag`, `shared_match_flag`, `access_a_ride_flag`, `wav_request_flag`, `wav_match_flag` |

**Dimension tables:** `dim_hvfhs_license`, `dim_base` (from `reference/` CSVs)

### 3. Trusted layer (`build_trusted_layer.py`)

Joins all 4 raw tables + `dim_hvfhs_license`, applies quality filters (positive fare/distance, outlier caps, temporal consistency), and computes derived fields. Uses a 3 GB DuckDB memory limit to fit within Fargate's 4 GB container.

**Input:** `s3://<bucket>/raw/`  
**Output:** `s3://<bucket>/trusted/trusted_trips/year_month=YYYYMM/`

| Column group | Fields |
|---|---|
| **Identity** | `trip_id`, `year_month`, `processed_date` |
| **Provider** | `company_name`, `hvfhs_license_num` |
| **Timing** | `request_datetime`, `pickup_datetime`, `dropoff_datetime` |
| **Derived time** | `pickup_date`, `pickup_hour`, `pickup_day_of_week`, `pickup_day_name`, `wait_time_seconds`, `trip_duration_seconds` |
| **Distance** | `trip_miles`, `trip_time_seconds`, `pickup_location_id`, `dropoff_location_id` |
| **Fare** | `base_passenger_fare`, `tolls`, `congestion_surcharge`, `airport_fee`, `tips`, `driver_pay`, `total_fare`, `fare_per_mile` |
| **Flags** | `is_shared_request`, `is_shared_match`, `is_wav_match` |

### 4. Specialized layer (4 parallel tasks)

Each table is built by its own script, running in parallel via Step Functions. All read from `trusted_trips` and write Hive-partitioned output.

**Input:** `s3://<bucket>/trusted/trusted_trips/`  
**Output:** `s3://<bucket>/specialized/<table_name>/year_month=YYYYMM/`

| Script | Table | Question | Grain |
|---|---|---|---|
| `build_spec_hourly_volume.py` | **spec_hourly_volume** | Q1: Peak hours of day | `year_month` × `company` × `pickup_hour` |
| `build_spec_daily_volume.py` | **spec_daily_volume** | Q2: Peak days of week | `year_month` × `company` × `day_of_week` |
| `build_spec_trip_distance.py` | **spec_trip_distance** | Q3: Distance distribution | `year_month` × `company` |
| `build_spec_distance_fare.py` | **spec_distance_fare** | Q4: Distance vs fare | `year_month` × `company` × `distance_bucket` |

---

## Infrastructure

All AWS resources are defined as Terraform in `infra/`. The pipeline is designed to stay within free-tier limits where possible.

| Resource | Purpose |
|---|---|
| **ECS Fargate** (ARM64) | Run pipeline tasks (download, raw, trusted, 4× specialized) |
| **ECR** | Docker image registry |
| **S3** | Data storage (staging, raw, trusted, specialized) |
| **Step Functions** | Orchestrate the full pipeline: download → raw → trusted → specialized (parallel) |
| **Glue Data Catalog** | Table/partition metadata for Athena queries |
| **CloudWatch Logs** | Task logs (7-day retention) |
| **IAM** | Task roles, GitHub Actions OIDC, developer read access |

### First-time setup

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # edit with your values
terraform init
terraform plan
terraform apply
```

After `terraform apply`, set the following GitHub repository secrets:

| Secret | Value (from Terraform output) |
|---|---|
| `AWS_ROLE_ARN` | `github_actions_role_arn` |
| `STATE_MACHINE_ARN` | `state_machine_arn` |

### CI/CD pipeline

The GitHub Actions workflow (`.github/workflows/deploy.yml`) runs on every push to `main` that touches `app/`, `reference/`, or the workflow itself:

1. **build-and-push** — Builds the ARM64 Docker image and pushes to ECR
2. **deploy** — Registers new ECS task definition revisions (7 task families) with the updated image
3. **run-pipeline** *(manual only)* — Starts the Step Functions pipeline via `workflow_dispatch` with configurable `start_month` / `end_month`

---

## Local development

**Python:** 3.12+

```bash
pip install -r app/src/requirements.txt      # pipeline dependencies
pip install -r notebooks/requirements.txt    # notebook dependencies (optional)
```
