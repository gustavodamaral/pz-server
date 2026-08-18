locals {
  cloud_config = templatefile("${path.module}/cloud-init/cloud-config.yaml.tftpl", {
    bootstrap_script_base64 = base64encode(file("${path.module}/cloud-init/bootstrap.sh"))
    data_volume_id_base64   = base64encode(aws_ebs_volume.world.id)
    initialize_data_volume  = var.initialize_blank_data_volume
    repository_ref_base64   = base64encode(var.repository_ref)
    repository_url_base64   = base64encode(var.repository_url)
  })
}

resource "aws_instance" "server" {
  ami                                  = data.aws_ami.ubuntu.id
  instance_type                        = var.normal_instance_type
  availability_zone                    = local.availability_zone
  subnet_id                            = aws_subnet.public.id
  vpc_security_group_ids               = [aws_security_group.game_server.id]
  iam_instance_profile                 = aws_iam_instance_profile.server.name
  associate_public_ip_address          = true
  instance_initiated_shutdown_behavior = "stop"
  monitoring                           = var.enable_detailed_monitoring
  user_data                            = local.cloud_config
  user_data_replace_on_change          = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gib
    iops                  = 3000
    throughput            = 125
    encrypted             = true
    kms_key_id            = var.ebs_kms_key_id
    delete_on_termination = true

    tags = {
      Name      = "${var.project_name}-${var.environment}-root"
      DataClass = "disposable-system"
    }
  }

  tags = local.instance_tags

  lifecycle {
    # Start-PZ.ps1 can safely select Normal/Party while stopped. Terraform should
    # not undo that deliberate runtime choice on the next unrelated apply.
    ignore_changes = [ami, instance_type]
  }

  depends_on = [
    aws_iam_role_policy_attachment.ssm_core,
    aws_route_table_association.public,
  ]
}
