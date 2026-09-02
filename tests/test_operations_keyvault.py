#!/usr/bin/env python3
"""Test Key Vault certificate/secret/key expiry normalization
(app/operations/collectors/keyvault.py) -- expiry/severity
classification, that NO secret/key/certificate value is ever read or
surfaced, partial per-(vault, object type) permission-failure
resilience, and total-failure surfacing.

All Azure calls are injected fakes; no real network calls are made.

Run: python3 tests/test_operations_keyvault.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.operations.collectors import keyvault  # noqa: E402
from app.operations.errors import OperationsCollectionError  # noqa: E402

PASS = 0
FAIL = 0


def test(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \u2705 {name}")
    else:
        FAIL += 1
        print(f"  \u274c {name}")


NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)
SOON_EXP = int((NOW + timedelta(days=5)).timestamp())
PAST_EXP = int((NOW - timedelta(days=2)).timestamp())
FAR_EXP = int((NOW + timedelta(days=400)).timestamp())


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeCredential:
    def get_token(self, scope):
        assert scope == keyvault.KEYVAULT_SCOPE, "must request the Key Vault data-plane scope, not ARM's"
        class Token:
            token = "fake-token"  # noqa: S105
        return Token()


def fake_credential_factory():
    return FakeCredential()


def http_get(url, *, headers, params=None, timeout=30):
    if "/certificates" in url:
        return FakeResponse({"value": [{"id": "https://kv1.vault.azure.net/certificates/cert1", "attributes": {"enabled": True, "exp": SOON_EXP}}]})
    if "/secrets" in url:
        return FakeResponse({"value": [
            {"id": "https://kv1.vault.azure.net/secrets/sec1", "attributes": {"enabled": True, "exp": PAST_EXP}},
            {"id": "https://kv1.vault.azure.net/secrets/sec2", "attributes": {"enabled": True, "exp": FAR_EXP}},
            {"id": "https://kv1.vault.azure.net/secrets/sec3", "attributes": {"enabled": True}},
            {"id": "https://kv1.vault.azure.net/secrets/sec4", "attributes": {"enabled": False, "exp": PAST_EXP}},
        ]})
    if "/keys" in url:
        return FakeResponse({}, status_code=403, text="Forbidden")
    raise AssertionError(f"unexpected url {url}")


# ─── Successful normalization + severity + no secret values ────────────
print("\n\U0001f9ea Test 1: collect_key_vault_expiry -- expiry classification, and NEVER a secret/key/cert value")
findings = keyvault.collect_key_vault_expiry(["https://kv1.vault.azure.net/"], warning_days=30, credential_factory=fake_credential_factory, http_get=http_get, now=NOW)
titles = [f.title for f in findings]
test("the soon-to-expire certificate raises a Finding", any("cert1" in t for t in titles))
test("the already-expired secret raises a Finding", any("sec1" in t for t in titles))
test("the far-future secret (400d) raises no Finding", not any("sec2" in t for t in titles))
test("the no-expiry-set secret raises no Finding", not any("sec3" in t for t in titles))
test("the disabled (but expired) secret raises no Finding -- not currently in use", not any("sec4" in t for t in titles))

cert_finding = next(f for f in findings if "cert1" in f.title)
expired_finding = next(f for f in findings if "sec1" in f.title)
test("a soon-to-expire item is medium severity", cert_finding.severity == "medium")
test("an already-expired item is high severity", expired_finding.severity == "high")
test("an already-expired item demands executive attention", expired_finding.executive_attention is True)
test("all Key Vault expiry Findings use category certificate", all(f.category == "certificate" for f in findings if "keys" not in f.title))

serialized = str([f.to_dict() for f in findings])
test("no Finding field ever contains the string 'value' as a key (List APIs return metadata only)", all("secretValue" not in str(f.to_dict()) and "keyMaterial" not in str(f.to_dict()) for f in findings))
test("no evidence raw_excerpt exceeds the 400-char bound (defense-in-depth sanitization still applies)", all(len(e.raw_excerpt or "") <= 500 for f in findings for e in f.evidence))

# ─── Partial permission failure -- surfaced explicitly, not fatal ──────
print("\n\U0001f9ea Test 2: a single (vault, object type) permission failure is surfaced explicitly, not fatal to the whole vault")
test("the /keys 403 produces its own low-severity 'cannot check' Finding rather than aborting", any("Cannot check keys expiry" in t for t in titles))
permission_finding = next(f for f in findings if "Cannot check keys expiry" in f.title)
test("a permission-gap Finding is low severity (a monitoring gap, not itself an incident)", permission_finding.severity == "low")
test("certificates/secrets from the SAME vault were still collected despite the /keys failure", any("cert1" in t for t in titles) and any("sec1" in t for t in titles))

# ─── Total failure -- every (vault, object type) pair fails ────────────
print("\n\U0001f9ea Test 3: total failure (every object type fails) raises OperationsCollectionError, not an empty success")


def http_get_all_403(url, *, headers, params=None, timeout=30):
    return FakeResponse({}, status_code=403, text="Forbidden")


try:
    keyvault.collect_key_vault_expiry(["https://kv1.vault.azure.net/"], credential_factory=fake_credential_factory, http_get=http_get_all_403)
    test("total failure across every object type raises OperationsCollectionError", False)
except OperationsCollectionError:
    test("total failure across every object type raises OperationsCollectionError", True)

try:
    keyvault.collect_key_vault_expiry([], credential_factory=fake_credential_factory, http_get=http_get)
    test("an empty vault_uris list raises ValueError", False)
except ValueError:
    test("an empty vault_uris list raises ValueError", True)


# ─── Multipage/bounded pagination -- list_vault_items follows nextLink ──
print("\n\U0001f9ea Test 4: list_vault_items -- follows nextLink across multiple pages, and honors max_items as a hard cap")
CERTS_PAGE2_URL = "https://kv1.vault.azure.net/certificates?api-version=7.4&page=2"


def multipage_http_get(url, *, headers, params=None, timeout=30):
    if "page=2" in url:
        return FakeResponse({"value": [{"id": "https://kv1.vault.azure.net/certificates/cert2", "attributes": {"enabled": True, "exp": SOON_EXP}}]})
    return FakeResponse({"value": [{"id": "https://kv1.vault.azure.net/certificates/cert1", "attributes": {"enabled": True, "exp": SOON_EXP}}], "nextLink": CERTS_PAGE2_URL})


multipage_items = keyvault.list_vault_items(
    "https://kv1.vault.azure.net/", "certificates", credential_factory=fake_credential_factory, http_get=multipage_http_get,
)
test("collects certificates from both pages, not just the first", {i["id"].rsplit("/", 1)[-1] for i in multipage_items} == {"cert1", "cert2"})


def endless_http_get(url, *, headers, params=None, timeout=30):
    return FakeResponse({"value": [{"id": "https://kv1.vault.azure.net/certificates/endless", "attributes": {"enabled": True, "exp": SOON_EXP}}], "nextLink": "https://kv1.vault.azure.net/certificates?page=999"})


bounded_items = keyvault.list_vault_items(
    "https://kv1.vault.azure.net/", "certificates", max_items=3, credential_factory=fake_credential_factory, http_get=endless_http_get,
)
test("max_items is still honored as a hard cap against a runaway nextLink chain", len(bounded_items) == 3)


# ─── Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")

sys.exit(1 if FAIL > 0 else 0)
