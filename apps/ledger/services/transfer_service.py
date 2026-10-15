"""Atomic wallet-to-wallet fund transfers."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from django.db import transaction

from apps.ledger.models import LedgerEntry, LedgerTransaction, Wallet


class InsufficientFundsException(Exception):
    """Raised when a source wallet cannot cover a transfer."""


class CurrencyMismatchException(Exception):
    """Raised when a transfer crosses wallet currencies."""


class WalletNotFoundException(Exception):
    """Raised when either wallet in a transfer does not exist."""


class TransferService:
    """Execute atomic, double-entry transfers between wallets."""

    @staticmethod
    def transfer_funds(
        source_wallet_id: UUID,
        dest_wallet_id: UUID,
        amount: Decimal,
        idempotency_key: str,
        description: str = "",
    ) -> LedgerTransaction:
        """Transfer funds and persist the transaction's debit and credit entries."""
        if source_wallet_id == dest_wallet_id:
            raise ValueError("Source and destination wallets must be different.")
        if amount <= Decimal("0"):
            raise ValueError("Transfer amount must be greater than zero.")

        with transaction.atomic():
            sorted_ids = sorted([source_wallet_id, dest_wallet_id])
            wallets = Wallet.objects.select_for_update().filter(
                id__in=sorted_ids
            ).order_by("id")
            wallets_by_id = {wallet.id: wallet for wallet in wallets}

            if len(wallets_by_id) != 2:
                missing_wallet_ids = set(sorted_ids) - wallets_by_id.keys()
                raise WalletNotFoundException(
                    f"Wallet(s) not found: {', '.join(map(str, sorted(missing_wallet_ids)))}"
                )

            source_wallet = wallets_by_id[source_wallet_id]
            dest_wallet = wallets_by_id[dest_wallet_id]

            if source_wallet.currency != dest_wallet.currency:
                raise CurrencyMismatchException(
                    "Source and destination wallets must use the same currency."
                )
            if source_wallet.balance < amount:
                raise InsufficientFundsException(
                    "Source wallet balance is insufficient for this transfer."
                )

            source_wallet.balance -= amount
            dest_wallet.balance += amount
            source_wallet.save(update_fields=["balance", "updated_at"])
            dest_wallet.save(update_fields=["balance", "updated_at"])

            ledger_transaction = LedgerTransaction.objects.create(
                idempotency_key=idempotency_key,
                status=LedgerTransaction.Status.POSTED,
                description=description,
            )
            LedgerEntry.objects.bulk_create(
                [
                    LedgerEntry(
                        transaction=ledger_transaction,
                        wallet=source_wallet,
                        entry_type=LedgerEntry.EntryType.DEBIT,
                        amount=amount,
                        balance_after=source_wallet.balance,
                    ),
                    LedgerEntry(
                        transaction=ledger_transaction,
                        wallet=dest_wallet,
                        entry_type=LedgerEntry.EntryType.CREDIT,
                        amount=amount,
                        balance_after=dest_wallet.balance,
                    ),
                ]
            )

            return ledger_transaction
