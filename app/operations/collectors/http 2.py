"""Shared ARM REST GET helper for operations collectors.

Every collector that talks to an Azure Resource Manager REST endpoint
(Alerts Management, Compute usages, Cognitive Services usages, ...) goes
through `arm_get` so authentication, error handling, and dependency
injection are consistent and centrally testable -- no collector calls
`requests` or `azure.identity` directly.

Auth reuses app.azure_data._credential (ManagedIdentityCredential when
AZURE_CLIENT_ID is set, else DefaultAzureCredential), matching every
other Azure call in this codebase.

`arm_get` is a thin wrapper around the more general `scoped_get`, which
takes an explicit OAuth scope -- added in Phase 2 so the Key Vault expiry
collector (app.operations.collectors.keyvault) can reuse the same auth/
error-handling path against the *data-plane* Key Vault scope
(KEYVAULT_SCOPE) instead of ARM's (ARM_SCOPE), without duplicating any of
the token-acquisition/HTTP-error logic below.

`paginated_get` is the shared, bounded `nextLink`-following helper every
collector whose list API can legitimately return more than one page
(Azure Monitor/Defender alerts, Defender assessments, Cost Management
budgets, Compute/Cognitive Services usages, Key Vault list APIs,
Automation jobs, ...) should go through -- see its own docstring for the
bound/truncation contract. It replaces what used to be two independent,
copy-pasted bounded-pagination loops (Key Vault's `list_vault_items` and
Automation's `collect_automation_failures`) and adds the same bounded
`nextLink` following to every ARM list collector that previously ignored
`nextLink` entirely (Azure Monitor/Defender alerts, Defender assessments,
Cost Management budgets, and Compute/Cognitive Services usages).
"""

import json as _json
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from app.azure_data import _credential as default_credential_factory
from app.operations.errors import OperationsCollectionError

_logger = logging.getLogger(__name__)

__all__ = [
    "ARM_BASE_URL",
    "ARM_SCOPE",
    "KEYVAULT_SCOPE",
    "CredentialFactory",
    "HttpGet",
    "HttpPost",
    "PagedListResult",
    "default_credential_factory",
    "default_http_get",
    "default_http_post",
    "arm_get",
    "arm_post",
    "scoped_get",
    "paginated_get",
]

ARM_BASE_URL = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
# Key Vault's data-plane token scope -- distinct from ARM's. Used by
# app.operations.collectors.keyvault, which talks to a per-vault base URL
# (the vault URI itself) rather than ARM_BASE_URL.
KEYVAULT_SCOPE = "https://vault.azure.net/.default"

# A TokenCredential-like callable: () -> object with .get_token(scope).token
CredentialFactory = Callable[[], object]
# (url, *, headers, params, timeout) -> a requests.Response-like object
# with .status_code, .json(), and .text
HttpGet = Callable[..., object]
# (url, *, headers, json, timeout) -> a requests.Response-like object
# with .status_code, .json(), and .text
HttpPost = Callable[..., object]

# Absolute ceilings `paginated_get` never lets a caller-supplied
# max_pages/max_records exceed, regardless of what a collector (or a
# future misconfiguration) asks for -- defense-in-depth against a
# runaway crawl of a single Azure list API (e.g. a nextLink loop, or a
# tenant with an unexpectedly huge object count) blocking collection of
# every other source behind it.
_HARD_MAX_PAGES = 50
_HARD_MAX_RECORDS = 5000
# Sane per-call defaults for a collector that doesn't need a tighter
# bound of its own -- deliberately generous relative to normal Azure
# list sizes (a subscription's alerts/assessments/budgets/usages) while
# still far below the hard ceilings above.
DEFAULT_MAX_PAGES = 20
DEFAULT_MAX_RECORDS = 2000


def default_http_get(url: str, *, headers: dict, params: Optional[dict] = None, timeout: int = 30):
    return requests.get(url, headers=headers, params=params, timeout=timeout)


def default_http_post(url: str, *, headers: dict, json: Optional[dict] = None, timeout: int = 30):
    return requests.post(url, headers=headers, json=json, timeout=timeout)


def scoped_get(
    url: str,
    *,
    source: str,
    scope: str,
    params: Optional[dict] = None,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    timeout: int = 30,
) -> dict:
    """GET `url` with a bearer token for `scope`, and return the parsed
    JSON body.

    Always raises OperationsCollectionError -- never returns a
    success-shaped empty dict -- on token acquisition failure, a
    non-2xx response, or a body that isn't valid JSON.
    """
    try:
        credential = credential_factory()
        token = credential.get_token(scope).token
    except Exception as exc:
        # Broad on purpose and the one place it's appropriate: this is
        # exactly the auth-failure case callers must see explicitly (see
        # module docstring) rather than have swallowed into an empty
        # result. Always re-raised as a typed error, never suppressed.
        raise OperationsCollectionError(source, "failed to acquire an Azure AD token", detail=str(exc)) from exc

    try:
        response = http_get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise OperationsCollectionError(source, f"request to {url} failed", detail=str(exc)) from exc

    status_code = getattr(response, "status_code", None)
    if status_code is None or status_code < 200 or status_code >= 300:
        detail = getattr(response, "text", "")
        raise OperationsCollectionError(
            source, f"{url} returned HTTP {status_code}", detail=str(detail)[:500] if detail else None
        )

    try:
        return response.json()
    except (ValueError, _json.JSONDecodeError) as exc:
        raise OperationsCollectionError(source, f"{url} returned a non-JSON body", detail=str(exc)) from exc


