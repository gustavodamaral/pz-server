variable "aws_region" {
  description = "AWS Region in which to provision the server."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be a valid AWS Region identifier."
  }
}

variable "project_name" {
  description = "Stable project tag and resource-name prefix used by lifecycle scripts."
  type        = string
  default     = "pz-server"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", var.project_name))
    error_message = "project_name must contain 3-32 lowercase letters, numbers, or hyphens."
  }
}

variable "environment" {
  description = "Stable environment tag used by lifecycle scripts."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", var.environment))
    error_message = "environment must contain 3-32 lowercase letters, numbers, or hyphens."
  }

  validation {
    condition     = length(var.project_name) + length(var.environment) <= 59
    error_message = "project_name and environment must total at most 59 characters so the derived IAM role name fits AWS's 64-character limit."
  }
}

variable "availability_zone" {
  description = "Optional fixed AZ. It must offer both configured instance types; otherwise the first compatible AZ is selected deterministically."
  type        = string
  default     = null
  nullable    = true
}

variable "vpc_cidr" {
  description = "IPv4 CIDR for the dedicated VPC."
  type        = string
  default     = "10.42.0.0/16"

  validation {
    condition = try(
      can(cidrnetmask(var.vpc_cidr)) &&
      tonumber(split("/", var.vpc_cidr)[1]) >= 16 &&
      tonumber(split("/", var.vpc_cidr)[1]) <= 28 &&
      split("/", var.vpc_cidr)[0] == cidrhost(var.vpc_cidr, 0) &&
      can(regex("^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.)", cidrhost(var.vpc_cidr, 0))),
      false
    )
    error_message = "vpc_cidr must be a canonical private RFC 1918 IPv4 CIDR with a /16 through /28 prefix."
  }
}

variable "public_subnet_cidr" {
  description = "IPv4 CIDR for the public game-server subnet."
  type        = string
  default     = "10.42.1.0/24"

  validation {
    condition = try(
      can(cidrnetmask(var.public_subnet_cidr)) &&
      tonumber(split("/", var.public_subnet_cidr)[1]) >= 16 &&
      tonumber(split("/", var.public_subnet_cidr)[1]) <= 28 &&
      split("/", var.public_subnet_cidr)[0] == cidrhost(var.public_subnet_cidr, 0),
      false
    )
    error_message = "public_subnet_cidr must be a canonical AWS IPv4 subnet CIDR with a /16 through /28 prefix."
  }

}

variable "allowed_game_cidrs" {
  description = "IPv4 networks allowed to reach gameplay UDP ports. Restrict this when players have stable egress CIDRs."
  type        = set(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition     = length(var.allowed_game_cidrs) > 0 && alltrue([for cidr in var.allowed_game_cidrs : can(cidrnetmask(cidr))])
    error_message = "allowed_game_cidrs must contain at least one valid IPv4 CIDR."
  }
}

variable "normal_instance_type" {
  description = "Normal-session EC2 type. Runtime helpers may switch back to this type while stopped."
  type        = string
  default     = "r7a.large"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*\\.[a-z0-9]+$", var.normal_instance_type))
    error_message = "normal_instance_type must look like r7a.large."
  }
}

variable "party_instance_type" {
  description = "Optional larger EC2 type selected manually for party sessions while the instance is stopped."
  type        = string
  default     = "m7a.xlarge"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*\\.[a-z0-9]+$", var.party_instance_type))
    error_message = "party_instance_type must look like m7a.xlarge."
  }
}

variable "root_volume_size_gib" {
  description = "Disposable encrypted root volume size in GiB."
  type        = number
  default     = 24

  validation {
    condition     = var.root_volume_size_gib >= 16 && var.root_volume_size_gib <= 200
    error_message = "root_volume_size_gib must be between 16 and 200."
  }
}

variable "data_volume_size_gib" {
  description = "Protected persistent Project Zomboid gp3 volume size in GiB."
  type        = number
  default     = 40

  validation {
    condition     = var.data_volume_size_gib >= 20 && var.data_volume_size_gib <= 16384
    error_message = "data_volume_size_gib must be between 20 and 16384."
  }
}

variable "data_volume_snapshot_id" {
  description = "Optional EBS snapshot from which to create the initial persistent data volume."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.data_volume_snapshot_id == null || can(regex("^snap-[0-9a-f]+$", var.data_volume_snapshot_id))
    error_message = "data_volume_snapshot_id must be null or a valid snapshot ID."
  }
}

variable "initialize_blank_data_volume" {
  description = "Explicit one-time permission to format an attached signature-free volume. Set true only for initial creation, then immediately return it to false."
  type        = bool
  default     = false
}

variable "ebs_kms_key_id" {
  description = "Optional customer-managed KMS key ARN/ID. Null uses the AWS-managed EBS key."
  type        = string
  default     = null
  nullable    = true
}

variable "repository_url" {
  description = "Public HTTPS Git repository cloned by cloud-init. Do not embed credentials in this URL."
  type        = string
  default     = "https://github.com/gustavodamaral/pz-server.git"

  validation {
    condition     = can(regex("^https://[^@]+$", var.repository_url))
    error_message = "repository_url must be a credential-free HTTPS URL."
  }
}

variable "repository_ref" {
  description = "Exact 40-character Git commit SHA used only when bootstrapping a newly created EC2 host. Use Deploy-PZ.ps1 for routine deployments."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{40}$", var.repository_ref))
    error_message = "repository_ref must be an immutable 40-character Git commit SHA."
  }
}

variable "allow_instance_replacement" {
  description = "Persist one-time provider authorization to remove EC2 termination protection from the current instance during its next deletion. Use only in the documented staged replacement workflow."
  type        = bool
  default     = false
}

variable "enable_cost_alerts" {
  description = "Create a notification-only, account-wide monthly AWS cost budget. No automated budget actions are created."
  type        = bool
  default     = false
}

variable "billing_alert_email" {
  description = "Direct email subscriber for AWS Budget alerts. Required when enable_cost_alerts is true."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.billing_alert_email == null || can(regex("^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$", var.billing_alert_email))
    error_message = "billing_alert_email must be null or a valid email address."
  }

  validation {
    condition     = !var.enable_cost_alerts || var.billing_alert_email != null
    error_message = "billing_alert_email is required when enable_cost_alerts is true."
  }
}

variable "billing_warning_usd" {
  description = "Absolute monthly USD threshold for warning-level actual and forecast AWS Budget notifications."
  type        = number
  default     = 7

  validation {
    condition     = var.billing_warning_usd > 0
    error_message = "billing_warning_usd must be greater than zero."
  }
}

variable "billing_critical_usd" {
  description = "Absolute monthly USD threshold and limit for critical actual and forecast AWS Budget notifications."
  type        = number
  default     = 10

  validation {
    condition     = var.billing_critical_usd > var.billing_warning_usd
    error_message = "billing_critical_usd must be greater than billing_warning_usd."
  }
}

variable "enable_detailed_monitoring" {
  description = "Enable one-minute EC2 detailed monitoring. Disabled by default to avoid extra cost."
  type        = bool
  default     = false
}

variable "additional_tags" {
  description = "Additional tags merged into all supported resources."
  type        = map(string)
  default     = {}

  validation {
    condition = length(setintersection(
      toset(keys(var.additional_tags)),
      toset(["Project", "ManagedBy", "Environment", "Name", "Role", "DataClass", "PZNormalInstanceType", "PZPartyInstanceType"])
    )) == 0
    error_message = "additional_tags cannot override tags reserved for lifecycle, discovery, or data classification."
  }
}
