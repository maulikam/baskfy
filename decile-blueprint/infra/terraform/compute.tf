# The box itself.

data "aws_ami" "al2023_arm64" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-arm64"]
  }
}

# --------------------------------------------------------------------------- identity
# The instance's own role. No access keys exist anywhere in this configuration; anything the box
# needs from AWS it gets from this role via the metadata service.
resource "aws_iam_role" "box" {
  name = "baskfy-phase-a-box"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

# docs/08 §3: "No SSH keys — SSM Session Manager". This managed policy is what makes that work;
# without it there is no way onto the box at all, since nothing opens port 22.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.box.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Exactly the archive bucket, exactly the verbs the app uses. Not `s3:*`, and not `Resource: "*"`:
# this box reaches the order path, and a wildcard here would let a compromised container read
# every bucket in the account.
resource "aws_iam_role_policy" "archive" {
  name = "baskfy-archive-access"
  role = aws_iam_role.box.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.archive.arn
      },
      {
        Effect = "Allow"
        # No `s3:DeleteObject`. The archive is evidence — raw NSE files, pg_dumps, the desk's
        # SQLite backups. Nothing running on this box has a reason to delete any of it, and the
        # lifecycle rules below handle expiry. Versioning is on, so an overwrite is recoverable.
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.archive.arn}/*"
      },
    ]
  })
}

# Pulling its own images. Missed on the first apply, and the failure is unhelpful: `docker login`
# reports "not authorized to perform: ecr:GetAuthorizationToken" rather than anything about the
# instance role, and it happens at deploy time rather than at apply time.
#
# `GetAuthorizationToken` cannot be scoped — it is an account-level action and AWS rejects a
# resource ARN on it. Everything that actually reads an image IS scoped, to these two
# repositories, so a compromised container cannot enumerate or pull anything else in the registry.
resource "aws_iam_role_policy" "ecr_pull" {
  name = "baskfy-ecr-pull"
  role = aws_iam_role.box.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect = "Allow"
        # Read only. Nothing on this box has a reason to push: images are built and pushed from a
        # workstation or CI, which is what makes the deployed artefact reproducible from a commit.
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = [
          "arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/baskfy-web",
          "arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/baskfy-py",
        ]
      },
    ]
  })
}

resource "aws_iam_instance_profile" "box" {
  name = "baskfy-phase-a-box"
  role = aws_iam_role.box.name
}

# --------------------------------------------------------------------------- instance
resource "aws_instance" "box" {
  ami                    = data.aws_ami.al2023_arm64.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.box.id]
  iam_instance_profile   = aws_iam_instance_profile.box.name

  # No `key_name`. There is no SSH key to lose, because there is no SSH.

  # docs/08 §3: "T4g is burstable — leave unlimited mode on for backfill night and accept the
  # few-rupee surcharge rather than a stalled chain."
  credit_specification {
    cpu_credits = "unlimited"
  }

  metadata_options {
    http_endpoint = "enabled"
    # IMDSv2 required, per §3. v1's unauthenticated GET is how an SSRF in any container on this
    # box turns into the instance role's credentials.
    http_tokens                 = "required"
    http_put_response_hop_limit = 2 # containers are one hop further than the host
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = false # the database lives here; see the DLM policy in storage.tf

    tags = {
      Name   = "baskfy-phase-a-root"
      Backup = "daily" # the selector the DLM policy matches on
    }
  }

  user_data                   = file("${path.module}/user-data.sh")
  user_data_replace_on_change = false # editing bootstrap must not rebuild the box under the data

  tags = { Name = "baskfy-phase-a" }

  lifecycle {
    ignore_changes = [
      # A newer AL2023 AMI appears every few weeks. Without this, a routine `terraform apply`
      # would replace the instance — and with it Postgres and every Docker volume — to pick up a
      # patch release. Rebuilding the box is a deliberate act with a backup taken first, which is
      # runbook §6, not a side effect of an unrelated plan.
      ami,
    ]
  }
}
