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
"""Tests for bounded retries of read-only Google Ads queries.

Reads are safe to repeat; writes are not. So the properties under test are
that a transient read error is retried until it succeeds or the attempts run
out, that every other error fails at once, and that a mutate is never
retried.
"""

import importlib
import logging
import pkgutil
import re
from typing import Any, Iterator, List, Optional
from unittest import mock

import google.ads.googleads as googleads_package
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import exceptions as api_exceptions
import grpc
import pytest

from agentic_dsta.core import telemetry
from agentic_dsta.tools.google_ads import google_ads_asset_groups
from agentic_dsta.tools.google_ads import google_ads_getter
from agentic_dsta.tools.google_ads import google_ads_retry
from agentic_dsta.tools.google_ads import google_ads_updater


class _Call:
    """The failed gRPC call that a GoogleAdsException carries."""

    def __init__(self, code: grpc.StatusCode) -> None:
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code


def _errors_types(module: str) -> Any:
    """Imports a module of the newest Google Ads API version's error types."""
    versions = sorted(
        int(info.name[1:])
        for info in pkgutil.iter_modules(googleads_package.__path__)
        if re.fullmatch(r"v\d+", info.name)
    )
    return importlib.import_module(f"google.ads.googleads.v{versions[-1]}.errors.types.{module}")


def _ads_exception(
    status: grpc.StatusCode = grpc.StatusCode.INTERNAL,
    internal_error: Optional[str] = "INTERNAL_ERROR",
    quota_error: Optional[str] = None,
    message: str = "Internal error encountered.",
) -> GoogleAdsException:
    """Builds a real GoogleAdsException with a real GoogleAdsFailure."""
    errors = _errors_types("errors")
    code = {}
    if internal_error:
        code["internal_error"] = getattr(
            _errors_types("internal_error").InternalErrorEnum.InternalError, internal_error
        )
    if quota_error:
        code["quota_error"] = getattr(_errors_types("quota_error").QuotaErrorEnum.QuotaError, quota_error)
    failure = errors.GoogleAdsFailure(
        errors=[errors.GoogleAdsError(error_code=errors.ErrorCode(**code), message=message)]
    )
    call = _Call(status)
    return GoogleAdsException(call, call, failure, "req-123")


class _RawRpcError(grpc.RpcError):
    """A bare gRPC error, as raised when no GoogleAdsFailure is attached."""

    def __init__(self, code: grpc.StatusCode) -> None:
        super().__init__(code.name)
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code


class _Flaky:
    """A read that raises the given errors in turn, then returns ``result``."""

    def __init__(self, errors: List[BaseException], result: Any = "rows") -> None:
        self.errors = list(errors)
        self.result = result
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


