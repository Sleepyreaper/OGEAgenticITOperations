"""ZeroOps demo scenarios: break something real (safely), watch who fixes it.

Two tiers:
  * ``sre``   -- runbook-shaped; the Azure SRE Agent triages and proposes the
                 fix on its own (Review mode: a human approves the action).
  * ``squad`` -- ambiguous, cross-domain or business-impacting; the SRE Agent
                 escalates to the OGE squad over MCP, which reasons about
                 impact and produces a fix/script for human approval.

Inject/cleanup only touch resources named by ``ZEROOPS_DEMO_*`` settings
(deployed by infra/zeroops-demo/) and only when ``ZEROOPS_CHAOS_ENABLED``
is true. Every step returns an explicit status; nothing is silently skipped.
"""
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import requests

ARM = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
KV_SCOPE = "https://vault.azure.net/.default"

# Tags the demo template applies; cleanup restores exactly these.
BASE_TAGS = {"zeroops-demo": "true"}
CHAOS_NSG_RULE = "zeroops-open-door-ssh"
BAD_DEPLOY_COMMAND = "python -c \"raise SystemExit('ZeroOps bad deploy: missing config DB_CONN')\""
CERT_SECRET_NAME = "zeroops-2am-tls-bundle"
CERT_RUNBOOK_NAME = "Renew-ZeroOpsTlsCert"
CERT_RUNBOOK_SCRIPT = (
    "Write-Output 'Renewing TLS bundle zeroops-2am-tls-bundle...'\n"
    "throw 'Renewal failed: ACME challenge timed out (dns-01 record not found)'\n"
)


class ScenarioError(Exception):
    pass


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    emoji: str
    tier: str
    tagline: str
    story: str
    signals: tuple
    sre_playbook: tuple
    human_approval: str
    sre_task_profile: str
    required_settings: tuple
    escalation_question: str = ""
    why_escalate: str = ""
    category: str = ""
    expected_agents: tuple = field(default_factory=tuple)
    remediation_script: str = ""

    def to_dict(self, config: "DemoConfig" = None) -> dict:
        data = asdict(self)
        config = config or DemoConfig.from_env()
        data["configured"] = all(config.get(k) for k in self.required_settings)
        data["missing_settings"] = [f"ZEROOPS_DEMO_{k.upper()}" for k in self.required_settings if not config.get(k)]
        data["remediation_script"] = config.render(self.remediation_script)
        return data


@dataclass(frozen=True)
class DemoConfig:
    enabled: bool
    subscription_id: str
    resource_group: str
    nsg: str
    webapp: str
    plan: str
    storage: str
    keyvault: str
    automation: str

    @classmethod
    def from_env(cls) -> "DemoConfig":
        env = lambda k: os.environ.get(f"ZEROOPS_DEMO_{k}", "").strip()  # noqa: E731
        return cls(
            enabled=os.environ.get("ZEROOPS_CHAOS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"),
            subscription_id=os.environ.get("ZEROOPS_SUBSCRIPTION_ID", "").strip() or os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip(),
            resource_group=env("RESOURCE_GROUP"), nsg=env("NSG"), webapp=env("WEBAPP"), plan=env("PLAN"),
            storage=env("STORAGE"), keyvault=env("KEYVAULT"), automation=env("AUTOMATION"),
        )

    def get(self, key: str) -> str:
        return getattr(self, key, "") or ""

    def public(self) -> dict:
        return {
            "chaos_enabled": self.enabled,
            "resource_group_configured": bool(self.resource_group),
            "resources": {k: bool(self.get(k)) for k in ("nsg", "webapp", "plan", "storage", "keyvault", "automation")},
        }

    def render(self, text: str) -> str:
        out = text
        for key in ("resource_group", "nsg", "webapp", "plan", "storage", "keyvault", "automation"):
            out = out.replace("{" + key + "}", self.get(key) or f"<{key}>")
        return out

    def rg_path(self) -> str:
        return f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"


