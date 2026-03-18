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
