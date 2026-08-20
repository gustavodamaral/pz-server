resource "aws_ebs_volume" "world" {
  availability_zone = local.availability_zone
  type              = "gp3"
  size              = var.data_volume_size_gib
  iops              = 3000
  throughput        = 125
  encrypted         = true
  kms_key_id        = var.ebs_kms_key_id
  snapshot_id       = var.data_volume_snapshot_id

  tags = {
    Name      = "${var.project_name}-${var.environment}-world"
    DataClass = "persistent-world"
  }

  lifecycle {
    prevent_destroy = true

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

resource "aws_volume_attachment" "world" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.world.id
  instance_id                    = aws_instance.server.id
  force_detach                   = false
  stop_instance_before_detaching = true
}