# --- is_transient_error ------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        _ads_exception(grpc.StatusCode.INTERNAL, "INTERNAL_ERROR"),
        _ads_exception(grpc.StatusCode.UNAVAILABLE, None, message="Service unavailable"),
        # A failure that says "transient" counts even under another status.
        _ads_exception(grpc.StatusCode.UNKNOWN, "TRANSIENT_ERROR", message="Try again"),
        _RawRpcError(grpc.StatusCode.INTERNAL),
        _RawRpcError(grpc.StatusCode.UNAVAILABLE),
        api_exceptions.InternalServerError("500 Internal error encountered."),
        api_exceptions.ServiceUnavailable("503 unavailable"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_transient_errors_are_retried(error: BaseException) -> None:
    assert google_ads_retry.is_transient_error(error)


@pytest.mark.parametrize(
    "error",
    [
        _ads_exception(
            grpc.StatusCode.RESOURCE_EXHAUSTED, None, quota_error="RESOURCE_EXHAUSTED", message="Quota"
        ),
        _ads_exception(grpc.StatusCode.INVALID_ARGUMENT, None, message="Bad GAQL"),
        _ads_exception(grpc.StatusCode.UNAUTHENTICATED, None, message="Token expired"),
        _RawRpcError(grpc.StatusCode.DEADLINE_EXCEEDED),
        api_exceptions.ResourceExhausted("429 quota"),
        api_exceptions.PermissionDenied("403 denied"),
        ValueError("Campaign not found"),
        RuntimeError("Failed to get Google Ads client."),
    ],
    ids=lambda e: type(e).__name__,
)
def test_other_errors_are_final(error: BaseException) -> None:
    assert not google_ads_retry.is_transient_error(error)


# --- call_with_retry ---------------------------------------------------------


def test_recovers_after_transient_failures(caplog: pytest.LogCaptureFixture) -> None:
    read = _Flaky([_ads_exception(), _RawRpcError(grpc.StatusCode.UNAVAILABLE)])
    sleep = mock.Mock()
    with caplog.at_level(logging.INFO):
        result = google_ads_retry.call_with_retry(
            read, operation="search_stream", customer_id="5341114500", sleep=sleep
        )

    assert result == "rows"
    assert read.calls == 3
    # About 1s then 2s, each within the +/-20% jitter.
    delays = [c.args[0] for c in sleep.call_args_list]
    assert len(delays) == 2
    assert 0.8 <= delays[0] <= 1.2 and 1.6 <= delays[1] <= 2.4

    retries = [r for r in caplog.records if getattr(r, "event", None) == telemetry.EVENT_DEPENDENCY_RETRY]
    assert [r.attempt for r in retries] == [1, 2]
    assert all(r.levelno == logging.WARNING for r in retries)
    assert all(r.dependency == "google_ads" and r.customer_id == "5341114500" for r in retries)
    assert retries[0].error_class == "unavailable"
    assert "request_id req-123" in retries[0].getMessage()
    assert any("succeeded on attempt 3" in r.getMessage() for r in caplog.records)


def test_gives_up_after_the_last_attempt(caplog: pytest.LogCaptureFixture) -> None:
    errors = [_ads_exception() for _ in range(google_ads_retry.MAX_ATTEMPTS)]
    read = _Flaky(errors)
    sleep = mock.Mock()
    with caplog.at_level(logging.INFO), pytest.raises(GoogleAdsException) as raised:
        google_ads_retry.call_with_retry(read, operation="search", sleep=sleep)

    assert raised.value is errors[-1]
    assert read.calls == google_ads_retry.MAX_ATTEMPTS
    assert sleep.call_count == google_ads_retry.MAX_ATTEMPTS - 1
    assert all(c.args[0] <= google_ads_retry.MAX_DELAY_S * 1.2 for c in sleep.call_args_list)
    assert any("giving up" in r.getMessage() for r in caplog.records)


def test_final_error_is_raised_at_once() -> None:
    error = _ads_exception(grpc.StatusCode.INVALID_ARGUMENT, None, message="Bad GAQL")
    read = _Flaky([error])
    sleep = mock.Mock()
    with pytest.raises(GoogleAdsException) as raised:
        google_ads_retry.call_with_retry(read, operation="search_stream", sleep=sleep)

    assert raised.value is error
    assert read.calls == 1
    sleep.assert_not_called()


def test_default_wait_can_be_stubbed() -> None:
    read = _Flaky([_ads_exception()])
    with mock.patch.object(google_ads_retry, "_sleep") as sleep:
        assert google_ads_retry.call_with_retry(read, operation="search") == "rows"
    sleep.assert_called_once()


# --- RetryingGoogleAdsService ------------------------------------------------


def _stream(batches: List[Any], fail_after: Optional[int] = None) -> Iterator[Any]:
    """A search_stream response that can fail part-way through."""
    for index, batch in enumerate(batches):
        if fail_after is not None and index == fail_after:
            raise _RawRpcError(grpc.StatusCode.INTERNAL)
        yield batch


def test_stream_failing_part_way_is_reread_whole() -> None:
    service = mock.MagicMock()
    service.search_stream.side_effect = [
        _stream(["batch1", "batch2"], fail_after=1),
        _stream(["batch1", "batch2"]),
    ]
    wrapped = google_ads_retry.RetryingGoogleAdsService(service)

    with mock.patch.object(google_ads_retry, "_sleep"):
        batches = wrapped.search_stream(customer_id="1", query="SELECT campaign.id FROM campaign")

    # No batch is duplicated by the retry.
    assert batches == ["batch1", "batch2"]
    assert service.search_stream.call_count == 2
    service.search_stream.assert_called_with(customer_id="1", query="SELECT campaign.id FROM campaign")


def test_search_is_read_fully_and_retried() -> None:
    service = mock.MagicMock()
    service.search.side_effect = [api_exceptions.ServiceUnavailable("503"), iter(["row1", "row2"])]
    wrapped = google_ads_retry.google_ads_service(mock.MagicMock(get_service=mock.Mock(return_value=service)))

    with mock.patch.object(google_ads_retry, "_sleep"):
        assert wrapped.search(customer_id="1", query="q") == ["row1", "row2"]
    assert service.search.call_count == 2


def test_mutations_pass_through_and_are_never_retried() -> None:
    service = mock.MagicMock()
    service.mutate.side_effect = _ads_exception()
    wrapped = google_ads_retry.RetryingGoogleAdsService(service)

    with mock.patch.object(google_ads_retry, "_sleep") as sleep, pytest.raises(GoogleAdsException):
        wrapped.mutate(customer_id="1", mutate_operations=[])

    service.mutate.assert_called_once()
    sleep.assert_not_called()


def test_google_ads_service_wraps_the_google_ads_service() -> None:
    client = mock.MagicMock()
    wrapped = google_ads_retry.google_ads_service(client)
    client.get_service.assert_called_once_with("GoogleAdsService")
    assert wrapped.mutate is client.get_service.return_value.mutate


# --- The tools use it ---------------------------------------------------------


def _client_with_flaky_stream(rows: List[Any]) -> mock.MagicMock:
    """A Google Ads client whose first search_stream fails with INTERNAL."""
    client = mock.MagicMock()
    client.get_service.return_value.search_stream.side_effect = [
        _ads_exception(),
        [mock.MagicMock(results=rows)],
    ]
    return client


def test_getter_survives_a_transient_error() -> None:
    row = mock.MagicMock()
    client = _client_with_flaky_stream([row])
    with mock.patch.object(google_ads_getter, "get_google_ads_client", return_value=client), mock.patch.object(
        google_ads_getter, "MessageToDict", return_value={"id": "24252893412"}
    ), mock.patch.object(google_ads_retry, "_sleep"):
        result = google_ads_getter.get_google_ads_campaign_details("5341114500", "24252893412")

    assert result == {"id": "24252893412"}
    assert client.get_service.return_value.search_stream.call_count == 2


def test_budget_update_retries_its_read_but_not_its_write() -> None:
    row = mock.MagicMock()
    row.campaign.campaign_budget = "customers/5341114500/campaignBudgets/1"
    client = _client_with_flaky_stream([row])
    service = client.get_service.return_value
    service.mutate_campaign_budgets.side_effect = _ads_exception()
    with mock.patch.dict("os.environ", {"ADSTA_DRY_RUN": ""}), mock.patch.object(
        google_ads_updater, "get_google_ads_client", return_value=client
    ), mock.patch.object(google_ads_retry, "_sleep"):
        result = google_ads_updater.update_google_ads_campaign_budget("5341114500", "24252893412", 2_000_000)

    assert service.search_stream.call_count == 2
    service.mutate_campaign_budgets.assert_called_once()
    assert result["success"] is False


def test_asset_group_listing_survives_a_transient_error() -> None:
    client = mock.MagicMock()
    search = client.get_service.return_value.search
    search.side_effect = [_ads_exception(), iter([])]
    with mock.patch.object(google_ads_asset_groups, "get_google_ads_client", return_value=client), mock.patch.object(
        google_ads_retry, "_sleep"
    ):
        result = google_ads_asset_groups.list_google_ads_asset_groups("5341114500", "24252893412")

    assert search.call_count == 2
    assert result["success"] is True and result["asset_group_count"] == 0
