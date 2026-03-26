locals {
  developers = toset([
    "leandro.sousa",
    "caio.ribeiro",
    "joao.albino",
  ])
}

resource "aws_iam_user" "developers" {
  for_each = local.developers
  name     = each.key
}

resource "aws_iam_user_login_profile" "developers" {
  for_each                = local.developers
  user                    = aws_iam_user.developers[each.key].name
  password_reset_required = true
}

resource "aws_iam_group" "developers" {
  name = "${var.project_name}-developers"
}

resource "aws_iam_group_membership" "developers" {
  name  = "${var.project_name}-developers"
  group = aws_iam_group.developers.name
  users = [for u in aws_iam_user.developers : u.name]
}

resource "aws_iam_group_policy" "developers" {
  name  = "developer-access"
  group = aws_iam_group.developers.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ECRAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "ECRRepo"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:PutImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeRepositories",
          "ecr:ListImages",
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Sid    = "ECS"
        Effect = "Allow"
        Action = [
          "ecs:DescribeClusters",
          "ecs:ListClusters",
          "ecs:DescribeTaskDefinition",
          "ecs:ListTaskDefinitions",
          "ecs:DescribeTasks",
          "ecs:ListTasks",
          "ecs:DescribeServices",
          "ecs:ListServices",
          "ecs:RunTask",
        ]
        Resource = "*"
      },
      {
        Sid    = "StepFunctions"
        Effect = "Allow"
        Action = [
          "states:ListStateMachines",
          "states:DescribeStateMachine",
          "states:ListExecutions",
          "states:DescribeExecution",
          "states:GetExecutionHistory",
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
        Sid      = "S3ReadOnly"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetBucketLocation", "s3:ListBucket"]
        Resource = "*"
      },
      {
        Sid      = "S3ListAll"
        Effect   = "Allow"
        Action   = ["s3:ListAllMyBuckets"]
        Resource = "*"
      },
      {
        Sid    = "EC2Describe"
        Effect = "Allow"
        Action = [
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
        ]
        Resource = "*"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:GetLogEvents",
          "logs:FilterLogEvents",
          "logs:DescribeLogStreams",
          "logs:DescribeLogGroups",
        ]
        Resource = "${aws_cloudwatch_log_group.ecs.arn}:*"
      }
    ]
  })
}
