# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Bounded retries for read-only Google Ads queries.

Google Ads sometimes answers a valid query with a transient server error:
gRPC INTERNAL ("Internal error encountered") or UNAVAILABLE. Live testing hit
two in one day. One failed the config sheet sync's budget read and the other
failed a campaign's severe_budget playbook. Neither was retried.

GAQL reads (``search`` and ``search_stream``) are therefore retried a few
times with exponential backoff and jitter. Writes are deliberately not
retried: a mutate that failed with INTERNAL may still have been applied, and
repeating it could apply the change twice. A failed write is reported to the
playbook exactly as before.

``google_ads_service(client)`` returns the client's GoogleAdsService wrapped
so that its two read methods retry. Both return lists (rows for ``search``,
response batches for ``search_stream``) rather than lazy iterators, because a
stream can fail part-way through and only a fully read result can be retried
as a unit. Every other attribute passes through to the real service.
"""

import logging
import random
import time
from typing import Any, Callable, Dict, List, Optional, TypeVar

from agentic_dsta.core import telemetry

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Four attempts in all, waiting about 1s, 2s and 4s between them. That rides
# out a brief blip while adding at most ~8s to a read that keeps failing.
MAX_ATTEMPTS = 4
BASE_DELAY_S = 1.0
MAX_DELAY_S = 8.0
_JITTER = 0.2

# gRPC status codes worth retrying for a read.
_TRANSIENT_STATUS_CODES = frozenset({"INTERNAL", "UNAVAILABLE"})

# Google Ads InternalError values that mean "try again".
_TRANSIENT_INTERNAL_ERRORS = frozenset({"INTERNAL_ERROR", "TRANSIENT_ERROR"})


def _status_name(error: BaseException) -> Optional[str]:
    """Returns the gRPC status code name an error carries, if any.

    Covers GoogleAdsException (its ``error`` is the failed gRPC call), a raw
    grpc.RpcError (which is the call itself) and google.api_core exceptions
    (``grpc_status_code``).

    Args:
        error: The exception raised by the read.

    Returns:
        For example 'INTERNAL', or None if the error has no status code.
    """
    for call in (getattr(error, "error", None), error):
        code = getattr(call, "code", None)
        if not callable(code):
            continue
        try:
            name = getattr(code(), "name", None)
        except Exception:  # pylint: disable=broad-except
            continue
        if isinstance(name, str):
            return name
    name = getattr(getattr(error, "grpc_status_code", None), "name", None)
    return name if isinstance(name, str) else None


def _failure_errors(error: BaseException) -> List[Any]:
    """Returns the GoogleAdsFailure errors of a GoogleAdsException, if any."""
    failure = getattr(error, "failure", None)
    try:
        return list(getattr(failure, "errors", None) or ())
    except TypeError:
        return []


def is_transient_error(error: BaseException) -> bool:
    """Reports whether a failed Google Ads read is worth retrying.

    Args:
        error: The exception raised by the read.

    Returns:
        True for gRPC INTERNAL or UNAVAILABLE, and for Google Ads
        INTERNAL_ERROR or TRANSIENT_ERROR failures. Everything else, including
        quota, authentication and invalid query errors, is final.
    """
    if _status_name(error) in _TRANSIENT_STATUS_CODES:
        return True
    for item in _failure_errors(error):
        internal = getattr(getattr(item, "error_code", None), "internal_error", None)
        if getattr(internal, "name", None) in _TRANSIENT_INTERNAL_ERRORS:
            return True
    return False


def _describe(error: BaseException) -> str:
    """Summarises an error on one line for the logs.

    A GoogleAdsException's str() spans many lines, so its failure messages
    and request id are used instead.
    """
    messages = [getattr(item, "message", None) for item in _failure_errors(error)]
    text = "; ".join(m for m in messages if isinstance(m, str) and m)
    if not text:
        lines = str(error).strip().splitlines()
        text = lines[0] if lines else ""
    status = _status_name(error)
    summary = f"{type(error).__name__}{f' [{status}]' if status else ''}: {text[:300]}"
    request_id = getattr(error, "request_id", None)
    if isinstance(request_id, str) and request_id:
        summary += f" (request_id {request_id})"
    return summary


def _sleep(seconds: float) -> None:
    """Waits between attempts. Looked up at call time so tests can stub it."""
    time.sleep(seconds)


def call_with_retry(
    fn: Callable[[], T],
    *,
    operation: str,
    customer_id: Optional[str] = None,
    max_attempts: int = MAX_ATTEMPTS,
    base_delay_s: float = BASE_DELAY_S,
    sleep: Optional[Callable[[float], None]] = None,
) -> T:
    """Calls ``fn``, retrying transient Google Ads errors with backoff.

    Each retry logs a WARNING ``dependency_retry`` event. No alert is built
    on it: a read that recovers needs no action, and one that never recovers
    raises and is reported by the caller as before.

    Args:
        fn: The read to perform. It must be safe to repeat.
        operation: Name for the logs, for example 'search_stream'.
        customer_id: The account queried, for the logs.
        max_attempts: Total attempts, including the first.
        base_delay_s: Wait before the first retry. It doubles for each later
            retry, up to MAX_DELAY_S, with +/-20% jitter.
        sleep: Waits between attempts. Defaults to time.sleep.

    Returns:
        What ``fn`` returned.

    Raises:
        Exception: ``fn``'s error, when it is not transient or the last
            attempt also failed.
    """
    wait = sleep or _sleep
    attempt = 1
    while True:
        try:
            result = fn()
        except Exception as err:  # pylint: disable=broad-except
            transient = is_transient_error(err)
            if not transient or attempt >= max_attempts:
                if transient:
                    logger.warning(
                        "Google Ads %s still failing after %d attempts; giving up: %s",
                        operation,
                        attempt,
                        _describe(err),
                        extra={"dependency": "google_ads", "operation": operation, "customer_id": customer_id},
                    )
                raise
            # GoogleAdsException's str() is empty, so classify the summary,
            # which carries the failure message and the gRPC status.
            description = _describe(err)
            delay = min(MAX_DELAY_S, base_delay_s * 2 ** (attempt - 1))
            delay *= random.uniform(1 - _JITTER, 1 + _JITTER)
            logger.warning(
                "Transient Google Ads error on %s (attempt %d of %d); retrying in %.1fs: %s",
                operation,
                attempt,
                max_attempts,
                delay,
                description,
                extra=telemetry.event_fields(
                    telemetry.EVENT_DEPENDENCY_RETRY,
                    dependency="google_ads",
                    operation=operation,
                    customer_id=customer_id,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error_class=telemetry.classify_error(description),
                ),
            )
            wait(delay)
            attempt += 1
            continue
        if attempt > 1:
            logger.info(
                "Google Ads %s succeeded on attempt %d of %d",
                operation,
                attempt,
                max_attempts,
                extra={"dependency": "google_ads", "operation": operation, "customer_id": customer_id},
            )
        return result


def _customer_id(kwargs: Dict[str, Any]) -> Optional[str]:
    """The customer_id keyword argument of a read, for the logs."""
    value = kwargs.get("customer_id")
    return value if isinstance(value, str) else None


class RetryingGoogleAdsService:
    """A GoogleAdsService whose GAQL reads retry transient errors.

    ``search`` returns a list of rows and ``search_stream`` a list of response
    batches, so ``for row in ...`` and ``for batch in ...`` loops work as
    before. An error now surfaces from the call itself rather than part-way
    through the loop; every caller makes the call inside the same try block
    as its loop, so their error handling is unchanged.
    """

    def __init__(self, service: Any) -> None:
        """Wraps a GoogleAdsService client.

        Args:
            service: The object returned by client.get_service("GoogleAdsService").
        """
        self._service = service

    def search(self, *args: Any, **kwargs: Any) -> List[Any]:
        """GoogleAdsService.search, fully read, with retries.

        Returns:
            Every GoogleAdsRow from every page.
        """
        return call_with_retry(
            lambda: list(self._service.search(*args, **kwargs)),
            operation="search",
            customer_id=_customer_id(kwargs),
        )

    def search_stream(self, *args: Any, **kwargs: Any) -> List[Any]:
        """GoogleAdsService.search_stream, fully read, with retries.

        Returns:
            Every SearchGoogleAdsStreamResponse batch, in order.
        """
        return call_with_retry(
            lambda: list(self._service.search_stream(*args, **kwargs)),
            operation="search_stream",
            customer_id=_customer_id(kwargs),
        )

    def __getattr__(self, name: str) -> Any:
        # Only called for attributes not found normally, e.g. mutate().
        service = self.__dict__.get("_service")
        if service is None:
            raise AttributeError(name)
        return getattr(service, name)


def google_ads_service(client: Any) -> RetryingGoogleAdsService:
    """Returns the client's GoogleAdsService with retrying reads.

    Args:
        client: A GoogleAdsClient.

    Returns:
        The wrapped GoogleAdsService.
    """
    return RetryingGoogleAdsService(client.get_service("GoogleAdsService"))
