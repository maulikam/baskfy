#!/usr/bin/env bash
# Does this machine have what `terraform apply` needs? Run before the first apply.
#
#     bash tools/deploy/preflight-aws.sh
#
# Every check is read-only. Nothing here creates, modifies or deletes an AWS resource, and it
# never prints a credential — only the account id and the identity's ARN, both of which appear in
# every CloudTrail entry anyway.
#
# It exists because `terraform apply` fails *late*: it will happily create a VPC, a subnet and an
# internet gateway before discovering it cannot create an IAM role, and you are then holding half
# a stack and a state file. Nine seconds of preflight is cheaper than that cleanup.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TF_DIR="$ROOT/decile-blueprint/infra/terraform"
REGION="${AWS_REGION:-ap-south-1}"
OK=0; BAD=0
pass() { printf '  \033[32mok\033[0m   %s\n' "$*"; OK=$((OK+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; BAD=$((BAD+1)); }
note() { printf '       %s\n' "$*"; }

echo "── tooling"
command -v aws >/dev/null 2>&1 && pass "aws cli $(aws --version 2>&1 | cut -d' ' -f1)" \
  || { bad "aws cli not installed"; note "brew install awscli"; }
command -v docker >/dev/null 2>&1 && pass "docker $(docker --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" \
  || bad "docker not installed (terraform and the images both run through it)"

echo "── credentials"
IDENT="$(aws sts get-caller-identity --output json 2>&1)"
if ! grep -q '"Account"' <<<"$IDENT"; then
  bad "no working credentials"
  note "Console login is not CLI login. Pick one:"
  note "  aws configure sso          # recommended: short-lived, nothing on disk"
  note "  aws configure              # access key: works, but a long-lived secret on the laptop"
  echo; echo "preflight: $OK ok, $BAD blocking"; exit 1
fi
ACCOUNT="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["Account"])' <<<"$IDENT")"
ARN="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["Arn"])' <<<"$IDENT")"
pass "authenticated as $ARN"
note "account $ACCOUNT"

echo "── region"
[ "$REGION" = "ap-south-1" ] && pass "region ap-south-1 (Mumbai)" \
  || { bad "region is $REGION, not ap-south-1"; note "docs/08 §1: the order path's registered IP and Kite's RTT both pin this"; }

echo "── permissions (read-only probes)"
# An SCP denial and an IAM denial read almost the same and have nothing in common as problems.
# An IAM denial you fix by editing a policy in your own account. An SCP denial you cannot fix from
# inside the account at all — it is applied by the AWS Organization's management account, and it
# overrides AdministratorAccess. Reporting both as "access denied" sent this straight down the
# wrong path once; the distinction is now in the output.
SCP_HITS=0
probe() { # probe <label> <command...>
  local label="$1"; shift
  if out="$("$@" 2>&1)"; then pass "$label"; return; fi
  if grep -q 'service control policy' <<<"$out"; then
    bad "$label — DENIED BY A SERVICE CONTROL POLICY, not by IAM"
    SCP_HITS=$((SCP_HITS+1))
  elif grep -qiE 'AccessDenied|UnauthorizedOperation|not authorized' <<<"$out"; then
    bad "$label — access denied (IAM)"
  else
    pass "$label"   # a "does not exist" answer still proves the call was permitted
  fi
}
probe "ec2  describe"        aws ec2 describe-vpcs --max-results 5 --region "$REGION"
probe "iam  read roles"      aws iam list-roles --max-items 1
probe "s3   list buckets"    aws s3api list-buckets --max-items 1
probe "r53  list zones"      aws route53 list-hosted-zones --max-items 1
probe "dlm  read policies"   aws dlm get-lifecycle-policies --region "$REGION"
probe "trail describe"       aws cloudtrail describe-trails --region "$REGION"
probe "budgets describe"     aws budgets describe-budgets --account-id "$ACCOUNT" --max-results 1

if [ "$SCP_HITS" -gt 0 ]; then
  echo "── organization"
  ORG="$(aws organizations describe-organization --output json 2>/dev/null)"
  if grep -q MasterAccountId <<<"$ORG"; then
    MASTER="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["Organization"]["MasterAccountId"])' <<<"$ORG")"
    EMAIL="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["Organization"]["MasterAccountEmail"])' <<<"$ORG")"
    if [ "$MASTER" != "$ACCOUNT" ]; then
      bad "this account is a MEMBER of an organization it does not control"
      note "management account $MASTER ($EMAIL) applies the SCP"
      note "Nothing inside this account can override it — not AdministratorAccess, not a new role."
      note "Either the management account changes the SCP, or deploy into an account you own."
    else
      note "you are the management account; edit the SCP yourself in AWS Organizations"
    fi
  fi
fi

echo "── state of the world"
if aws s3api head-bucket --bucket baskfy-archive >/dev/null 2>&1; then
  note "s3://baskfy-archive already exists — terraform will adopt or conflict; check the plan"
else
  note "s3://baskfy-archive does not exist yet (expected on a first apply)"
fi
ZONES="$(aws route53 list-hosted-zones-by-name --dns-name baskfy.com --max-items 1 \
  --query 'HostedZones[?Name==`baskfy.com.`].Id' --output text 2>/dev/null)"
if [ -n "$ZONES" ] && [ "$ZONES" != "None" ]; then
  note "a hosted zone for baskfy.com EXISTS — set create_hosted_zone = false in terraform.tfvars,"
  note "or you get a second zone with different nameservers and a confusing outage"
else
  note "no hosted zone for baskfy.com yet (terraform will create one)"
fi

echo "── deployment inputs"
[ -f "$TF_DIR/terraform.tfvars" ] && pass "terraform.tfvars present" \
  || { bad "no terraform.tfvars"; note "cp $TF_DIR/terraform.tfvars.example $TF_DIR/terraform.tfvars"; }
[ -f "$ROOT/.env.staging.compose" ] && pass ".env.staging.compose present" \
  || note ".env.staging.compose missing — needed on the box, not for terraform (gate-password.sh)"

echo
if [ "$BAD" -eq 0 ]; then
  echo "PREFLIGHT OK — $OK checks passed. Next: terraform plan (runbook §2)."
else
  echo "PREFLIGHT BLOCKED — $BAD of $((OK+BAD)) checks failed."
  exit 1
fi
