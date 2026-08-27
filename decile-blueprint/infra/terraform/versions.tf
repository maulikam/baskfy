# Phase A — `docs/08-aws-architecture.md` §3, as code.
#
# One `t4g.large` in ap-south-1 running the compose stack, an EIP, a 100 GB gp3 volume, an S3
# archive bucket, a Route 53 record, DLM snapshots, and a budget alarm. §7 prices this at ≈$50/mo.
#
# WHAT THIS DELIBERATELY DOES NOT DO
# ----------------------------------
# No RDS, no ECS, no ALB, no NAT gateway. Those are Phase B (§5) and cost four times as much;
# §7's closing line is the reason: "nothing above $75/month exists until the SEBI gate is passed".
# The budget alarm below is set at exactly that number so the moment it stops being true, someone
# is told.
#
# It also creates **no order-path IP registration**. §3 says the EIP is the "future SECONDARY
# order IP" and §5 spells out the choreography — register as secondary, shadow, promote, retire —
# with one change per week allowed. That is an operator sequence against Zerodha's console, not
# something Terraform can or should do.

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # Uncomment once the bucket exists. Chicken-and-egg: the first apply necessarily uses local
  # state because the bucket it would store state in does not exist yet. Create it, then migrate
  # with `terraform init -migrate-state`. `use_lockfile` is S3-native locking (provider 5.70+),
  # which replaces the DynamoDB table the older pattern needed.
  #
  # backend "s3" {
  #   bucket       = "baskfy-tfstate"
  #   key          = "phase-a/terraform.tfstate"
  #   region       = "ap-south-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "baskfy"
      Phase     = "A"
      ManagedBy = "terraform"
      Source    = "decile-blueprint/infra/terraform"
    }
  }
}
