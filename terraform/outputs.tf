output "instance_id" {
  description = "Current EC2 instance ID. Helper scripts discover it by tags instead of hardcoding this value."
  value       = aws_instance.server.id
}

output "instance_public_ip" {
  description = "Public IPv4 observed during the last Terraform refresh. Start-PZ.ps1 retrieves the current address after every start."
  value       = aws_instance.server.public_ip
}

output "persistent_volume_id" {
  description = "Protected EBS volume containing Project Zomboid world data."
  value       = aws_ebs_volume.world.id
}

output "availability_zone" {
  description = "AZ shared by the instance and persistent world volume."
  value       = local.availability_zone
}

output "normal_instance_type" {
  description = "Normal mode instance type for the PowerShell helper."
  value       = var.normal_instance_type
}

output "party_instance_type" {
  description = "Party mode instance type for the PowerShell helper."
  value       = var.party_instance_type
}

output "instance_tag_selector" {
  description = "Stable tags used by Windows lifecycle scripts."
  value = {
    Project     = var.project_name
    Environment = var.environment
    Role        = "game-server"
  }
}

output "ssm_session_command" {
  description = "Example Session Manager command; no inbound SSH is provisioned."
  value       = "aws ssm start-session --region ${var.aws_region} --target ${aws_instance.server.id}"
}
