"""Request serializers for wallet ledger operations."""

from decimal import Decimal

from rest_framework import serializers


class TransferRequestSerializer(serializers.Serializer):
    """Validate the input required to transfer funds between wallets."""

    source_wallet_id = serializers.UUIDField(required=True)
    dest_wallet_id = serializers.UUIDField(required=True)
    amount = serializers.DecimalField(
        max_digits=18,
        decimal_places=4,
        min_value=Decimal("0.0001"),
        required=True,
    )
    description = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default="",
    )

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if attrs["source_wallet_id"] == attrs["dest_wallet_id"]:
            raise serializers.ValidationError(
                "Source and destination wallets must be different."
            )
        return attrs
