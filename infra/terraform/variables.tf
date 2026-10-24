variable "region" {
  description = "AWS region"
  type        = string
  default     = "ap-south-1"
}

variable "project" {
  description = "Name prefix for every resource"
  type        = string
  default     = "opspilot"
}

variable "instance_type" {
  description = "EC2 type. Check free-tier eligibility for your account; use t3.small if memory is tight."
  type        = string
  default     = "t3.micro"
}

variable "my_ip_cidr" {
  description = "Your public IP as CIDR (e.g. 203.0.113.7/32). Only this address can reach SSH and the demo ports."
  type        = string
}

variable "repo_url" {
  description = "Git URL the instance clones"
  type        = string
  default     = "https://github.com/damodharg6/opspilot.git"
}

variable "alert_email" {
  description = "Email for budget alerts and alarm notifications (confirm the SNS subscription email)"
  type        = string
}

variable "budget_limit_usd" {
  description = "Monthly cost budget; alerts at 80% actual and 100% forecast"
  type        = number
  default     = 5
}

variable "error_rate_threshold" {
  description = "5xx error rate (percent) that triggers an investigation"
  type        = number
  default     = 5
}

variable "ssh_key_name" {
  description = "Optional EC2 key pair for SSH. Leave empty to use SSM Session Manager only."
  type        = string
  default     = ""
}

variable "llm_provider" {
  type    = string
  default = "groq"
}

variable "llm_model" {
  type    = string
  default = "llama-3.3-70b-versatile"
}

# --- secrets: stored as SSM SecureString, read by the instance at boot -------------------
variable "llm_api_key" {
  type      = string
  sensitive = true
}

variable "postgres_password" {
  type      = string
  sensitive = true
}

variable "shop_admin_token" {
  type      = string
  sensitive = true
}

variable "opspilot_api_secret" {
  type      = string
  sensitive = true
}
