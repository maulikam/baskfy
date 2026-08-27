# One public subnet, one instance, one Elastic IP.
#
# There is no private subnet and no NAT in Phase A, and that is the point: NAT Gateway is ~$46/mo
# (docs/08 §7), which would nearly double a $50 box to buy an isolation boundary that one host
# with one security group does not need. Phase B (§5) adds private subnets and fck-nat when there
# are five services to isolate.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "baskfy-phase-a" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "baskfy-phase-a" }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.20.1.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]

  # The instance gets its address from the EIP below, not from this. Auto-assign would hand out a
  # second, *changing* public IP — and docs/08 §1 requires "one stable egress IP for the order
  # path". Two public addresses on one interface is how that requirement quietly stops holding.
  map_public_ip_on_launch = false

  tags = { Name = "baskfy-phase-a-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "baskfy-phase-a-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "box" {
  name        = "baskfy-phase-a"
  description = "Caddy only. Everything else reaches this box through SSM Session Manager."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "baskfy-phase-a" }
}

# HTTP exists only so Caddy can answer the ACME HTTP-01 challenge and redirect to HTTPS. Closing
# it means no certificate.
resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.box.id
  description       = "ACME HTTP-01 challenge and the redirect to 443"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.box.id
  description       = "Caddy. The gate lives behind this, not instead of it."
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

# NO port 22 RULE, ANYWHERE, ON PURPOSE.
# docs/08 §3: "No SSH keys — SSM Session Manager, IMDSv2 required, security group closed to the
# world except Caddy's 443." SSM works through an outbound-only agent connection, so shell access
# needs no inbound rule and no key to lose. `admin_cidrs` exists for a genuine emergency and
# defaults to empty; if you find yourself filling it in, ask why SSM is not working first.
resource "aws_vpc_security_group_ingress_rule" "admin" {
  for_each = toset(var.admin_cidrs)

  security_group_id = aws_security_group.box.id
  description       = "Break-glass SSH — should normally be empty; prefer SSM"
  cidr_ipv4         = each.value
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.box.id
  description       = "Kite, NSE, ECR, S3, ACME. All of it leaves via the EIP below, which is what makes the order-path IP stable."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# The address docs/08 §1 calls "the registered static IP". It is created here and registered with
# Zerodha by hand, as the SECONDARY slot, following §5's choreography — never both slots in one
# calendar week.
resource "aws_eip" "box" {
  domain   = "vpc"
  instance = aws_instance.box.id

  tags = { Name = "baskfy-phase-a" }

  lifecycle {
    # Losing this address means a week of Zerodha IP-slot choreography to get a new one
    # registered. It must never be replaced to satisfy a diff.
    prevent_destroy = true
  }

  depends_on = [aws_internet_gateway.main]
}
