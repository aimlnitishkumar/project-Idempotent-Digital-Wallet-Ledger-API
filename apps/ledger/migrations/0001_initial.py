import uuid
from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Create the initial wallet and ledger schema."""

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="IdempotencyRecord",
            fields=[
                (
                    "key",
                    models.CharField(max_length=255, primary_key=True, serialize=False),
                ),
                ("request_path", models.CharField(max_length=2048)),
                ("request_hash", models.CharField(max_length=64)),
                ("response_code", models.IntegerField()),
                ("response_body", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "idempotency_records",
            },
        ),
        migrations.CreateModel(
            name="LedgerTransaction",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "idempotency_key",
                    models.CharField(
                        db_index=True,
                        max_length=255,
                        unique=True,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("POSTED", "Posted"),
                            ("FAILED", "Failed"),
                        ],
                        default="PENDING",
                        max_length=7,
                    ),
                ),
                ("description", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="Wallet",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("owner_id", models.UUIDField()),
                ("currency", models.CharField(default="USD", max_length=3)),
                (
                    "balance",
                    models.DecimalField(
                        decimal_places=4,
                        default=Decimal("0.0000"),
                        max_digits=18,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["owner_id", "currency"],
                        name="wallet_owner_currency_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("balance__gte", 0)),
                        name="non_negative_balance",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="LedgerEntry",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "entry_type",
                    models.CharField(
                        choices=[("DEBIT", "Debit"), ("CREDIT", "Credit")],
                        max_length=6,
                    ),
                ),
                (
                    "amount",
                    models.DecimalField(decimal_places=4, max_digits=18),
                ),
                (
                    "balance_after",
                    models.DecimalField(decimal_places=4, max_digits=18),
                ),
                (
                    "transaction",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="entries",
                        to="ledger.ledgertransaction",
                    ),
                ),
                (
                    "wallet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ledger_entries",
                        to="ledger.wallet",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("amount__gt", 0)),
                        name="positive_ledger_entry_amount",
                    ),
                ],
            },
        ),
    ]
