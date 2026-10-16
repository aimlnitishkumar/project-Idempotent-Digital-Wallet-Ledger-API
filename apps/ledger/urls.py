"""URL routes for wallet ledger operations."""

from django.urls import path

from apps.ledger.views import (
    DashboardPageView,
    DashboardSummaryView,
    TransferView,
)

urlpatterns = [
    path("dashboard/summary/", DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("wallets/transfer/", TransferView.as_view(), name="wallet-transfer"),
]
