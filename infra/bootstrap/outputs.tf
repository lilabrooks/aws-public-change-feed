output "state_bucket_name" {
  description = "Name of the remote state bucket."
  value       = aws_s3_bucket.state.id
}

output "state_bucket_arn" {
  description = "ARN of the remote state bucket."
  value       = aws_s3_bucket.state.arn
}

output "region" {
  description = "Region where the remote state bucket lives."
  value       = var.region
}

output "backend_policy" {
  description = "IAM policy document for the remote state backend principal."
  value       = data.aws_iam_policy_document.backend_principal.json
}