@dataclass
class PagedListResult:
    """The bounded result of `paginated_get` -- always what was
    actually collected (never raises just because a bound was hit), plus
    explicit, honest metadata about whether more data existed beyond the
    bound. Callers decide what to do with `truncated` (e.g. surface it in
    a Finding/log a warning) -- it is never silently dropped."""
    items: list = field(default_factory=list)
    truncated: bool = False
    pages_fetched: int = 0


def paginated_get(
    path: str,
    *,
    source: str,
    scope: str = ARM_SCOPE,
    params: Optional[dict] = None,
    value_key: str = "value",
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    timeout: int = 30,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> PagedListResult:
    """Follow an ARM/data-plane list API's `nextLink` (the standard Azure
    REST pagination convention every collector in this codebase's list
    APIs uses -- Alerts Management, Microsoft.Security, Cost Management,
    Compute/Cognitive Services usages, Key Vault, Automation) via
    `scoped_get`, accumulating each page's `value_key` array, up to
    `max_pages` page fetches or `max_records` total items -- whichever
    is hit first.

    `nextLink` is documented (Azure REST API guidelines) to be an
    absolute, ready-to-call URL that already carries its own query
    string -- so every page after the first is fetched with `params=None`
    to avoid re-appending/duplicating query parameters.

    Bounded, never unbounded: `max_pages`/`max_records` are clamped to
    this module's hard ceilings (`_HARD_MAX_PAGES`/`_HARD_MAX_RECORDS`)
    regardless of what a caller asks for -- one runaway/misbehaving list
    API (or an unexpectedly huge tenant) can never turn into an unbounded
    crawl that starves every other source's collection budget.

    NEVER raises just because a bound was reached -- reaching a page/
    record bound with more data still available is an expected, safe
    stopping point, not a collection failure (the caller already got a
    trustworthy, if incomplete, result). Returns a `PagedListResult`
    whose `truncated` flag is explicit and logs a warning (visible in
    AppTraces via this codebase's OpenTelemetry logging integration --
    see app/telemetry.py) so truncation is surfaced, never silent.
    Genuine failures (auth, a non-2xx page, a non-JSON body) still raise
    OperationsCollectionError via `scoped_get`, exactly as a single-page
    `arm_get`/`scoped_get` call would.
    """
    max_pages = max(1, min(max_pages, _HARD_MAX_PAGES))
    max_records = max(1, min(max_records, _HARD_MAX_RECORDS))

    url = path if path.startswith("http") else f"{ARM_BASE_URL}{path}"
    current_params = params
    items: list = []
    pages_fetched = 0
    next_link = None

    for _ in range(max_pages):
        body = scoped_get(
            url, source=source, scope=scope, params=current_params,
            credential_factory=credential_factory, http_get=http_get, timeout=timeout,
        )
        pages_fetched += 1
        items.extend(body.get(value_key) or [])
        next_link = body.get("nextLink")
        if len(items) >= max_records:
            break
        if not next_link:
            break
        url = next_link
        current_params = None  # nextLink already carries its own query string

    truncated = False
    if len(items) > max_records:
        items = items[:max_records]
        truncated = True
    if next_link:
        # Either the record bound was hit with more still available on
        # this same page, or max_pages was exhausted while the last
        # fetched page still pointed at a nextLink -- either way, this is
        # a bounded stop, not "there was no more data".
        truncated = True

    if truncated:
        _logger.warning(
            "%s: paginated_get stopped after %d page(s)/%d item(s) (max_pages=%d, max_records=%d) with more data "
            "still available -- result is bounded, not exhaustive.",
            source, pages_fetched, len(items), max_pages, max_records,
        )

    return PagedListResult(items=items, truncated=truncated, pages_fetched=pages_fetched)


def arm_get(
    path: str,
    *,
    source: str,
    params: Optional[dict] = None,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    timeout: int = 30,
) -> dict:
    """GET an ARM REST endpoint and return the parsed JSON body. See
    `scoped_get` for the shared implementation/error semantics."""
    url = path if path.startswith("http") else f"{ARM_BASE_URL}{path}"
    return scoped_get(
        url, source=source, scope=ARM_SCOPE, params=params,
        credential_factory=credential_factory, http_get=http_get, timeout=timeout,
    )


def arm_post(
    path: str,
    *,
    source: str,
    json_body: Optional[dict] = None,
    credential_factory: CredentialFactory = default_credential_factory,
    http_post: HttpPost = default_http_post,
    timeout: int = 30,
) -> dict:
    """POST an ARM REST endpoint (e.g. Cost Management's Query API, which
    has no GET equivalent) and return the parsed JSON body.

    Same error semantics as `arm_get`/`scoped_get`: always raises
    OperationsCollectionError -- never a success-shaped empty dict -- on
    token acquisition failure, a non-2xx response, or a non-JSON body.
    """
    url = path if path.startswith("http") else f"{ARM_BASE_URL}{path}"

    try:
        credential = credential_factory()
        token = credential.get_token(ARM_SCOPE).token
    except Exception as exc:
        raise OperationsCollectionError(source, "failed to acquire an Azure AD token", detail=str(exc)) from exc

    try:
        response = http_post(
            url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=json_body, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise OperationsCollectionError(source, f"request to {url} failed", detail=str(exc)) from exc

    status_code = getattr(response, "status_code", None)
    if status_code is None or status_code < 200 or status_code >= 300:
        detail = getattr(response, "text", "")
        raise OperationsCollectionError(
            source, f"{url} returned HTTP {status_code}", detail=str(detail)[:500] if detail else None
        )

    try:
        return response.json()
    except (ValueError, _json.JSONDecodeError) as exc:
        raise OperationsCollectionError(source, f"{url} returned a non-JSON body", detail=str(exc)) from exc
