locals {
  billing_alert_emails = [coalesce(var.billing_alert_email, "disabled@example.invalid")]
}

resource "aws_budgets_budget" "monthly_cost" {
  count = var.enable_cost_alerts ? 1 : 0

  name         = "${var.project_name}-${var.environment}-monthly-cost"
  budget_type  = "COST"
  limit_amount = tostring(var.billing_critical_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "ACTUAL"
    threshold                  = var.billing_warning_usd
    threshold_type             = "ABSOLUTE_VALUE"
    subscriber_email_addresses = local.billing_alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "FORECASTED"
    threshold                  = var.billing_warning_usd
    threshold_type             = "ABSOLUTE_VALUE"
    subscriber_email_addresses = local.billing_alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "ACTUAL"
    threshold                  = var.billing_critical_usd
    threshold_type             = "ABSOLUTE_VALUE"
    subscriber_email_addresses = local.billing_alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "FORECASTED"
    threshold                  = var.billing_critical_usd
    threshold_type             = "ABSOLUTE_VALUE"
    subscriber_email_addresses = local.billing_alert_emails
  }
}
