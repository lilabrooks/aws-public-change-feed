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

output "concurrency_bucket_name" {
  description = "Name of the ADR-019 concurrent-promotion suite bucket."
  value       = aws_s3_bucket.concurrency.id
}

output "concurrency_bucket_arn" {
  description = "ARN of the ADR-019 concurrent-promotion suite bucket."
  value       = aws_s3_bucket.concurrency.arn
}

output "concurrency_identity_arn" {
  description = "ARN of the scoped identity the ADR-019 suite runs as."
  value       = aws_iam_user.concurrency_test.arn
}
