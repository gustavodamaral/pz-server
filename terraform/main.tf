provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(
      {
        Project     = var.project_name
        ManagedBy   = "terraform"
        Environment = var.environment
      },
      var.additional_tags
    )
  }
}

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

data "aws_ec2_instance_type_offerings" "normal" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.normal_instance_type]
  }
}

data "aws_ec2_instance_type_offerings" "party" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.party_instance_type]
  }
}

data "aws_ec2_instance_type" "normal" {
  instance_type = var.normal_instance_type
}

data "aws_ec2_instance_type" "party" {
  instance_type = var.party_instance_type
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  compatible_availability_zones = sort(tolist(setintersection(
    toset(data.aws_availability_zones.available.names),
    toset(data.aws_ec2_instance_type_offerings.normal.locations),
    toset(data.aws_ec2_instance_type_offerings.party.locations),
  )))
  availability_zone               = var.availability_zone != null ? var.availability_zone : try(local.compatible_availability_zones[0], null)
  availability_zone_is_compatible = try(contains(local.compatible_availability_zones, local.availability_zone), false)
  instance_types_support_ami = alltrue([
    contains(data.aws_ec2_instance_type.normal.supported_architectures, "x86_64"),
    contains(data.aws_ec2_instance_type.party.supported_architectures, "x86_64"),
    contains(data.aws_ec2_instance_type.normal.supported_root_device_types, "ebs"),
    contains(data.aws_ec2_instance_type.party.supported_root_device_types, "ebs"),
    contains(data.aws_ec2_instance_type.normal.supported_virtualization_types, "hvm"),
    contains(data.aws_ec2_instance_type.party.supported_virtualization_types, "hvm"),
    contains(data.aws_ec2_instance_type.normal.supported_usages_classes, "on-demand"),
    contains(data.aws_ec2_instance_type.party.supported_usages_classes, "on-demand"),
    data.aws_ec2_instance_type.normal.ebs_encryption_support == "supported",
    data.aws_ec2_instance_type.party.ebs_encryption_support == "supported",
    data.aws_ec2_instance_type.normal.bare_metal == false,
    data.aws_ec2_instance_type.party.bare_metal == false,
    data.aws_ec2_instance_type.normal.memory_size >= 16384,
    data.aws_ec2_instance_type.party.memory_size >= 16384,
  ])
  availability_zone_error = var.availability_zone == null ? (
    "No available AZ in ${var.aws_region} offers both ${var.normal_instance_type} and ${var.party_instance_type}. Choose compatible instance types or another region."
    ) : (
    "availability_zone ${var.availability_zone} is not available for both ${var.normal_instance_type} and ${var.party_instance_type} in ${var.aws_region}. Compatible AZs: ${length(local.compatible_availability_zones) == 0 ? "none" : join(", ", local.compatible_availability_zones)}."
  )
  instance_type_ami_error = "normal_instance_type and party_instance_type must both provide at least 16 GiB RAM and be non-bare-metal types supporting x86_64, encrypted EBS roots, HVM, and On-Demand usage for the selected Ubuntu AMI and stopped-instance resize workflow."
  vpc_network_number = sum([
    for index, octet in split(".", cidrhost(var.vpc_cidr, 0)) : tonumber(octet) * pow(256, 3 - index)
  ])
  vpc_broadcast_number = sum([
    for index, octet in split(".", cidrhost(var.vpc_cidr, -1)) : tonumber(octet) * pow(256, 3 - index)
  ])
  subnet_network_number = sum([
    for index, octet in split(".", cidrhost(var.public_subnet_cidr, 0)) : tonumber(octet) * pow(256, 3 - index)
  ])
  subnet_broadcast_number = sum([
    for index, octet in split(".", cidrhost(var.public_subnet_cidr, -1)) : tonumber(octet) * pow(256, 3 - index)
  ])
  subnet_is_within_vpc = (
    local.subnet_network_number >= local.vpc_network_number &&
    local.subnet_broadcast_number <= local.vpc_broadcast_number
  )
  subnet_vpc_error = "public_subnet_cidr must be fully contained within vpc_cidr."
  instance_tags = {
    Name                 = "${var.project_name}-${var.environment}"
    Role                 = "game-server"
    PZNormalInstanceType = var.normal_instance_type
    PZPartyInstanceType  = var.party_instance_type
  }
}
