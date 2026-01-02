variable "aws_region" {
  type    = string
  default = "us-west-2"
}

variable "project_name" {
  type    = string
  default = "fitness-tracker"
}

variable "user_id" {
  type    = string
  default = "kyle"
}

# Date you start counting toward the yearly goals
variable "start_date" {
  type    = string
  default = "2026-01-01"
}

# Optional lightweight protection: require ?token=... in requests.
# Set to something random (recommended). If empty, token checks are disabled.
variable "secret_token" {
  type    = string
  default = ""
}
