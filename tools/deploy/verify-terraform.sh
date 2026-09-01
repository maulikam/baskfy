#!/usr/bin/env bash
# G6 — the Phase A Terraform is valid, formatted, and describes what docs/08 §3 specifies.
#
# Terraform is not installed on this machine and this does not install it: it runs the official
# image, so the check works on any machine with Docker and leaves nothing behind. `init` downloads
# the AWS provider (~600 MB) into ./.terraform on the first run and is cached after that.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
TF_DIR="$BLUE/infra/terraform"
TF_IMAGE="hashicorp/terraform:1.9"
tf() { docker run --rm -v "$TF_DIR:/w" -w /w "$TF_IMAGE" "$@"; }

tf fmt -check -recursive >/dev/null 2>&1 || {
  echo "unformatted files:"; tf fmt -list -recursive; fail "run: docker run --rm -v $TF_DIR:/w -w /w $TF_IMAGE fmt -recursive"
}

# `-backend=false` because the S3 backend is commented out until the bucket exists (chicken and
# egg, documented in versions.tf) and validation does not need remote state anyway.
tf init -backend=false -input=false >/dev/null 2>&1 || fail "terraform init failed"
tf validate -no-color >/dev/null 2>&1 || { tf validate -no-color; fail "terraform validate failed"; }

# The shape docs/08 §3 actually asks for. A configuration that validates can still describe the
# wrong architecture, so each line of the spec gets an assertion.
need() { grep -rq -- "$2" "$TF_DIR" || fail "docs/08 §3 wants $1, not found ($2)"; }
need "a t4g.large"                 't4g.large'
need "unlimited burst credits"     'cpu_credits = "unlimited"'
need "a 100 GB gp3 root volume"    'volume_type           = "gp3"'
need "an encrypted root volume"    'encrypted             = true'
need "IMDSv2 required"             'http_tokens                 = "required"'
need "SSM instead of SSH keys"     'AmazonSSMManagedInstanceCore'
need "an Elastic IP"               'aws_eip'
need "S3 versioning"               'status = "Enabled"'
need "Standard-IA at 30 days"      'storage_class = "STANDARD_IA"'
need "DLM daily snapshots"         'aws_dlm_lifecycle_policy'
need "a Route 53 record"           'aws_route53_record'
need "a budget alarm"              'aws_budgets_budget'
need "CloudTrail on"               'aws_cloudtrail'

# And the things that must NOT be there.
#
# Comments are stripped first. The first version of this check matched the line
# `# No \`key_name\`. There is no SSH key to lose` and reported an SSH key pair that does not
# exist — a check that fails on its own documentation teaches people to ignore it.
code_only() { grep -rhv -E '^[[:space:]]*#' "$TF_DIR"/*.tf; }

code_only | grep -q 'key_name' && fail "an SSH key pair is configured; docs/08 §3 is No SSH keys"
if code_only | grep -q 'from_port         = 22'; then
  code_only | grep -q 'for_each = toset(var.admin_cidrs)' \
    || fail "port 22 is open unconditionally rather than behind the empty admin_cidrs list"
fi
code_only | grep -qE 'aws_db_instance|aws_ecs_service|aws_lb\b' \
  && fail "Phase B resources in the Phase A configuration; docs/08 §7 caps this at 75 USD/mo"

# No secret may reach state or a plan output.
code_only | grep -qE '"AKIA[0-9A-Z]{16}"|secret_key[[:space:]]*=|password[[:space:]]*=[[:space:]]*"' \
  && fail "a credential is written into the configuration"

RES="$(grep -rhoE '^resource "[a-z0-9_]+"' "$TF_DIR" | wc -l | tr -d ' ')"
echo "TERRAFORM OK — valid, formatted, ${RES} resources, docs/08 §3 shape asserted"
