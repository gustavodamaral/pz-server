locals {
  gameplay_rules = {
    for rule in flatten([
      for cidr in var.allowed_game_cidrs : [
        {
          key         = "game-${replace(cidr, "/", "-")}"
          cidr        = cidr
          port        = 16261
          description = "PZ Build 42 primary gameplay UDP"
        },
        {
          key         = "direct-${replace(cidr, "/", "-")}"
          cidr        = cidr
          port        = 16262
          description = "PZ Build 42 direct-connection UDP"
        }
      ]
    ]) : rule.key => rule
  }
}

resource "aws_security_group" "game_server" {
  name        = "${var.project_name}-${var.environment}"
  description = "Project Zomboid gameplay only; no SSH or RCON ingress"
  vpc_id      = aws_vpc.this.id

  tags = {
    Name = "${var.project_name}-${var.environment}"
  }
}

resource "aws_vpc_security_group_ingress_rule" "gameplay" {
  for_each = local.gameplay_rules

  security_group_id = aws_security_group.game_server.id
  description       = each.value.description
  cidr_ipv4         = each.value.cidr
  from_port         = each.value.port
  to_port           = each.value.port
  ip_protocol       = "udp"
}

resource "aws_vpc_security_group_egress_rule" "internet" {
  security_group_id = aws_security_group.game_server.id
  description       = "SteamCMD, SSM, package updates, and player responses"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
