"""HTTP endpoints for wallet ledger operations."""

import logging
import uuid

from django.db.models import Q, Sum
from django.views.generic import TemplateView
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.idempotency import idempotent_operation
from apps.ledger.serializers import TransferRequestSerializer
from apps.ledger.services.transfer_service import (
    CurrencyMismatchException,
    InsufficientFundsException,
    TransferService,
    WalletNotFoundException,
)
from apps.ledger.models import LedgerEntry, LedgerTransaction, Wallet

logger = logging.getLogger(__name__)
DEMO_NAMESPACE = uuid.UUID("e2f0e407-6958-48db-a54e-614c77f00242")
DEMO_OWNER_NAMES = {
    uuid.uuid5(DEMO_NAMESPACE, "Examiner_Alice"): "Alice",
    uuid.uuid5(DEMO_NAMESPACE, "Examiner_Bob"): "Bob",
}


class DashboardPageView(TemplateView):
    """Render the same-origin wallet operations dashboard."""

    template_name = "ledger/dashboard.html"


class DashboardSummaryView(APIView):
    """Expose wallet balances and recent ledger activity to the dashboard."""

    def get(self, request: Request) -> Response:
        wallets = list(Wallet.objects.order_by("created_at", "id"))
        wallet_names = {
            wallet.id: DEMO_OWNER_NAMES.get(
                wallet.owner_id,
                f"Wallet {str(wallet.id)[:8]}",
            )
            for wallet in wallets
        }
        totals = LedgerEntry.objects.aggregate(
            debits=Sum(
                "amount",
                filter=Q(entry_type=LedgerEntry.EntryType.DEBIT),
            ),
            credits=Sum(
                "amount",
                filter=Q(entry_type=LedgerEntry.EntryType.CREDIT),
            ),
        )

        transactions = (
            LedgerTransaction.objects.order_by("-created_at")
            .prefetch_related("entries__wallet")[:25]
        )
        recent_transactions = []
        for ledger_transaction in transactions:
            entries = list(ledger_transaction.entries.all())
            debit = next(
                (
                    entry
                    for entry in entries
                    if entry.entry_type == LedgerEntry.EntryType.DEBIT
                ),
                None,
            )
            credit = next(
                (
                    entry
                    for entry in entries
                    if entry.entry_type == LedgerEntry.EntryType.CREDIT
                ),
                None,
            )
            recent_transactions.append(
                {
                    "id": str(ledger_transaction.id),
                    "timestamp": ledger_transaction.created_at.isoformat(),
                    "status": ledger_transaction.status,
                    "idempotency_key": ledger_transaction.idempotency_key,
                    "description": ledger_transaction.description,
                    "amount": str(debit.amount) if debit else None,
                    "source": (
                        wallet_names.get(debit.wallet_id, str(debit.wallet_id))
                        if debit
                        else None
                    ),
                    "destination": (
                        wallet_names.get(credit.wallet_id, str(credit.wallet_id))
                        if credit
                        else None
                    ),
                    "debit": str(debit.amount) if debit else None,
                    "credit": str(credit.amount) if credit else None,
                }
            )

        return Response(
            {
                "wallets": [
                    {
                        "id": str(wallet.id),
                        "owner": DEMO_OWNER_NAMES.get(
                            wallet.owner_id,
                            f"Wallet {str(wallet.id)[:8]}",
                        ),
                        "balance": str(wallet.balance),
                        "currency": wallet.currency,
                        "is_active": wallet.is_active,
                        "updated_at": wallet.updated_at.isoformat(),
                    }
                    for wallet in wallets
                ],
                "ledger": {
                    "debits": str(totals["debits"] or 0),
                    "credits": str(totals["credits"] or 0),
                },
                "transactions": recent_transactions,
            }
        )


class TransferView(APIView):
    """Create an atomic, idempotent transfer between two wallets."""

    @method_decorator(idempotent_operation)
    def post(self, request: Request) -> Response:
        serializer = TransferRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        idempotency_key = request.headers.get("Idempotency-Key")
        if not idempotency_key:
            return Response(
                {"detail": "Idempotency-Key header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        try:
            ledger_transaction = TransferService.transfer_funds(
                source_wallet_id=data["source_wallet_id"],
                dest_wallet_id=data["dest_wallet_id"],
                amount=data["amount"],
                idempotency_key=idempotency_key,
                description=data["description"],
            )
        except InsufficientFundsException as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except WalletNotFoundException as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except CurrencyMismatchException as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Unexpected error while processing wallet transfer.")
            return Response(
                {
                    "error": "Transfer processing error",
                    "detail": str(exc),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "status": "SUCCESS",
                "transaction_id": str(ledger_transaction.id),
                "amount": str(data["amount"]),
                "description": ledger_transaction.description,
            },
            status=status.HTTP_201_CREATED,
        )
