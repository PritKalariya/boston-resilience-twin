# Email alerts to the billing account's admins at 25, 50 and 80 percent of
# one member's credit. Credits are excluded: education credits would bring
# the net cost to about 0, so an alert on net spend would never fire. This
# tracks usage before credits, which is what draws the credit down.
resource "google_billing_budget" "project_2" {
  provider = google.billing

  billing_account = var.billing_account
  display_name    = "${var.project_id} credit"

  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_amount_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.25
  }
  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.8
  }
  # A warning before the spend happens: Google's forecast for the month crosses 80%.
  threshold_rules {
    threshold_percent = 0.8
    spend_basis       = "FORECASTED_SPEND"
  }

  depends_on = [google_project_service.project_2]
}
