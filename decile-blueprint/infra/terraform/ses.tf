# Email, per `docs/08-aws-architecture.md` §2: "Email | none needed / SES sandbox" in Phase A,
# "SES production (ap-south-1)" in Phase B.
#
# WHY SES AND NOT RESEND
# ----------------------
# `docs/02` names Resend and the code supports it. SES wins here for one reason that is not about
# either product: it is in the account and region this already runs in, so there is no second
# vendor, no second bill, and no third-party holding a key to send mail as this domain while the
# regulatory posture (D3) is still ⚠ UNREVIEWED. `email/sender.py` reaches it over SMTP, which
# needed STARTTLS and AUTH added — that transport was written for mailpit and could not have
# talked to SES.
#
# THE SANDBOX IS NOT A MISTAKE
# ----------------------------
# A new SES account sends only to *verified* addresses, 200/day. That is exactly right for a
# staging host nobody but Maulik can log into, and it is a guardrail worth keeping until the site
# is public: a misconfigured loop cannot mail strangers. Production access is a support request,
# and it belongs with the decision to go public (runbook §7), not before it.

resource "aws_sesv2_email_identity" "domain" {
  email_identity = var.domain_name

  dkim_signing_attributes {
    # Easy DKIM: AWS holds the private key and publishes three CNAMEs for us to delegate to.
    # The alternative (BYODKIM) means generating and rotating a keypair by hand for no benefit
    # at this scale.
    next_signing_key_length = "RSA_2048_BIT"
  }
}

# A custom MAIL FROM makes SPF align with the visible From: address rather than with
# amazonses.com. Without it DMARC passes only on DKIM, which is one leg instead of two — and
# `baskfy.com` already publishes `p=quarantine`.
resource "aws_sesv2_email_identity_mail_from_attributes" "domain" {
  email_identity   = aws_sesv2_email_identity.domain.email_identity
  mail_from_domain = "mail.${var.domain_name}"

  # If the MX record is missing, fall back to amazonses.com rather than dropping the mail. A
  # verification email that silently fails to send is the worst outcome available here.
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
}

# The recipient for sandbox testing. SES will not deliver to an unverified address while the
# account is in the sandbox, so without this there is nobody to send a test to.
resource "aws_sesv2_email_identity" "operator" {
  email_identity = var.budget_notification_email
}

# --------------------------------------------------------------------------- SMTP credentials
# SES SMTP credentials are an IAM access key put through a documented derivation. The IAM user
# exists only to hold that key and can do exactly one thing.
#
# `aws_iam_access_key` writes the secret into Terraform state. That is why `versions.tf` insists
# the state bucket is encrypted and private, and why the derived SMTP password is never printed:
# `tools/deploy/ses-credentials.sh` computes it and writes it straight into the box's env file.
resource "aws_iam_user" "smtp" {
  name = "baskfy-ses-smtp"
  path = "/service/"
}

resource "aws_iam_user_policy" "smtp" {
  name = "baskfy-ses-send"
  user = aws_iam_user.smtp.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      # Send, and nothing else. Not `ses:*` — this credential lives in a file on a box that
      # reaches the order path, and the blast radius of it leaking should be "someone can send
      # mail as us", not "someone can delete our identities and read our reputation metrics".
      Action   = ["ses:SendRawEmail", "ses:SendEmail"]
      Resource = "*"
      Condition = {
        StringLike = {
          # ...and only as one of ours, so a stolen key cannot send as anyone else.
          #
          # Two entries, not one. `*@baskfy.com` is the end state and covers `no-reply@`. The
          # operator address is here because the domain is not verified until the DKIM CNAMEs are
          # published, and SES in sandbox will only deliver to a verified address anyway — so
          # without it there is no address the system could send from *or* to, and the whole path
          # would stay untestable until a registrar change lands. Narrow it to the domain alone
          # once `aws sesv2 get-email-identity --email-identity baskfy.com` reports SUCCESS.
          "ses:FromAddress" = [
            "*@${var.domain_name}",
            var.budget_notification_email,
          ]
        }
      }
    }]
  })
}

resource "aws_iam_access_key" "smtp" {
  user = aws_iam_user.smtp.name
}

output "ses_dkim_records" {
  description = "Add these three CNAMEs at the registrar, or DKIM never verifies and mail is unsigned."
  value = [
    for token in aws_sesv2_email_identity.domain.dkim_signing_attributes[0].tokens :
    {
      name  = "${token}._domainkey"
      type  = "CNAME"
      value = "${token}.dkim.amazonses.com"
    }
  ]
}

output "ses_mail_from_records" {
  description = "SPF alignment for the custom MAIL FROM subdomain. Also registrar-side."
  value = [
    { name = "mail", type = "MX", value = "10 feedback-smtp.${var.region}.amazonses.com" },
    { name = "mail", type = "TXT", value = "v=spf1 include:amazonses.com ~all" },
  ]
}

output "ses_smtp_username" {
  description = "The SMTP username. The password is derived from the secret and never printed — see tools/deploy/ses-credentials.sh."
  value       = aws_iam_access_key.smtp.id
}
