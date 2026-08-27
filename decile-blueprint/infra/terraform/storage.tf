# S3 archive, and the EBS snapshot schedule.

resource "aws_s3_bucket" "archive" {
  bucket = var.archive_bucket

  # The raw NSE archive is not reproducible — the exchange does not serve arbitrary history — and
  # the pg_dumps beside it are the recovery path. Root CLAUDE.md's rail about unrebuildable
  # evidence applies to this bucket as much as to portfolio.db.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id
  versioning_configuration {
    # docs/08 §3: "Versioning on". An idempotent re-run that writes a different file is a bug
    # (house rule 7) — versioning is how you can still prove what the first run wrote.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket                  = aws_s3_bucket.archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket     = aws_s3_bucket.archive.id
  depends_on = [aws_s3_bucket_versioning.archive]

  # §3: "lifecycle: archive objects → Standard-IA at 30 days."
  rule {
    id     = "raw-archive-to-ia"
    status = "Enabled"
    filter { prefix = "nse/" }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
  }

  # Backups age out; the archive never does. A pg_dump from March is worth nothing once April's
  # exists and the restore drill has passed, but a bhavcopy from 2011 cannot be re-fetched.
  rule {
    id     = "expire-old-database-dumps"
    status = "Enabled"
    filter { prefix = "backups/" }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    expiration { days = 365 }
  }

  # Versioning keeps every overwrite forever otherwise, which is a bill nobody notices growing.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

# --------------------------------------------------------------------------- snapshots
# docs/08 §3: "plus DLM EBS snapshots (7-day retention)". This is the second leg of the backup
# story; the first is the nightly pg_dump to S3, which the worker does after the 20:15 publish
# check. Two mechanisms because they fail differently: a dump can be silently empty, a snapshot
# cannot; a snapshot cannot be restored into a different Postgres version, a dump can.
resource "aws_iam_role" "dlm" {
  name = "baskfy-dlm"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "dlm.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "daily" {
  # DLM validates this against [0-9A-Za-z _-]+ only. An em dash failed, and so did a comma —
  # `terraform validate` catches both, which is the whole reason this file is validated and not
  # merely read.
  description        = "baskfy Phase A daily root volume snapshot 7 day retention"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]
    # Matches the tag on the root_block_device in compute.tf.
    target_tags = { Backup = "daily" }

    schedule {
      name = "daily-0200-ist"

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        # 20:30 UTC = 02:00 IST. After the nightly chain (18:45 IST), after the publish deadline
        # (20:15 IST) and after the alerts (20:30 IST) — docs/08 §1 — so a snapshot always
        # contains a complete day rather than a half-written one.
        times = ["20:30"]
      }

      retain_rule { count = 7 }

      tags_to_add = {
        SnapshotCreator = "dlm"
        Project         = "baskfy"
      }

      copy_tags = true
    }
  }
}
