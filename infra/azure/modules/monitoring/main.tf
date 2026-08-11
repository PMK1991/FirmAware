resource "azurerm_monitor_action_group" "this" {
  name                = "ag-${var.name_prefix}"
  resource_group_name = var.resource_group_name
  short_name          = substr("fa${var.env}", 0, 12)
  tags                = var.tags

  email_receiver {
    name          = "owner"
    email_address = var.alerts_email
    # Azure's own schema, not the common alert schema, so the mail names the
    # resource and the condition rather than wrapping them in an envelope.
    use_common_alert_schema = true
  }
}

# A failed training pipeline is the alert that matters: it means the gate
# blocked a model, or the contract rejected the data, and nothing was registered.
# Querying the log rather than a metric is what lets the message carry the reason.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "job_failure" {
  name                = "alert-${var.name_prefix}-job-failed"
  location            = var.location
  resource_group_name = var.resource_group_name
  severity            = 1
  scopes              = [var.log_analytics_workspace_id]
  description         = "An Azure ML job failed: a contract violation, a blocked gate, or an infrastructure fault."
  enabled             = true

  evaluation_frequency = "PT15M"
  window_duration      = "PT1H"

  criteria {
    query                   = <<-KQL
      AmlRunStatusChangedEvent
      | where Status in ("Failed", "Canceled")
      | project TimeGenerated, RunId, Status, WorkspaceName
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }

  tags = var.tags
}

# Managed online endpoints cannot scale to zero, so cost accrues whether or not
# anyone calls them. The budget is the control that makes that visible before
# the invoice does.
resource "azurerm_consumption_budget_resource_group" "this" {
  name              = "budget-${var.name_prefix}"
  resource_group_id = var.resource_group_id
  amount            = var.monthly_budget
  time_grain        = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00'Z'", timestamp())
  }

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      enabled        = true
      threshold      = notification.value
      operator       = "GreaterThan"
      threshold_type = "Actual"
      contact_emails = [var.alerts_email]
    }
  }

  lifecycle {
    # start_date is computed from the current time, which would otherwise make
    # every plan show a diff and every apply reset the budget window.
    ignore_changes = [time_period]
  }
}
