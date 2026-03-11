# OGE Ops Council — Demo Script for 3/19

> **Audience**: Rick + OGE leadership
> **Duration**: ~15 minutes
> **Presenter**: Christopher Smith
> **Goal**: Show that Azure AI delivers immediate, visible operational value that ServiceNow and Kiro can't match for their Cloud Ops use case.

---

## Opening (1 minute)

**Say**: "What you're about to see was purpose-built for OGE Cloud Operations. It addresses a specific challenge your teams face every day: getting operational answers without needing elevated access. We call it the Ops Council — five AI specialists, each named after OGE operations concepts, that debate each other and deliver balanced recommendations."

**Do**: Open the dashboard at the live URL. Show the environment overview.

---

## Act 1: Meet the Crew (2 minutes)

**Do**: Click the **"The Crew"** tab.

**Say**: "Each agent has a specific role and personality — and they're built to disagree with each other productively."

Walk through quickly:
- ⚡ **Pipeline** — coordinates everything, synthesizes the crew's takes
- 🛢️ **Barrel Counter** — every dollar is a barrel. Finds waste.
- 🔧 **The Roughneck** — the veteran who built this place. Knows *why* things are sized the way they are.
- 🔄 **Turnaround** — named after the most critical refinery event. Diagnoses issues.
- 🔥 **Flare Stack** — early warning system. When it lights up, something needs attention.

**Say**: "The tension between Barrel Counter and The Roughneck is the whole point. One wants to save money. The other knows why you spent it. Pipeline makes sure you see both sides."

---

## Act 2: The Money Demo — VM Sizing Debate (4 minutes)

**Do**: Click the **"Why is this VM so big?"** demo scenario.

**Say**: "A DevOps team member asks: 'Why is our SAP batch VM running on a D16s_v5? Can we save money?' Watch what happens."

**Wait for agents to respond.** Point out:
1. **Barrel Counter** identifies the VM averages 11.8% CPU. Recommends downsizing. Shows the exact math and SKU alternatives.
2. **The Roughneck** pushes back: "This VM peaks at 94% CPU on the last Friday of every month for SAP batch processing. Touch it and you break month-end close."
3. **Pipeline** synthesizes: "Here are three options with tradeoffs — including a B-series burstable compromise."

**Say**: "That right there is something a single AI can't do. Cost optimization *without* context causes outages. The Roughneck provides the context that keeps the lights on. And the user sees both perspectives transparently."

---

## Act 3: The Killer Use Case — Troubleshooting Without Access (3 minutes)

**Do**: Click the **"My deployment failed"** demo scenario.

**Say**: "This is the use case Christopher described as 'a massive win.' A team's Terraform deployment failed. They have Reader access in Test — they can't see the detailed activity logs. Today, they open a ticket and wait. With the Ops Council..."

**Wait for Turnaround to respond.** Point out:
1. **Timeline** of exactly what happened — timestamps, operations, status
2. **Root cause**: subnet modification blocked by VNet peering
3. **Remediation**: specific steps the team can take, and what needs Cloud Ops intervention

**Say**: "The team got a full incident write-up in seconds. No access escalation. No ticket queue. No waiting. Turnaround *is* their elevated access."

---

## Act 4: Real Data (2 minutes)

**Do**: Toggle **"Live Azure"** in the top nav.

**Say**: "Everything you've seen so far uses demo data that represents OGE's environment. But watch this."

**Wait for the scan.** The dashboard updates with real subscription data.

**Do**: Click one of the live findings (e.g., non-compliant resource groups or public IPs). Let the crew analyze it.

**Say**: "That's not canned. The agents just queried your Azure subscription through Resource Graph, analyzed what they found, and gave you a recommendation — all through a Managed Identity with Reader access only."

---

## Act 5: Governance & Security (2 minutes)

**Say**: "Let me address the elephant in the room — what access does this need?"

Reference the key points:
- **5 roles on 1 Managed Identity**
- 3 read-only at subscription scope (Reader, Log Analytics Reader, Monitoring Reader)
- 2 resource-scoped (Key Vault Secrets User, OpenAI User)
- **Zero write permissions anywhere**
- **Zero changes to any team's existing access**
- No passwords — Managed Identity with Entra ID tokens
- Key Vault behind private endpoint, no public access

**Say**: "This solution *reinforces* your governance model. It doesn't bypass it. Teams get insight without access. That's the design principle."

---

## Closing (1 minute)

**Say**: "ServiceNow automates your help desk. Kiro writes your code. This gives your Cloud Ops team a crew of AI specialists who know your environment, respect your standards, and debate each other so you get balanced recommendations — all without anyone needing more access than they have today."

**Say**: "And we built this in a day because when you combine Azure's AI platform with an understanding of your actual operational challenges, you get purpose-built solutions, not generic products."

---

## Q&A Prep

| Likely Question | Answer |
|----------------|--------|
| "Can this work across our production subscriptions?" | Yes — add the 3 Reader roles per subscription to the Managed Identity. The architecture scales horizontally. |
| "What models does it use?" | o4-mini for deep reasoning (cost analysis, diagnostics), gpt-4.1 for broad knowledge (standards), gpt-4.1-mini for fast coordination, gpt-5-nano for lightweight monitoring. All on your existing Azure OpenAI deployment. |
| "Can we feed it our Terraform standards?" | Absolutely — The Roughneck's system prompt is where organizational knowledge lives. We'd ground it with your actual standards docs, module library, and naming conventions. |
| "What about cost data?" | We can add Cost Management Reader role and wire in Azure Cost Management APIs. Barrel Counter is already designed for it — we just need the RBAC assignment. |
| "How does this compare to Azure Copilot Agents?" | Azure Copilot Agents require the preview to be enabled at tenant scope (Mandy's team). This runs today, no preview needed, no tenant-scope changes. If the preview gets enabled later, this is complementary. |
| "Can it take action, not just recommend?" | By design, it recommends but doesn't act — that's governance-first. Phase 2 could add approval workflows where a recommendation triggers a Terraform PR that goes through your normal change process. |
| "What does this cost to run?" | The Web App runs on your existing P0v3 plan. OpenAI costs are per-token — a typical agent council call uses ~4K-8K tokens total across all agents. At Azure OpenAI pricing, that's pennies per interaction. |