SCENARIOS = (
    Scenario(
        id="open-door", title="The Open Door", emoji="🚪", tier="sre",
        tagline="Someone opened SSH to the internet. The SRE Agent closes it before coffee.",
        story=(
            "A well-meaning engineer adds an inbound allow rule for port 22 from * to debug a VM. "
            "Activity Log + NSG drift fire. This is a known pattern with a known runbook."
        ),
        signals=("Activity Log: securityRules/write", "NSG drift: 22/tcp open to 0.0.0.0/0"),
        sre_playbook=(
            "Skill 'nsg-open-port-triage' matches the alert",
            "Reads the rule, the caller identity and the change time",
            "Confirms no approved exception tag on the NSG",
            "Proposes: delete the rule (Review mode -> human approves)",
        ),
        human_approval="Approve the SRE Agent's 'delete NSG rule' action in the SRE Agent portal.",
        sre_task_profile="quick_question",
        required_settings=("resource_group", "nsg"),
        remediation_script=(
            "az network nsg rule delete -g {resource_group} --nsg-name {nsg} -n " + CHAOS_NSG_RULE
        ),
    ),
    Scenario(
        id="bad-deploy", title="Bad Deploy Friday", emoji="💥", tier="sre",
        tagline="A config push takes the demo web app down. The SRE Agent rolls it back.",
        story=(
            "A Friday deploy changes the web app's startup command to one that expects a missing "
            "DB_CONN setting. The container crash-loops and the HTTP 5xx alert fires."
        ),
        signals=("Azure Monitor alert: Http5xx > 0", "App Service: container exited", "Change Analysis: siteConfig.appCommandLine changed"),
        sre_playbook=(
            "Skill 'webapp-bad-deploy-revert' correlates the 5xx spike with the config change",
            "Reads container logs in App Service / App Insights",
            "Proposes: revert the startup command to the last known good value",
            "Verifies health after a human approves the revert",
        ),
        human_approval="Approve the SRE Agent's 'revert site config' action.",
        sre_task_profile="incident_investigation",
        required_settings=("resource_group", "webapp"),
        remediation_script='az webapp config set -g {resource_group} -n {webapp} --startup-file ""',
    ),
    Scenario(
        id="storm-surge", title="Storm Surge", emoji="⛈️", tier="squad",
        tagline="Capacity, cost and SLO all move at once. Scale up or ride it out?",
        story=(
            "An outage-season traffic surge triggers an emergency scale-out of the demo plan. Cost jumps, "
            "quota headroom shrinks and the SLO budget burns. Restarting won't help -- this needs a "
            "business trade-off, so the SRE Agent escalates."
        ),
        signals=("App Service plan scaled to premium x3", "Cost anomaly", "Quota/capacity headroom drop", "SLO burn"),
        sre_playbook=(
            "Skill 'escalation-policy' flags multi-domain impact (cost + capacity + reliability)",
            "Collects the scale event, metrics and current cost",
            "Escalates to the OGE squad via ogeops_escalate",
        ),
        why_escalate="Cross-domain trade-off (cost vs. capacity vs. SLO) with no single safe runbook action.",
        escalation_question=(
            "The demo App Service plan was emergency-scaled to premium with 3 instances during a traffic surge. "
            "Assess the cost, capacity/quota and SLO impact together and recommend whether to keep, right-size, "
            "or revert the scale-out, with an approval-ready change script."
        ),
        category="", expected_agents=("cost_sentinel", "diagnostics_sre", "standards_architect"),
        human_approval="Approve the squad's proposal (right-size script) in the ZeroOps view or ADO.",
        sre_task_profile="incident_investigation",
        required_settings=("resource_group", "plan"),
        remediation_script="az appservice plan update -g {resource_group} -n {plan} --sku B1 --number-of-workers 1",
    ),
    Scenario(
        id="rogue-hotfix", title="The Rogue Hotfix", emoji="🩹", tier="squad",
        tagline="A 2 AM hotfix 'temporarily' opened storage to the public. Revert it -- or break prod?",
        story=(
            "During an incident someone enabled anonymous blob access and dropped TLS to 1.0 on the demo "
            "storage account, tagging it 'hotfix'. It violates policy and is a security gap, but reverting "
            "blindly might break whatever the hotfix was for. The SRE Agent escalates."
        ),
        signals=("Policy: storage public access / min TLS non-compliant", "Security drift: anonymous blob access", "Activity Log: storageAccounts/write tagged hotfix"),
        sre_playbook=(
            "Skill 'escalation-policy' sees policy + security + possible business dependency",
            "Collects the change, caller, tags and policy state",
            "Escalates to the OGE squad via ogeops_escalate",
        ),
        why_escalate="Policy and security impact with an unknown business dependency -- needs reasoning, not a reflex revert.",
        escalation_question=(
            "An emergency hotfix enabled anonymous blob access and set minimum TLS 1.0 on the demo storage account "
            "(tagged hotfix). Assess the security, policy and business impact, decide whether to revert now or "
            "grant a time-boxed exception, and produce an approval-ready remediation script."
        ),
        category="", expected_agents=("scout", "compliance_inspector", "standards_architect"),
        human_approval="Approve the squad's proposal (revert or exception) in the ZeroOps view or ADO.",
        sre_task_profile="incident_investigation",
        required_settings=("resource_group", "storage"),
        remediation_script=(
            "az storage account update -g {resource_group} -n {storage} --allow-blob-public-access false --min-tls-version TLS1_2\n"
            "az tag update --resource-id $(az storage account show -g {resource_group} -n {storage} --query id -o tsv) "
            "--operation Delete --tags hotfix"
        ),
    ),
    Scenario(
        id="2am-cert", title="The 2 AM Cert", emoji="⏰", tier="squad",
        tagline="A TLS bundle expires in 48 hours and the auto-renew job just failed.",
        story=(
            "A certificate bundle in the demo Key Vault expires in two days. The Automation runbook that "
            "renews it failed with an ACME DNS challenge error. Re-running the job won't fix DNS, so the "
            "SRE Agent escalates for a root cause and a fallback plan."
        ),
        signals=("Key Vault: object expires in < 3 days", "Automation: runbook job Failed", "Job output: dns-01 challenge timed out"),
        sre_playbook=(
            "Skill 'escalation-policy' sees expiry deadline + failed automation (no safe retry)",
            "Collects job output and the vault object metadata",
            "Escalates to the OGE squad via ogeops_escalate",
        ),
        why_escalate="Deadline-driven, multi-system failure (Key Vault + Automation + DNS) without a safe automated retry.",
        escalation_question=(
            "A TLS certificate bundle in the demo Key Vault expires in about 48 hours and the Automation renewal "
            "runbook failed with a DNS challenge error. Identify the root cause, the blast radius if it expires, "
            "and an approval-ready renewal/fallback plan."
        ),
        category="", expected_agents=("diagnostics_sre", "standards_architect", "compliance_inspector"),
        human_approval="Approve the squad's renewal/fallback proposal in the ZeroOps view or ADO.",
        sre_task_profile="full_remediation",
        required_settings=("resource_group", "keyvault", "automation"),
        remediation_script=(
            "# 1. Fix the DNS challenge record, then re-run renewal\n"
            "az automation runbook start -g {resource_group} --automation-account-name {automation} -n " + CERT_RUNBOOK_NAME + "\n"
            "# 2. Fallback: issue a short-lived replacement and rotate consumers\n"
            "az keyvault certificate create --vault-name {keyvault} -n zeroops-2am-tls-fallback "
            "-p \"$(az keyvault certificate get-default-policy)\""
        ),
    ),
)

