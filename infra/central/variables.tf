variable "deployment_file" {
  description = "Path to the deployment configuration input, relative to this root."
  type        = string
  default     = "deployment.yaml"
}

variable "enable_dynamodb_point_in_time_recovery" {
  description = "Whether DynamoDB point-in-time recovery is enabled. Chapter 05 leaves the production decision open."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to created resources."
  type        = map(string)
  default = {
    project = "aws-public-change-feed"
  }
}
