"""Key Vault certificate/secret/key expiry, normalized to Findings.

Uses the Key Vault DATA-PLANE List APIs
(GET {vaultUri}/certificates|secrets|keys?api-version=7.4) via
app.operations.collectors.http.paginated_get with KEYVAULT_SCOPE -- these
List operations return only item metadata (id, attributes, tags), never
the secret/key/certificate material, so this collector cannot leak a
value even in principle. It never calls a Get-value endpoint.

Data-plane RBAC/access-policy is commonly granted more narrowly than ARM
Reader (e.g. "Key Vault Secrets User" without "Key Vault Certificates
User"), so a permission failure on one (vault, object type) pair is
treated as a partial, per-pair gap -- not a reason to abandon every other
vault/object type. Each such gap becomes its own low-severity Finding
(see docs/AZURE_DATA_SOURCES.md) so the blind spot is visible, rather
than silently vanishing. Only if literally EVERY (vault, object type)
pair fails does this collector raise OperationsCollectionError (a
genuine, total collection failure -- e.g. the credential itself is
invalid).
"""

from datetime import datetime, timezone
from typing import Optional

from app.operations.collectors.http import (
    KEYVAULT_SCOPE,
    CredentialFactory,
    HttpGet,
    default_credential_factory,
    default_http_get,
    paginated_get,
)
from app.operations.errors import OperationsCollectionError
from app.operations.models import (
    ConfidenceLevel,
    EvidenceReference,
    EvidenceSource,
    Finding,
    FindingCategory,
    FindingStatus,
    Severity,
    format_utc_iso,
)

__all__ = ["list_vault_items", "collect_key_vault_expiry"]

SOURCE = EvidenceSource.KEY_VAULT_EXPIRY.value
API_VERSION = "7.4"
OBJECT_TYPES = ("certificates", "secrets", "keys")
_MAX_PAGES = 5  # bounded pagination -- never an unbounded crawl of a vault (see app.operations.collectors.http.paginated_get)


def _item_name_from_id(item_id: str) -> str:
    return item_id.rstrip("/").split("/")[-1] if item_id else ""


def list_vault_items(
    vault_uri: str,
    object_type: str,
    *,
    max_items: int = 200,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
) -> list:
    """List up to `max_items` metadata-only entries of one object type
    ('certificates' | 'secrets' | 'keys') from one vault.

    Always raises OperationsCollectionError -- never an empty
    success-shaped list -- on auth/permission failure or a non-2xx
    response; callers decide whether that's a total failure or a
    partial, per-vault/per-type gap (see collect_key_vault_expiry).
    """
    if object_type not in OBJECT_TYPES:
        raise ValueError(f"object_type must be one of {OBJECT_TYPES}, got {object_type!r}")

    paged = paginated_get(
        f"{vault_uri.rstrip('/')}/{object_type}",
        source=SOURCE, scope=KEYVAULT_SCOPE,
        params={"api-version": API_VERSION, "maxresults": min(max_items, 25)},
        credential_factory=credential_factory, http_get=http_get,
        max_pages=_MAX_PAGES, max_records=max_items,
    )
    return paged.items


def _expiry_finding(item: dict, *, object_type: str, vault_uri: str, warning_days: int, now: datetime) -> Optional[Finding]:
    attributes = item.get("attributes") or {}
    if attributes.get("enabled") is False:
        return None  # a disabled item is not currently usable; its expiry is not an active operational risk
    exp = attributes.get("exp")
    if exp is None:
        return None  # no expiry set -- out of scope for "approaching expiry within N days"

    expires_at = datetime.fromtimestamp(int(exp), tz=timezone.utc)
    days_remaining = (expires_at - now).total_seconds() / 86400.0
    if days_remaining > warning_days:
        return None

    item_id = item.get("id") or ""
    item_name = _item_name_from_id(item_id)
    already_expired = days_remaining < 0
    severity = Severity.HIGH if already_expired else Severity.MEDIUM
    singular = object_type[:-1]  # certificates -> certificate, secrets -> secret, keys -> key
    evaluated_at = format_utc_iso(now)
    status_text = "has already expired" if already_expired else f"expires in {days_remaining:.1f} day(s)"

    return Finding(
        category=FindingCategory.CERTIFICATE.value,
        severity=severity.value,
        status=FindingStatus.OPEN.value,
        title=f"Key Vault {singular} '{item_name}' {status_text}",
        summary=f"{singular.capitalize()} '{item_name}' in {vault_uri} {status_text} (expiry {format_utc_iso(expires_at)}).",
        business_impact=(
            f"Dependent services using this {singular} are likely already failing to authenticate/decrypt."
            if already_expired else
            f"Dependent services using this {singular} will fail once it expires."
        ),
        first_seen=evaluated_at,
        last_seen=evaluated_at,
        source=SOURCE,
        resource_id=None,  # Key Vault items are not independent ARM resources -- see module docstring
        confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=[EvidenceReference(
            source=SOURCE,
            title=f"{singular} {item_name}",
            observed_at=evaluated_at,
            reference=item_id,
            raw_excerpt=f"expiresAt={format_utc_iso(expires_at)}; enabled={attributes.get('enabled')}",
        )],
        recommended_action=(
            f"Rotate this {singular} immediately -- it has already expired."
            if already_expired else
            f"Rotate/renew this {singular} before it expires."
        ),
        approval_required=False,
        executive_attention=already_expired,
        metadata={
            "vault_uri": vault_uri, "object_type": object_type, "item_name": item_name,
            "expires_at": format_utc_iso(expires_at), "days_remaining": round(days_remaining, 1),
        },
        discriminator=item_id,
    )


