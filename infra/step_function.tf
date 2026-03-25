# ──────────────────────────────────────────────
# Step Functions state machine
# download → raw → trusted → specialized
# ──────────────────────────────────────────────

locals {
  ecs_network_config = {
    AwsvpcConfiguration = {
      Subnets        = data.aws_subnets.default.ids
      SecurityGroups = [aws_security_group.ecs_task.id]
      AssignPublicIp = "ENABLED"
    }
  }
}

resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.project_name}-pipeline"
  role_arn = aws_iam_role.step_functions.arn

  definition = jsonencode({
    Comment = "Full pipeline: download → raw → trusted → specialized"
    StartAt = "DownloadTripData"
    States = {
      DownloadTripData = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster              = aws_ecs_cluster.main.arn
          TaskDefinition       = aws_ecs_task_definition.download_trip_data.arn
          LaunchType           = "FARGATE"
          NetworkConfiguration = local.ecs_network_config
          Overrides = {
            ContainerOverrides = [{
              Name = "download-trip-data"
              Environment = [
                { "Name" = "START_MONTH", "Value.$" = "$.start_month" },
                { "Name" = "END_MONTH", "Value.$" = "$.end_month" },
              ]
            }]
          }
        }
        ResultPath = "$.downloadResult"
        Next       = "BuildRawLayer"
      }

      BuildRawLayer = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster              = aws_ecs_cluster.main.arn
          TaskDefinition       = aws_ecs_task_definition.build_raw_layer.arn
          LaunchType           = "FARGATE"
          NetworkConfiguration = local.ecs_network_config
          Overrides = {
            ContainerOverrides = [{
              Name = "build-raw-layer"
              Environment = [
                { "Name" = "START_MONTH", "Value.$" = "$.start_month" },
                { "Name" = "END_MONTH", "Value.$" = "$.end_month" },
              ]
            }]
          }
        }
        ResultPath = "$.rawResult"
        Next       = "BuildTrustedLayer"
      }

      BuildTrustedLayer = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster              = aws_ecs_cluster.main.arn
          TaskDefinition       = aws_ecs_task_definition.build_trusted_layer.arn
          LaunchType           = "FARGATE"
          NetworkConfiguration = local.ecs_network_config
          Overrides = {
            ContainerOverrides = [{
              Name = "build-trusted-layer"
              Environment = [
                { "Name" = "START_MONTH", "Value.$" = "$.start_month" },
                { "Name" = "END_MONTH", "Value.$" = "$.end_month" },
              ]
            }]
          }
        }
        ResultPath = "$.trustedResult"
        Next       = "BuildSpecializedLayer"
      }

      BuildSpecializedLayer = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster              = aws_ecs_cluster.main.arn
          TaskDefinition       = aws_ecs_task_definition.build_specialized_layer.arn
          LaunchType           = "FARGATE"
          NetworkConfiguration = local.ecs_network_config
          Overrides = {
            ContainerOverrides = [{
              Name = "build-specialized-layer"
              Environment = [
                { "Name" = "START_MONTH", "Value.$" = "$.start_month" },
                { "Name" = "END_MONTH", "Value.$" = "$.end_month" },
              ]
            }]
          }
        }
        End = true
      }
    }
  })
}

# ──────────────────────────────────────────────
# IAM role for Step Functions
# ──────────────────────────────────────────────
resource "aws_iam_role" "step_functions" {
  name = "${var.project_name}-step-functions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "step_functions" {
  name = "ecs-and-events"
  role = aws_iam_role.step_functions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ECSRunTask"
        Effect = "Allow"
        Action = [
          "ecs:RunTask",
          "ecs:StopTask",
          "ecs:DescribeTasks",
        ]
        Resource = "*"
      },
      {
        Sid    = "PassRole"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          aws_iam_role.ecs_task_execution.arn,
          aws_iam_role.ecs_task.arn,
        ]
      },
      {
        Sid    = "EventBridge"
        Effect = "Allow"
        Action = [
          "events:PutTargets",
          "events:PutRule",
          "events:DescribeRule",
        ]
        Resource = "arn:aws:events:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventsForECSTaskRule"
      }
    ]
  })
}
