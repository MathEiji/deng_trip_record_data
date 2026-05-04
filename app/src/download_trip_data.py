# S3_BUCKET     – target bucket (required)
# S3_PREFIX     – key prefix    (default: "staging")
# START_MONTH   – YYYY-MM
# END_MONTH     – YYYY-MM

import argparse
import logging
import os
import sys
from datetime import datetime

import boto3
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data"
MULTIPART_CHUNK = 8 * 1024 * 1024  # 8 MB


def month_range(start: str, end: str) -> list[str]:
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


def s3_key_exists(s3_client, bucket: str, key: str) -> bool:
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except s3_client.exceptions.ClientError:
        return False


def stream_to_s3(
    url: str, s3_client, bucket: str, key: str, timeout: int = 60
) -> int:
    """Stream a remote file directly into S3 without buffering locally."""
    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        content_length = int(resp.headers.get("content-length", 0))

        mpu = s3_client.create_multipart_upload(Bucket=bucket, Key=key)
        upload_id = mpu["UploadId"]
        parts: list[dict] = []
        part_number = 1
        uploaded = 0

        try:
            for chunk in resp.iter_content(chunk_size=MULTIPART_CHUNK):
                part = s3_client.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                )
                parts.append({"ETag": part["ETag"], "PartNumber": part_number})
                uploaded += len(chunk)
                part_number += 1

                if content_length:
                    pct = uploaded / content_length * 100
                    log.info(
                        "  %s: %.1f / %.1f MB (%.0f%%)",
                        key, uploaded / 1e6, content_length / 1e6, pct,
                    )

            s3_client.complete_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            s3_client.abort_multipart_upload(
                Bucket=bucket, Key=key, UploadId=upload_id,
            )
            raise

    return uploaded


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start", nargs="?", help="Start month YYYY-MM (or env START_MONTH)")
    parser.add_argument("end", nargs="?", help="End month YYYY-MM (or env END_MONTH)")
    parser.add_argument("--bucket", help="S3 bucket (or env S3_BUCKET)")
    parser.add_argument("--prefix", help="S3 key prefix (or env S3_PREFIX)")
    args = parser.parse_args(argv)

    bucket = args.bucket or os.environ.get("S3_BUCKET")
    prefix = (args.prefix or os.environ.get("S3_PREFIX", "staging")).strip("/")
    start = args.start or os.environ.get("START_MONTH")
    end = args.end or os.environ.get("END_MONTH")

    if not bucket:
        log.error("S3_BUCKET is required (pass --bucket or set S3_BUCKET env var)")
        sys.exit(1)
    if not start or not end:
        log.error("START_MONTH and END_MONTH are required (positional args or env vars)")
        sys.exit(1)

    months = month_range(start, end)
    s3_client = boto3.client("s3")

    log.info("Downloading %d file(s) → s3://%s/%s/", len(months), bucket, prefix)

    ok_count, skip_count, err_count = 0, 0, 0

    for ym in months:
        filename = f"fhvhv_tripdata_{ym}.parquet"
        key = f"{prefix}/{filename}"

        if s3_key_exists(s3_client, bucket, key):
            log.info("[skip] %s (already in S3)", filename)
            skip_count += 1
            continue

        url = f"{BASE_URL}/{filename}"
        log.info("[download] %s → s3://%s/%s", filename, bucket, key)
        try:
            total = stream_to_s3(url, s3_client, bucket, key)
            log.info("  -> uploaded %.1f MB", total / 1e6)
            ok_count += 1
        except requests.HTTPError as exc:
            log.error("  HTTP error for %s: %s", filename, exc)
            err_count += 1
        except Exception as exc:
            log.error("  Failed %s: %s", filename, exc)
            err_count += 1

    log.info(
        "Done. uploaded=%d  skipped=%d  errors=%d",
        ok_count, skip_count, err_count,
    )
    if err_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
