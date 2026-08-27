# docs/08 §7: "The shape of the spend is the point: nothing above $75/month exists until the SEBI
# gate is passed." That is a claim about the future, and this is the thing that notices when it
# stops being true. Phase A is priced at ≈$50, so the alarm has room without being noise.

resource "aws_budgets_budget" "monthly" {
  name         = "baskfy-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Actual spend at 80% — early enough to act inside the month.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_notification_email]
  }

  # And a forecast at 100%, which is the one that catches a backfill night or a runaway task
  # *while it is still running* rather than on the first of next month.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_notification_email]
  }
}

# docs/08 §8, P0: "AWS account hygiene: org, ap-south-1 default, CloudTrail on, budgets + billing
# alarms". CloudTrail is the audit log for an account that will hold a broker connection.
resource "aws_cloudtrail" "main" {
  name                          = "baskfy"
  s3_bucket_name                = aws_s3_bucket.archive.id
  s3_key_prefix                 = "cloudtrail"
  include_global_service_events = true
  is_multi_region_trail         = true
  enable_log_file_validation    = true

  depends_on = [aws_s3_bucket_policy.cloudtrail]
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_policy" "cloudtrail" {
  bucket = aws_s3_bucket.archive.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.archive.arn
      },
      {
        Sid       = "AWSCloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.archive.arn}/cloudtrail/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = {
          StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" }
        }
      },
    ]
  })
}
