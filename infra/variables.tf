variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "trip-record-data"
}

variable "github_org" {
  description = "GitHub organization or username"
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name"
  type        = string
  default     = "deng_trip_record_data"
}

variable "s3_bucket_name" {
  description = "S3 bucket name for trip record data (must be globally unique)"
  type        = string
}

variable "start_month" {
  description = "Default start month for data download (YYYY-MM)"
  type        = string
  default     = "2025-01"
}

variable "end_month" {
  description = "Default end month for data download (YYYY-MM)"
  type        = string
  default     = "2025-06"
}

variable "glue_database" {
  description = "Glue Data Catalog database name for the raw layer"
  type        = string
  default     = "trip_record_data"
}