SCENARIOS_BY_ID = {s.id: s for s in SCENARIOS}


def list_scenarios(config: DemoConfig = None) -> list:
    config = config or DemoConfig.from_env()
    return [s.to_dict(config) for s in SCENARIOS]


def get_scenario(scenario_id: str) -> Scenario:
    scenario = SCENARIOS_BY_ID.get(scenario_id)
    if scenario is None:
        raise KeyError(scenario_id)
    return scenario


# ─── ARM / data-plane helpers ───────────────────────────────────

def _token(scope: str) -> str:
    from app.azure_data import _credential
    return _credential().get_token(scope).token


def _call(method: str, url: str, *, scope: str = ARM_SCOPE, body: dict = None, params: dict = None, text: str = None) -> dict:
    headers = {"Authorization": f"Bearer {_token(scope)}"}
    if text is not None:
        headers["Content-Type"] = "text/powershell"
        resp = requests.request(method, url, headers=headers, params=params, data=text.encode(), timeout=60)
    else:
        resp = requests.request(method, url, headers=headers, params=params, json=body, timeout=60)
    if resp.status_code == 404 and method == "DELETE":
        return {"status": "already_clean"}
    if resp.status_code >= 400:
        raise ScenarioError(f"{method} {url.split('?')[0].replace(ARM, '')} -> {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json() if resp.content else {}
    except ValueError:
        return {}


def _step(name: str, fn) -> dict:
    try:
        detail = fn()
        return {"step": name, "status": "ok", "detail": detail}
    except Exception as exc:  # noqa: BLE001 -- every step becomes an explicit status; never silently skipped
        return {"step": name, "status": "error", "error": str(exc)[:400]}


def _require(config: DemoConfig, scenario: Scenario):
    if not config.enabled:
        raise ScenarioError("chaos is disabled -- set ZEROOPS_CHAOS_ENABLED=true to allow inject/cleanup")
    if not config.subscription_id:
        raise ScenarioError("no subscription configured (ZEROOPS_SUBSCRIPTION_ID or AZURE_SUBSCRIPTION_ID)")
    missing = [f"ZEROOPS_DEMO_{k.upper()}" for k in scenario.required_settings if not config.get(k)]
    if missing:
        raise ScenarioError(f"scenario {scenario.id!r} is not configured; missing {missing}")


# ─── Inject / cleanup per scenario ──────────────────────────────

def _open_door(config: DemoConfig, inject: bool) -> list:
    url = f"{ARM}{config.rg_path()}/providers/Microsoft.Network/networkSecurityGroups/{config.nsg}/securityRules/{CHAOS_NSG_RULE}"
    params = {"api-version": "2023-09-01"}
    if inject:
        body = {"properties": {
            "protocol": "Tcp", "sourceAddressPrefix": "*", "sourcePortRange": "*", "destinationAddressPrefix": "*",
            "destinationPortRange": "22", "access": "Allow", "direction": "Inbound", "priority": 110,
            "description": "ZeroOps demo: The Open Door",
        }}
        return [_step("open 22/tcp to the internet", lambda: _call("PUT", url, body=body, params=params).get("name"))]
    return [_step("delete open-door rule", lambda: _call("DELETE", url, params=params) or "deleted")]


def _bad_deploy(config: DemoConfig, inject: bool) -> list:
    url = f"{ARM}{config.rg_path()}/providers/Microsoft.Web/sites/{config.webapp}/config/web"
    params = {"api-version": "2023-12-01"}
    command = BAD_DEPLOY_COMMAND if inject else ""
    label = "push broken startup command" if inject else "restore startup command"

    def apply():
        _call("PATCH", url, body={"properties": {"appCommandLine": command}}, params=params)
        return command or "restored"
    return [_step(label, apply)]


def _storm_surge(config: DemoConfig, inject: bool) -> list:
    url = f"{ARM}{config.rg_path()}/providers/Microsoft.Web/serverfarms/{config.plan}"
    params = {"api-version": "2023-12-01"}
    sku = {"name": "P0v3", "tier": "Premium0V3", "capacity": 3} if inject else {"name": "B1", "tier": "Basic", "capacity": 1}
    tags = {**BASE_TAGS, "zeroops-scenario": "storm-surge", "change": "emergency-scale-out"} if inject else dict(BASE_TAGS)
    label = "emergency scale-out to P0v3 x3" if inject else "scale back to B1 x1"
    return [_step(label, lambda: _call("PATCH", url, body={"sku": sku, "tags": tags}, params=params).get("sku"))]


def _rogue_hotfix(config: DemoConfig, inject: bool) -> list:
    url = f"{ARM}{config.rg_path()}/providers/Microsoft.Storage/storageAccounts/{config.storage}"
    params = {"api-version": "2023-05-01"}
    if inject:
        body = {"properties": {"allowBlobPublicAccess": True, "minimumTlsVersion": "TLS1_0"},
                "tags": {**BASE_TAGS, "hotfix": "INC-0200-temp", "zeroops-scenario": "rogue-hotfix"}}
        label = "enable anonymous blob access + TLS 1.0"
    else:
        body = {"properties": {"allowBlobPublicAccess": False, "minimumTlsVersion": "TLS1_2"}, "tags": dict(BASE_TAGS)}
        label = "disable anonymous blob access + TLS 1.2"
    return [_step(label, lambda: _call("PATCH", url, body=body, params=params).get("properties", {}).get("minimumTlsVersion"))]


def _two_am_cert(config: DemoConfig, inject: bool) -> list:
    vault = f"https://{config.keyvault}.vault.azure.net"
    aa = f"{ARM}{config.rg_path()}/providers/Microsoft.Automation/automationAccounts/{config.automation}"
    aa_params = {"api-version": "2023-11-01"}
    steps = []
    if inject:
        expires = int((datetime.now(timezone.utc) + timedelta(hours=48)).timestamp())
        steps.append(_step("store TLS bundle expiring in 48h", lambda: _call(
            "PUT", f"{vault}/secrets/{CERT_SECRET_NAME}", scope=KV_SCOPE, params={"api-version": "7.4"},
            body={"value": "zeroops-demo-placeholder", "contentType": "application/x-pkcs12",
                  "attributes": {"exp": expires}, "tags": {"zeroops-scenario": "2am-cert"}},
        ).get("id", "").split("/")[-1]))
        location = lambda: _call("GET", aa, params=aa_params)["location"]  # noqa: E731

        def publish_runbook():
            loc = location()
            _call("PUT", f"{aa}/runbooks/{CERT_RUNBOOK_NAME}", params=aa_params, body={
                "location": loc, "properties": {"runbookType": "PowerShell", "logProgress": False, "logVerbose": False,
                                                "description": "ZeroOps demo: TLS renewal (fails on purpose)"}})
            _call("PUT", f"{aa}/runbooks/{CERT_RUNBOOK_NAME}/draft/content", params=aa_params, text=CERT_RUNBOOK_SCRIPT)
            _call("POST", f"{aa}/runbooks/{CERT_RUNBOOK_NAME}/publish", params=aa_params)
            return CERT_RUNBOOK_NAME

        steps.append(_step("publish renewal runbook", publish_runbook))
        steps.append(_step("start renewal job (fails)", lambda: _call(
            "PUT", f"{aa}/jobs/{uuid.uuid4()}", params=aa_params,
            body={"properties": {"runbook": {"name": CERT_RUNBOOK_NAME}}},
        ).get("properties", {}).get("status", "queued")))
    else:
        steps.append(_step("delete expiring TLS bundle", lambda: _call(
            "DELETE", f"{vault}/secrets/{CERT_SECRET_NAME}", scope=KV_SCOPE, params={"api-version": "7.4"}) and "deleted"))
        steps.append(_step("purge deleted bundle", lambda: _call(
            "DELETE", f"{vault}/deletedsecrets/{CERT_SECRET_NAME}", scope=KV_SCOPE, params={"api-version": "7.4"}) or "purged"))
    return steps


_HANDLERS = {
    "open-door": _open_door, "bad-deploy": _bad_deploy, "storm-surge": _storm_surge,
    "rogue-hotfix": _rogue_hotfix, "2am-cert": _two_am_cert,
}


def run(scenario_id: str, action: str, config: DemoConfig = None) -> dict:
    if action not in ("inject", "cleanup"):
        raise ValueError("action must be 'inject' or 'cleanup'")
    scenario = get_scenario(scenario_id)
    config = config or DemoConfig.from_env()
    _require(config, scenario)
    steps = _HANDLERS[scenario.id](config, action == "inject")
    ok = all(s["status"] == "ok" for s in steps)
    return {
        "scenario_id": scenario.id, "action": action, "status": "ok" if ok else "partial" if any(s["status"] == "ok" for s in steps) else "error",
        "steps": steps, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ─── Fast detection probes (read-only) ──────────────────────────
# Each probe looks for the *symptom* with an independent signal (Resource
# Graph fleet scan, HTTP health check, Key Vault / Automation state) rather
# than reading back what inject wrote. They run in seconds, which is what the
# console times; the Activity Log alert to the SRE Agent is the ~5 min backstop.

def _rg_rows(config: DemoConfig, query: str) -> list:
    from app.azure_data import query_resource_graph
    return query_resource_graph(query, config.subscription_id)


def _kql(value: str) -> str:
    return value.replace("'", "")


def _probe_open_door(config: DemoConfig) -> dict:
    rows = _rg_rows(config, (
        "Resources | where type =~ 'Microsoft.Network/networkSecurityGroups' "
        f"| where resourceGroup =~ '{_kql(config.resource_group)}' and name =~ '{_kql(config.nsg)}' "
        "| mvexpand rule=properties.securityRules "
        "| where rule.properties.access =~ 'Allow' and rule.properties.direction =~ 'Inbound' "
        "and tostring(rule.properties.sourceAddressPrefix) in ('*', 'Internet', '0.0.0.0/0') "
        "and tostring(rule.properties.destinationPortRange) in ('22', '3389', '*') "
        "| project nsg=name, rule=tostring(rule.name), port=tostring(rule.properties.destinationPortRange), "
        "priority=toint(rule.properties.priority)"))
    return {"detected": bool(rows), "source": "Azure Resource Graph (NSG drift scan)",
            "signal": (f"{len(rows)} inbound rule(s) open to the internet: "
                       + ", ".join(f"{r['rule']} port {r['port']}" for r in rows)) if rows else "no internet-exposed management ports",
            "evidence": rows[:5]}


def _probe_bad_deploy(config: DemoConfig) -> dict:
    url = f"https://{config.webapp}.azurewebsites.net/"
    try:
        resp = requests.get(url, timeout=8, allow_redirects=True)
        code, err = resp.status_code, ""
    except requests.RequestException as exc:
        code, err = None, type(exc).__name__
    detected = code is None or code >= 500
    signal = f"HTTP {code}" if code is not None else f"no response ({err})"
    return {"detected": detected, "source": "HTTP synthetic probe",
            "signal": f"{url} -> {signal}", "evidence": [{"url": url, "status": code, "error": err}]}


def _probe_storm_surge(config: DemoConfig) -> dict:
    rows = _rg_rows(config, (
        "Resources | where type =~ 'Microsoft.Web/serverfarms' "
        f"| where resourceGroup =~ '{_kql(config.resource_group)}' and name =~ '{_kql(config.plan)}' "
        "| project plan=name, sku=tostring(sku.name), tier=tostring(sku.tier), capacity=toint(sku.capacity), "
        "change=tostring(tags.change)"))
    hot = [r for r in rows if (r.get("sku") or "").upper() != "B1" or int(r.get("capacity") or 1) > 1]
    signal = (f"plan {hot[0]['plan']} is {hot[0]['sku']} x{hot[0]['capacity']} (baseline B1 x1)" if hot
              else "plan at baseline B1 x1")
    return {"detected": bool(hot), "source": "Azure Resource Graph (cost drift scan)", "signal": signal, "evidence": rows[:5]}


def _probe_rogue_hotfix(config: DemoConfig) -> dict:
    rows = _rg_rows(config, (
        "Resources | where type =~ 'Microsoft.Storage/storageAccounts' "
        f"| where resourceGroup =~ '{_kql(config.resource_group)}' and name =~ '{_kql(config.storage)}' "
        "| project account=name, publicBlob=tobool(properties.allowBlobPublicAccess), "
        "minTls=tostring(properties.minimumTlsVersion), hotfix=tostring(tags.hotfix)"))
    hot = [r for r in rows if r.get("publicBlob") or (r.get("minTls") or "TLS1_2") in ("TLS1_0", "TLS1_1")]
    if hot:
        r = hot[0]
        gaps = [g for g, on in (("anonymous blob access", r.get("publicBlob")),
                                (f"min TLS {r.get('minTls')}", r.get("minTls") in ("TLS1_0", "TLS1_1"))) if on]
        signal = f"storage {r['account']}: " + " + ".join(gaps)
    else:
        signal = "storage hardened (TLS 1.2, no anonymous access)"
    return {"detected": bool(hot), "source": "Azure Resource Graph (storage posture scan)", "signal": signal, "evidence": rows[:5]}


def _probe_two_am_cert(config: DemoConfig) -> dict:
    evidence, found = [], []
    try:
        secret = _call("GET", f"https://{config.keyvault}.vault.azure.net/secrets/{CERT_SECRET_NAME}",
                       scope=KV_SCOPE, params={"api-version": "7.4"})
        exp = (secret.get("attributes") or {}).get("exp")
        if exp:
            hours = (datetime.fromtimestamp(exp, timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 3600
            evidence.append({"secret": CERT_SECRET_NAME, "expires_in_hours": round(hours, 1)})
            if hours <= 7 * 24:
                found.append(f"TLS bundle expires in {hours:.0f}h")
    except ScenarioError as exc:
        if "-> 404" not in str(exc):
            raise
    jobs = _call("GET", f"{ARM}{config.rg_path()}/providers/Microsoft.Automation/automationAccounts/{config.automation}/jobs",
                 params={"api-version": "2023-11-01", "$filter": f"properties/runbook/name eq '{CERT_RUNBOOK_NAME}'"})
    failed = [j for j in jobs.get("value", []) if (j.get("properties") or {}).get("status") == "Failed"]
    if failed:
        found.append(f"renewal runbook {CERT_RUNBOOK_NAME} Failed ({len(failed)} job(s))")
        evidence.append({"runbook": CERT_RUNBOOK_NAME, "failed_jobs": len(failed)})
    return {"detected": bool(found), "source": "Key Vault expiry + Automation job state",
            "signal": "; ".join(found) or "no expiring bundle or failed renewal", "evidence": evidence}


_PROBES = {
    "open-door": _probe_open_door, "bad-deploy": _probe_bad_deploy, "storm-surge": _probe_storm_surge,
    "rogue-hotfix": _probe_rogue_hotfix, "2am-cert": _probe_two_am_cert,
}


def probe(scenario_id: str, config: DemoConfig = None) -> dict:
    """Read-only symptom check; does not require ZEROOPS_CHAOS_ENABLED."""
    import time
    scenario = get_scenario(scenario_id)
    config = config or DemoConfig.from_env()
    if not config.subscription_id:
        raise ScenarioError("no subscription configured (ZEROOPS_SUBSCRIPTION_ID or AZURE_SUBSCRIPTION_ID)")
    missing = [f"ZEROOPS_DEMO_{k.upper()}" for k in scenario.required_settings if not config.get(k)]
    if missing:
        raise ScenarioError(f"scenario {scenario.id!r} is not configured; missing {missing}")
    started = time.monotonic()
    try:
        result = _PROBES[scenario.id](config)
    except Exception as exc:  # noqa: BLE001 -- probe failure is an explicit status, not a silent "not detected"
        raise ScenarioError(f"probe failed: {str(exc)[:300]}") from exc
    return {"scenario_id": scenario.id, "tier": scenario.tier, **result,
            "probe_ms": int((time.monotonic() - started) * 1000),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
