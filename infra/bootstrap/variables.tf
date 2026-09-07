variable "deployment_id" {
  description = "Short identifier for this deployment, used in resource naming."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,62}$", var.deployment_id))
    error_message = "deployment_id must be lowercase alphanumeric and hyphens, 1 to 63 characters."
  }
}

variable "region" {
  description = "AWS region for the remote state bucket."
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = "Supplementary bootstrap tags. Project, deployment_id, managed_by, and component are fixed by Terraform."
  type        = map(string)
  default = {
    project = "aws-public-change-feed"
  }
}
