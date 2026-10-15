"""Database models for wallets and the immutable financial ledger."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models


class Wallet(models.Model):
    """A currency-specific wallet with a cached, non-negative balance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_id = models.UUIDField()
    currency = models.CharField(max_length=3, default="USD")
    balance = models.DecimalField(
        max_digits=18,
        decimal_places=4,
        default=Decimal("0.0000"),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Database constraints and indexes for wallets."""

        constraints = [
            models.CheckConstraint(
                condition=models.Q(balance__gte=0),
                name="non_negative_balance",
            ),
        ]
        indexes = [
            models.Index(
                fields=("owner_id", "currency"),
                name="wallet_owner_currency_idx",
            ),
        ]


class LedgerTransaction(models.Model):
    """An idempotent operation that groups one or more ledger entries."""

    class Status(models.TextChoices):
        """Lifecycle states for a ledger transaction."""

        PENDING = "PENDING", "Pending"
        POSTED = "POSTED", "Posted"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
    )
    status = models.CharField(
        max_length=7,
        choices=Status.choices,
        default=Status.PENDING,
    )
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class LedgerEntry(models.Model):
    """An immutable debit or credit posted to a wallet."""

    class EntryType(models.TextChoices):
        """The direction of a ledger entry."""

        DEBIT = "DEBIT", "Debit"
        CREDIT = "CREDIT", "Credit"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transaction = models.ForeignKey(
        LedgerTransaction,
        related_name="entries",
        on_delete=models.PROTECT,
    )
    wallet = models.ForeignKey(
        Wallet,
        related_name="ledger_entries",
        on_delete=models.PROTECT,
    )
    entry_type = models.CharField(
        max_length=6,
        choices=EntryType.choices,
    )
    amount = models.DecimalField(max_digits=18, decimal_places=4)
    balance_after = models.DecimalField(max_digits=18, decimal_places=4)

    class Meta:
        """Database constraints for valid ledger entries."""

        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="positive_ledger_entry_amount",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Insert this entry once; reject attempts to update a saved entry."""
        if not self._state.adding:
            raise ValidationError("Ledger entries are immutable and cannot be updated.")
        super().save(*args, **kwargs)


class IdempotencyRecord(models.Model):
    """A persisted response used to replay an idempotent API request."""

    key = models.CharField(max_length=255, primary_key=True)
    request_path = models.CharField(max_length=2048)
    request_hash = models.CharField(max_length=64)
    response_code = models.IntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep the idempotency record table name stable."""

        db_table = "idempotency_records"
