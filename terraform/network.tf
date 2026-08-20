resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.project_name}-${var.environment}"
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${var.project_name}-${var.environment}"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  availability_zone       = local.availability_zone
  cidr_block              = var.public_subnet_cidr
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.project_name}-${var.environment}-public"
    Tier = "public"
  }

  lifecycle {
    precondition {
      condition     = local.availability_zone_is_compatible
      error_message = local.availability_zone_error
    }

    precondition {
      condition     = local.subnet_is_within_vpc
      error_message = local.subnet_vpc_error
    }
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-public"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
