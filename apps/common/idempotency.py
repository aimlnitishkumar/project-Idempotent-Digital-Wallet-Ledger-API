"""Request idempotency support for Django REST Framework views."""

from __future__ import annotations

import hashlib
import json
import logging
from functools import wraps
from typing import Any, Callable, ParamSpec

from django.http import HttpRequest
from django_redis import get_redis_connection
from redis.exceptions import RedisError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer

from apps.ledger.models import IdempotencyRecord

P = ParamSpec("P")
logger = logging.getLogger(__name__)

_LOCK_TTL_SECONDS = 120
_UNLOCK_IF_OWNED = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


def idempotent_operation(
    view_func: Callable[P, Response],
) -> Callable[P, Response]:
    """Require and persist idempotency for POST and PUT DRF requests."""

    @wraps(view_func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Response:
        request = next(
            (
                argument
                for argument in args
                if isinstance(argument, (Request, HttpRequest))
            ),
            None,
        )
        if request is None:
            raise TypeError(
                "@idempotent_operation must decorate a view receiving a Django request."
            )

        if request.method not in {"POST", "PUT"}:
            return view_func(*args, **kwargs)

        key = request.headers.get("Idempotency-Key")
        if not key:
            return Response(
                {"detail": "Idempotency-Key header is required."},
                status=400,
            )
        if len(key) > 255:
            return Response(
                {"detail": "Idempotency-Key must be 255 characters or fewer."},
                status=400,
            )

        payload_hash = hashlib.sha256(request.body).hexdigest()
        lock_key = f"lock:idempotency:{key}"

        try:
            redis_client = get_redis_connection("default")
            acquired = redis_client.set(
                lock_key,
                payload_hash,
                nx=True,
                ex=_LOCK_TTL_SECONDS,
            )
        except RedisError:
            logger.exception("Unable to acquire idempotency lock.")
            return Response(
                {"detail": "Idempotency service is temporarily unavailable."},
                status=503,
            )

        if not acquired:
            return Response(
                {
                    "detail": (
                        "A request with this idempotency key is currently processing."
                    )
                },
                status=409,
            )

        try:
            record = IdempotencyRecord.objects.filter(key=key).first()
            if record is not None:
                if record.request_hash != payload_hash:
                    return Response(
                        {
                            "detail": (
                                "Idempotency key reuse with different parameters."
                            )
                        },
                        status=422,
                    )
                return Response(
                    data=record.response_body,
                    status=record.response_code,
                )

            response = view_func(*args, **kwargs)
            if isinstance(response, Response):
                response_body: Any = json.loads(JSONRenderer().render(response.data))
                IdempotencyRecord.objects.create(
                    key=key,
                    request_path=request.path,
                    request_hash=payload_hash,
                    response_code=response.status_code,
                    response_body=response_body,
                )
            return response
        finally:
            try:
                redis_client.eval(_UNLOCK_IF_OWNED, 1, lock_key, payload_hash)
            except RedisError:
                logger.exception("Unable to release idempotency lock; it will expire.")

    return wrapper
