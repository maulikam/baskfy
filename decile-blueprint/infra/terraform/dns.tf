# docs/08 §3: "DNS now, product later: Route 53 hosted zone for baskfy.com (~$0.50/mo) with
# pipeline.baskfy.com on the EIP. desk.modelbasket.in stays exactly where it is."
#
# Nothing here touches modelbasket.in.

resource "aws_route53_zone" "main" {
  count = var.create_hosted_zone ? 1 : 0
  name  = var.domain_name

  # Deleting a zone means the registrar points at nameservers that no longer answer, and the site
  # is gone until DNS propagates a new set — hours, not minutes.
  lifecycle {
    prevent_destroy = true
  }
}

data "aws_route53_zone" "existing" {
  count        = var.create_hosted_zone ? 0 : 1
  name         = var.domain_name
  private_zone = false
}

locals {
  zone_id = var.create_hosted_zone ? aws_route53_zone.main[0].zone_id : data.aws_route53_zone.existing[0].zone_id
  fqdn    = "${var.host_name}.${var.domain_name}"
}

resource "aws_route53_record" "host" {
  zone_id = local.zone_id
  name    = local.fqdn
  type    = "A"
  # Short, because this record moves during the one operation that matters: replacing the box.
  # A 24-hour TTL turns a ten-minute cutover into a day of split traffic.
  ttl     = 300
  records = [aws_eip.box.public_ip]
}

# NO APEX RECORD. `baskfy.com` itself deliberately resolves to nothing.
#
# The four legal drafts under `apps/web/src/content/legal/` are unreviewed (NEEDS-MAULIK §19) and
# D3's regulatory posture is written but UNREVIEWED. Until counsel clears them the product is
# reachable only at the gated staging host. Adding the apex is runbook §7, and it is deliberately
# a separate, reviewed change rather than a variable someone can flip by accident.
