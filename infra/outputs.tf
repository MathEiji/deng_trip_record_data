output "ecr_repository_url" {
  description = "ECR repository URL for the app image"
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}

output "task_definition_family" {
  description = "ECS task definition family"
  value       = aws_ecs_task_definition.download_trip_data.family
}

output "s3_bucket_name" {
  description = "S3 bucket for trip record data"
  value       = aws_s3_bucket.data.id
}

output "github_actions_role_arn" {
  description = "IAM role ARN for GitHub Actions (set as AWS_ROLE_ARN secret)"
  value       = aws_iam_role.github_actions.arn
}

output "security_group_id" {
  description = "Security group ID for ECS tasks"
  value       = aws_security_group.ecs_task.id
}

output "subnet_ids" {
  description = "Default VPC subnet IDs for ECS tasks"
  value       = join(",", data.aws_subnets.default.ids)
}

output "state_machine_arn" {
  description = "Step Functions state machine ARN (set as STATE_MACHINE_ARN secret)"
  value       = aws_sfn_state_machine.pipeline.arn
}

output "iam_user_passwords" {
  description = "Initial passwords for developer IAM users (terraform output -json iam_user_passwords)"
  value       = { for k, v in aws_iam_user_login_profile.developers : k => v.password }
  sensitive   = true
}
