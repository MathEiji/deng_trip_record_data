resource "aws_ecs_cluster" "main" {
  name = var.project_name

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_task_definition" "download_trip_data" {
  family                   = "${var.project_name}-download"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"  # 0.25 vCPU — Fargate minimum
  memory                   = "512"  # 0.5 GB   — Fargate minimum
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64" # Graviton — qualifies for Fargate free tier
  }

  container_definitions = jsonencode([
    {
      name      = "download-trip-data"
      image     = "${aws_ecr_repository.app.repository_url}:latest"
      essential = true

      environment = [
        { name = "S3_BUCKET", value = var.s3_bucket_name },
        { name = "S3_PREFIX", value = "staging" },
        { name = "START_MONTH", value = var.start_month },
        { name = "END_MONTH", value = var.end_month },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "download"
        }
      }
    }
  ])
}

resource "aws_ecs_task_definition" "build_raw_layer" {
  family                   = "${var.project_name}-build-raw"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024" # 1 vCPU
  memory                   = "4096" # 4 GB — headroom for DuckDB materialization
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  ephemeral_storage {
    size_in_gib = 21
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "build-raw-layer"
      image     = "${aws_ecr_repository.app.repository_url}:latest"
      essential = true
      command   = ["src/build_raw_layer.py"]

      environment = [
        { name = "S3_BUCKET", value = var.s3_bucket_name },
        { name = "S3_STAGING_PREFIX", value = "staging" },
        { name = "S3_RAW_PREFIX", value = "raw" },
        { name = "START_MONTH", value = var.start_month },
        { name = "END_MONTH", value = var.end_month },
        { name = "GLUE_DATABASE", value = var.glue_database },
        { name = "AWS_REGION", value = var.aws_region },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "build-raw"
        }
      }
    }
  ])
}

resource "aws_ecs_task_definition" "build_trusted_layer" {
  family                   = "${var.project_name}-build-trusted"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "4096"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  ephemeral_storage {
    size_in_gib = 21
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "build-trusted-layer"
      image     = "${aws_ecr_repository.app.repository_url}:latest"
      essential = true
      command   = ["src/build_trusted_layer.py"]

      environment = [
        { name = "S3_BUCKET", value = var.s3_bucket_name },
        { name = "S3_RAW_PREFIX", value = "raw" },
        { name = "S3_TRUSTED_PREFIX", value = "trusted" },
        { name = "START_MONTH", value = var.start_month },
        { name = "END_MONTH", value = var.end_month },
        { name = "GLUE_DATABASE", value = var.glue_database },
        { name = "AWS_REGION", value = var.aws_region },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "build-trusted"
        }
      }
    }
  ])
}

locals {
  specialized_tables = {
    "spec-hourly-volume" = "src/build_spec_hourly_volume.py"
    "spec-daily-volume"  = "src/build_spec_daily_volume.py"
    "spec-trip-distance" = "src/build_spec_trip_distance.py"
    "spec-distance-fare" = "src/build_spec_distance_fare.py"
  }
}

resource "aws_ecs_task_definition" "build_specialized" {
  for_each = local.specialized_tables

  family                   = "${var.project_name}-build-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  ephemeral_storage {
    size_in_gib = 21
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "build-${each.key}"
      image     = "${aws_ecr_repository.app.repository_url}:latest"
      essential = true
      command   = [each.value]

      environment = [
        { name = "S3_BUCKET", value = var.s3_bucket_name },
        { name = "S3_TRUSTED_PREFIX", value = "trusted" },
        { name = "S3_SPECIALIZED_PREFIX", value = "specialized" },
        { name = "START_MONTH", value = var.start_month },
        { name = "END_MONTH", value = var.end_month },
        { name = "GLUE_DATABASE", value = var.glue_database },
        { name = "AWS_REGION", value = var.aws_region },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "build-${each.key}"
        }
      }
    }
  ])
}

resource "aws_security_group" "ecs_task" {
  name_prefix = "${var.project_name}-ecs-"
  description = "Allow outbound internet access for ECS Fargate tasks"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound traffic"
  }

  lifecycle {
    create_before_destroy = true
  }
}
