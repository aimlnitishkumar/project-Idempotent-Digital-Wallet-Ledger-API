"""Run a live HTTP and PostgreSQL demonstration of the wallet transfer API."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Any

import requests
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

BASE_URL = "http://127.0.0.1:8000"
TRANSFER_PATH = "/api/v1/wallets/transfer/"
TRANSFER_URL = f"{BASE_URL}{TRANSFER_PATH}"
TRANSFER_KEY = "DEMO-TX-001"
REPLAY_MAX_SECONDS = 2.0
RACE_KEYS = tuple(f"DEMO-TX-RACE-{number:02d}" for number in range(1, 21))
DEMO_KEYS = (TRANSFER_KEY, *RACE_KEYS)
WALLET_NAMESPACE = uuid.UUID("e2f0e407-6958-48db-a54e-614c77f00242")
ALICE_OWNER_ID = uuid.uuid5(WALLET_NAMESPACE, "Examiner_Alice")
BOB_OWNER_ID = uuid.uuid5(WALLET_NAMESPACE, "Examiner_Bob")
ALICE_WALLET_ID = uuid.uuid5(WALLET_NAMESPACE, "Wallet_A")
BOB_WALLET_ID = uuid.uuid5(WALLET_NAMESPACE, "Wallet_B")
DEMO_WALLET_IDS = (ALICE_WALLET_ID, BOB_WALLET_ID)

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
INFO = "\033[94m[INFO]\033[0m"


class DemoFailure(Exception):
    """An expected failure that should be reported cleanly to the examiner."""


def header(title: str) -> None:
    print(f"\n+{'-' * 74}+")
    print(f"| {title:<72} |")
    print(f"+{'-' * 74}+")


def info(message: str) -> None:
    print(f"{INFO} {message}")


def passed(message: str) -> None:
    print(f"{PASS} {message}")


def failed(message: str) -> None:
    print(f"{FAIL} {message}", flush=True)


def pause() -> None:
    time.sleep(0.5)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise DemoFailure(message)


def preflight() -> None:
    info(f"Checking Django server at {BASE_URL} ...")
    try:
        response = requests.get(BASE_URL, timeout=3)
    except requests.RequestException as exc:
        raise DemoFailure(
            "Django server is unreachable at http://127.0.0.1:8000. "
            "Start the API server and run this script again."
        ) from exc

    info(f"Server responded with HTTP {response.status_code}.")
    try:
        route_response = requests.options(TRANSFER_URL, timeout=3)
    except requests.RequestException as exc:
        raise DemoFailure(
            f"Could not check the transfer route {TRANSFER_PATH}: {exc}"
        ) from exc
    check(
        route_response.status_code != 404,
        (
            f"The server is live, but {TRANSFER_PATH} returned HTTP 404. "
            "The transfer API route must be implemented before this demo can run."
        ),
    )
    if route_response.status_code >= 500:
        raise DemoFailure(
            f"Transfer endpoint pre-flight returned HTTP "
            f"{route_response.status_code}."
        )
    passed("Server and transfer route are reachable.")


def setup_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def reset_and_seed_wallets() -> tuple[Any, Any]:
    from django.db import transaction

    from apps.ledger.models import (
        IdempotencyRecord,
        LedgerEntry,
        LedgerTransaction,
        Wallet,
    )

    with transaction.atomic():
        existing_wallets = Wallet.objects.filter(id__in=DEMO_WALLET_IDS)
        for wallet in existing_wallets:
            expected_owner = (
                ALICE_OWNER_ID
                if wallet.id == ALICE_WALLET_ID
                else BOB_OWNER_ID
            )
            check(
                wallet.owner_id == expected_owner,
                (
                    f"Refusing to reset wallet {wallet.id}: its owner does not "
                    "match the demo owner."
                ),
            )

        unrelated_entries = LedgerEntry.objects.filter(
            wallet_id__in=DEMO_WALLET_IDS
        ).exclude(transaction__idempotency_key__in=DEMO_KEYS)
        check(
            not unrelated_entries.exists(),
            (
                "Refusing to reset demo wallets that contain ledger entries "
                "outside this script's known demo idempotency keys."
            ),
        )

        demo_transactions = LedgerTransaction.objects.filter(
            idempotency_key__in=DEMO_KEYS
        )
        unrelated_wallet_entries = LedgerEntry.objects.filter(
            transaction__in=demo_transactions
        ).exclude(wallet_id__in=DEMO_WALLET_IDS)
        check(
            not unrelated_wallet_entries.exists(),
            (
                "Refusing to reset a demo transaction that also references "
                "a non-demo wallet."
            ),
        )

        transaction_ids = list(demo_transactions.values_list("id", flat=True))
        LedgerEntry.objects.filter(transaction_id__in=transaction_ids).delete()
        demo_transactions.delete()
        IdempotencyRecord.objects.filter(key__in=DEMO_KEYS).delete()
        Wallet.objects.filter(id__in=DEMO_WALLET_IDS).delete()

        alice = Wallet.objects.create(
            id=ALICE_WALLET_ID,
            owner_id=ALICE_OWNER_ID,
            currency="USD",
            balance=Decimal("100.0000"),
        )
        bob = Wallet.objects.create(
            id=BOB_WALLET_ID,
            owner_id=BOB_OWNER_ID,
            currency="USD",
            balance=Decimal("20.0000"),
        )
    return alice, bob


def display_wallets(
    wallets: list[tuple[str, uuid.UUID, Decimal]],
    title: str,
) -> None:
    print(title)
    print("+----------------+--------------------------------------+----------+")
    print("| Wallet         | UUID                                 | Balance  |")
    print("+----------------+--------------------------------------+----------+")
    for label, wallet_id, balance in wallets:
        print(f"| {label:<14} | {wallet_id} | ${balance:>7} |")
    print("+----------------+--------------------------------------+----------+")


def post_transfer(
    key: str,
    payload: dict[str, str],
    barrier: Barrier | None = None,
) -> requests.Response:
    if barrier is not None:
        barrier.wait(timeout=15)
    try:
        return requests.post(
            TRANSFER_URL,
            headers={"Idempotency-Key": key},
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise DemoFailure(f"HTTP request failed for idempotency key {key}: {exc}") from exc


def response_payload(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def balance_for(wallet_id: uuid.UUID) -> Decimal:
    from apps.ledger.models import Wallet

    return Wallet.objects.get(id=wallet_id).balance


def scenario_transfer(alice: Any, bob: Any) -> requests.Response:
    from apps.ledger.models import LedgerEntry

    header("SCENARIO 1 - ATOMIC DOUBLE-ENTRY TRANSFER")
    payload = {
        "source_wallet_id": str(alice.id),
        "dest_wallet_id": str(bob.id),
        "amount": "30.0000",
        "description": "Viva demo transfer Alice to Bob",
    }
    response = post_transfer(TRANSFER_KEY, payload)
    check(
        response.status_code in (200, 201),
        (
            f"Expected HTTP 200/201 for the transfer, received "
            f"{response.status_code}: {response_payload(response)}"
        ),
    )
    passed(f"Transfer returned HTTP {response.status_code}.")
    info(f"API response: {response_payload(response)}")

    alice_balance = balance_for(alice.id)
    bob_balance = balance_for(bob.id)
    check(
        alice_balance == Decimal("70.0000")
        and bob_balance == Decimal("50.0000"),
        f"Unexpected balances after transfer: Alice={alice_balance}, Bob={bob_balance}.",
    )
    display_wallets(
        [
            ("Alice", alice.id, alice_balance),
            ("Bob", bob.id, bob_balance),
        ],
        "Balances read back from PostgreSQL:",
    )

    entries = list(
        LedgerEntry.objects.filter(
            transaction__idempotency_key=TRANSFER_KEY
        ).order_by("entry_type")
    )
    check(len(entries) == 2, f"Expected 2 ledger entries, found {len(entries)}.")
    check(
        {entry.entry_type for entry in entries} == {"DEBIT", "CREDIT"}
        and all(entry.amount == Decimal("30.0000") for entry in entries),
        "The transfer did not create equal debit and credit entries.",
    )
    print("\nLedger entries:")
    print("+----------+--------------------------------------+----------+--------------+")
    print("| Type     | Wallet UUID                          | Amount   | Balance After|")
    print("+----------+--------------------------------------+----------+--------------+")
    for entry in entries:
        print(
            f"| {entry.entry_type:<8} | {str(entry.wallet_id)} | "
            f"${entry.amount:>7} | ${entry.balance_after:>11} |"
        )
    print("+----------+--------------------------------------+----------+--------------+")
    passed("Debit and credit entries balance at $30.0000.")
    pause()
    return response


def scenario_replay(first_response: requests.Response, alice: Any, bob: Any) -> None:
    from django.utils import timezone

    from apps.ledger.models import IdempotencyRecord

    header("SCENARIO 2 - IDEMPOTENCY REPLAY")
    payload = {
        "source_wallet_id": str(alice.id),
        "dest_wallet_id": str(bob.id),
        "amount": "30.0000",
        "description": "Viva demo transfer Alice to Bob",
    }
    started_at = time.perf_counter()
    response = post_transfer(TRANSFER_KEY, payload)
    replay_seconds = time.perf_counter() - started_at
    replay_observed_at = timezone.now()
    check(
        response.status_code in (200, 201),
        (
            f"Expected HTTP 200/201 for replay, received {response.status_code}: "
            f"{response_payload(response)}"
        ),
    )
    check(
        replay_seconds < REPLAY_MAX_SECONDS,
        (
            f"Idempotency replay took {replay_seconds:.3f}s, exceeding the "
            f"{REPLAY_MAX_SECONDS:.1f}s demo limit."
        ),
    )
    check(
        response.status_code == first_response.status_code
        and response_payload(response) == response_payload(first_response),
        "The replay did not return the originally cached response.",
    )
    alice_balance = balance_for(alice.id)
    bob_balance = balance_for(bob.id)
    check(
        alice_balance == Decimal("70.0000")
        and bob_balance == Decimal("50.0000"),
        (
            "Replay changed wallet balances: "
            f"Alice={alice_balance}, Bob={bob_balance}."
        ),
    )

    record = IdempotencyRecord.objects.get(key=TRANSFER_KEY)
    cached_response = json.dumps(
        record.response_body,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    cached_response_hash = hashlib.sha256(cached_response).hexdigest()
    passed("Replay returned the cached response without another debit.")
    print(f"  Cached response SHA-256 : {cached_response_hash}")
    print(f"  Stored request SHA-256  : {record.request_hash}")
    print(f"  Idempotency record time : {record.created_at.isoformat()}")
    print(f"  Replay observed at      : {replay_observed_at.isoformat()}")
    print(f"  Replay response time    : {replay_seconds:.3f}s")
    info("Replay time is observed by this runner; the API schema stores no replay timestamp.")
    pause()


def scenario_tamper(alice: Any, bob: Any) -> None:
    header("SCENARIO 3 - IDEMPOTENCY PAYLOAD TAMPER DETECTION")
    payload = {
        "source_wallet_id": str(alice.id),
        "dest_wallet_id": str(bob.id),
        "amount": "50.0000",
        "description": "Viva demo transfer Alice to Bob",
    }
    response = post_transfer(TRANSFER_KEY, payload)
    check(
        response.status_code in (409, 422),
        (
            f"Expected HTTP 422 or 409 for a changed payload, received "
            f"{response.status_code}: {response_payload(response)}"
        ),
    )
    passed(f"Tampered payload rejected with HTTP {response.status_code}.")
    info(f"Server error payload: {response_payload(response)}")
    pause()


def scenario_race(alice: Any, bob: Any) -> None:
    header("SCENARIO 4 - HIGH-CONCURRENCY RACE CONDITION")
    info(
        "Simulating 20 concurrent threads attempting to transfer $10 "
        "simultaneously from Alice's remaining $70.00."
    )
    barrier = Barrier(len(RACE_KEYS))
    payload = {
        "source_wallet_id": str(alice.id),
        "dest_wallet_id": str(bob.id),
        "amount": "10.0000",
        "description": "Viva concurrent race test",
    }
    with ThreadPoolExecutor(max_workers=len(RACE_KEYS)) as executor:
        futures = [
            executor.submit(post_transfer, key, payload, barrier)
            for key in RACE_KEYS
        ]
        responses = [future.result() for future in futures]

    successful = [response for response in responses if response.status_code in (200, 201)]
    insufficient = [response for response in responses if response.status_code == 400]
    unexpected = [
        response
        for response in responses
        if response.status_code not in (200, 201, 400)
    ]
    print("Concurrent request results:")
    status_counts: dict[int, int] = {}
    for response in responses:
        status_counts[response.status_code] = status_counts.get(response.status_code, 0) + 1
    for status_code, count in sorted(status_counts.items()):
        print(f"  HTTP {status_code}: {count}")
    check(
        not unexpected,
        (
            "Unexpected concurrent response(s): "
            + "; ".join(
                f"HTTP {response.status_code}: {response_payload(response)}"
                for response in unexpected
            )
        ),
    )
    check(
        len(successful) == 7 and len(insufficient) == 13,
        (
            f"Expected 7 successes and 13 insufficient-funds failures; got "
            f"{len(successful)} successes and {len(insufficient)} HTTP 400s."
        ),
    )
    check(
        all(
            "insufficient" in str(response_payload(response)).lower()
            for response in insufficient
        ),
        "At least one HTTP 400 response did not identify insufficient funds.",
    )

    alice_balance = balance_for(alice.id)
    bob_balance = balance_for(bob.id)
    check(
        alice_balance == Decimal("0.0000"),
        f"Alice's final balance should be $0.0000, got ${alice_balance}.",
    )
    check(
        bob_balance == Decimal("120.0000"),
        f"Bob's final balance should be $120.0000, got ${bob_balance}.",
    )
    passed("Exactly 7 transfers succeeded and 13 failed for insufficient funds.")
    display_wallets(
        [
            ("Alice", alice.id, alice_balance),
            ("Bob", bob.id, bob_balance),
        ],
        "Final balances read back from PostgreSQL:",
    )
    pause()


def scenario_ledger_audit() -> None:
    from django.db import connection

    header("SCENARIO 5 - MATHEMATICAL ZERO-SUM LEDGER AUDIT")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                SUM(CASE WHEN entry_type='DEBIT' THEN amount ELSE 0 END) AS debits,
                SUM(CASE WHEN entry_type='CREDIT' THEN amount ELSE 0 END) AS credits
            FROM ledger_ledgerentry;
            """
        )
        debits, credits = cursor.fetchone()

    check(
        debits is not None and credits is not None and debits == credits,
        f"Ledger is not zero-sum: debits={debits}, credits={credits}.",
    )
    print(f"  Total debits  : ${debits}")
    print(f"  Total credits : ${credits}")
    passed("All posted debit and credit totals match.")
    pause()


def main() -> int:
    header("IDEMPOTENT DIGITAL WALLET & LEDGER API - LIVE VIVA DEMO")
    try:
        preflight()
        setup_django()
        alice, bob = reset_and_seed_wallets()
        header("PRE-FLIGHT - RESET AND SEED DEMO WALLETS")
        display_wallets(
            [
                ("Alice", alice.id, alice.balance),
                ("Bob", bob.id, bob.balance),
            ],
            "Initial seeded state:",
        )
        pause()

        first_response = scenario_transfer(alice, bob)
        scenario_replay(first_response, alice, bob)
        scenario_tamper(alice, bob)
        scenario_race(alice, bob)
        scenario_ledger_audit()
    except DemoFailure as exc:
        failed(str(exc))
        return 1
    except (ImportError, OSError) as exc:
        failed(f"Demo setup failed: {exc}")
        return 1
    except (DatabaseError, ImproperlyConfigured) as exc:
        failed(f"Django/PostgreSQL setup failed: {exc}")
        return 1

    print(
        f"\n{PASS} Zero Race Conditions | Zero Double-Spend | Audit Trail Verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