def _permission_denied_finding(vault_uri: str, object_type: str, message: str, *, now: datetime) -> Finding:
    evaluated_at = format_utc_iso(now)
    return Finding(
        category=FindingCategory.CERTIFICATE.value,
        severity=Severity.LOW.value,
        status=FindingStatus.OPEN.value,
        title=f"Cannot check {object_type} expiry in {vault_uri}",
        summary=f"Listing {object_type} in {vault_uri} failed -- likely a data-plane RBAC/access-policy gap.",
        business_impact=f"Expiry monitoring for {object_type} in this vault is blind until access is granted.",
        first_seen=evaluated_at,
        last_seen=evaluated_at,
        source=SOURCE,
        resource_id=None,
        confidence=ConfidenceLevel.CONFIRMED.value,
        evidence=[EvidenceReference(
            source=SOURCE,
            title=f"{object_type} list permission check",
            observed_at=evaluated_at,
            reference=f"{vault_uri}/{object_type}",
            raw_excerpt=message[:300],
        )],
        recommended_action=f"Grant the collector's managed identity data-plane List permission on {object_type} for this vault.",
        approval_required=False,
        executive_attention=False,
        metadata={"vault_uri": vault_uri, "object_type": object_type},
        discriminator=f"permission-denied|{vault_uri}|{object_type}",
    )


def collect_key_vault_expiry(
    vault_uris: list,
    *,
    warning_days: int = 30,
    max_items_per_type: int = 200,
    credential_factory: CredentialFactory = default_credential_factory,
    http_get: HttpGet = default_http_get,
    now: Optional[datetime] = None,
) -> list:
    """Certificates/secrets/keys approaching (or past) expiry within
    `warning_days`, across every vault in `vault_uris`. Never fetches an
    item's value -- see module docstring."""
    if not vault_uris:
        raise ValueError("vault_uris must be a non-empty list")
    if warning_days <= 0:
        raise ValueError("warning_days must be positive")
    now = now or datetime.now(timezone.utc)

    findings = []
    permission_errors = []
    total_attempts = 0
    for vault_uri in vault_uris:
        for object_type in OBJECT_TYPES:
            total_attempts += 1
            try:
                items = list_vault_items(
                    vault_uri, object_type, max_items=max_items_per_type,
                    credential_factory=credential_factory, http_get=http_get,
                )
            except OperationsCollectionError as exc:
                permission_errors.append((vault_uri, object_type, str(exc)))
                continue
            for item in items:
                finding = _expiry_finding(item, object_type=object_type, vault_uri=vault_uri, warning_days=warning_days, now=now)
                if finding is not None:
                    findings.append(finding)

    if permission_errors and len(permission_errors) == total_attempts:
        # Nothing could be read anywhere -- a genuine collection
        # failure (e.g. the credential itself is invalid), not a
        # partial per-vault/per-type RBAC gap.
        detail = "; ".join(f"{v}/{t}: {msg}" for v, t, msg in permission_errors[:5])
        raise OperationsCollectionError(
            SOURCE, "failed to list any Key Vault object type across all configured vaults", detail=detail
        )

    for vault_uri, object_type, message in permission_errors:
        findings.append(_permission_denied_finding(vault_uri, object_type, message, now=now))
    return findings
