variable "region" {
  description = "AWS region. Mumbai is not a preference — docs/08 §1: order placement is legal only from an IP registered with Zerodha, and the desk's CLAUDE.md says 'host in AWS Mumbai for the lowest RTT' about Kite's own floor."
  type        = string
  default     = "ap-south-1"

  validation {
    condition     = var.region == "ap-south-1"
    error_message = "Phase A is ap-south-1. Moving regions moves the egress IP, which is the registered order-path IP (docs/08 §5) — that is an operator sequence with Zerodha, not a variable change."
  }
}

variable "instance_type" {
  description = "docs/08 §3: 't4g.large (8 GB), not medium. Polars factor computation over the full universe and the Celery compute queue want memory headroom; the desk's own sizing doc showed how tight 4 GB gets.'"
  type        = string
  default     = "t4g.large"
}

variable "root_volume_gb" {
  description = "EBS gp3. §7 prices 100 GB at $9.12/mo; the raw NSE archive lives in S3, not here."
  type        = number
  default     = 100
}

variable "domain_name" {
  description = "The apex domain the Route 53 zone is created for."
  type        = string
  default     = "baskfy.com"
}

variable "host_name" {
  description = "The staging host. Gated and noindex until the legal drafts clear review (NEEDS-MAULIK §19), which is why it is not the apex."
  type        = string
  default     = "staging"
}

variable "create_hosted_zone" {
  description = "False if a zone for domain_name already exists — creating a second one silently gives you a second set of nameservers, and whichever the registrar points at wins."
  type        = bool
  default     = true
}

variable "archive_bucket" {
  description = "docs/08 §3: 'S3 from day one, because it is a config value.' Layout: nse/{kind}/{date}.csv, backups/pg/, backups/sqlite/, backtests/."
  type        = string
  default     = "baskfy-archive"
}

variable "budget_limit_usd" {
  description = "docs/08 §7: 'nothing above $75/month exists until the SEBI gate is passed'. The alarm is that sentence, wired up."
  type        = number
  default     = 75
}

variable "budget_notification_email" {
  description = "Where the budget alarm goes. Required — an alarm nobody receives is not an alarm."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_notification_email))
    error_message = "budget_notification_email must be a real address; the budget alarm is the only thing watching the bill."
  }
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach anything but 80/443. Empty by default and it should stay empty — docs/08 §3: 'No SSH keys — SSM Session Manager', which needs no inbound rule at all."
  type        = list(string)
  default     = []
}
