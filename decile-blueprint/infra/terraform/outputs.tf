output "public_ip" {
  description = "The EIP. docs/08 §5: register this with Zerodha as the SECONDARY slot, shadow-execute, promote, and only then retire the old one — never both slots in one calendar week."
  value       = aws_eip.box.public_ip
}

output "host" {
  description = "The gated staging host. Not the apex — see dns.tf."
  value       = local.fqdn
}

output "nameservers" {
  description = "Point the registrar at these, or staging.baskfy.com never resolves and Caddy cannot complete an ACME challenge. NEEDS-MAULIK §21 item 2."
  value       = var.create_hosted_zone ? aws_route53_zone.main[0].name_servers : data.aws_route53_zone.existing[0].name_servers
}

output "archive_bucket" {
  description = "s3://… — raw NSE files, pg_dump, SQLite backups, CloudTrail."
  value       = aws_s3_bucket.archive.id
}

output "ssm_command" {
  description = "How to get a shell. There is no SSH and no key; this is the only way in."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.box.id}"
}

output "estimated_monthly_usd" {
  description = "docs/08 §7's Phase A line, restated so a plan shows it: EC2 32.26 + EBS 9.12 + EIP 3.60 + snapshots ~2 + S3 ~1 + CloudWatch ~2 + Route 53 0.50."
  value       = "~50 (beside the desk's Lightsail ~24 and Kite Connect Rs 500)"
}

output "desk_host" {
  description = "The desk's vhost on the same EIP (SW13, MD20). Caddy obtains its certificate; the desk's basic auth sits behind it."
  value       = aws_route53_record.desk.name
}
