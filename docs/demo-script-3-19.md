# OGE Ops Council — Demo Script for 3/19

> **Audience**: Rick + OGE leadership
> **Duration**: ~20 minutes
> **Presenter**: Christopher Smith / Brad Allen
> **Goal**: Show Azure AI delivers immediate, visible operational value — AND responds directly to Rick/Shane's continuous compliance ask

---

## Opening: What You Asked For (1 minute)

**Say**: "Before we show anything, let's start with what you asked us to solve."

**Put up the three asks** (say them, don't read a slide):

1. **"Diagnosing issues without giving users elevated access to Test/Prod would be a massive win."**
2. **"AI that enhances productivity while reinforcing standards and governance policy."**
3. **"Can an AI agent continuously watch for Azure Policy non-compliance, reason about why resources are non-compliant, and route fixes — either a PR to fix the policy or a PBI to investigate the workaround?"**

**Say**: "Everything you're about to see was built to answer those three questions. Six AI specialists, named after OGE operations. They debate each other. They disagree. And they produce actionable output — not just dashboards."

---

## Act 1: Executive Reliability (2 minutes)

**Do**: Open the **Reliability** tab. Toggle to **Live Azure**.

The reliability score animates. Four pillars populate: Security, Governance, Resilience, Cost Efficiency.

**Say**: "This is the leadership view. One number tells you if your Azure estate is healthy. Four pillars break it down. The scores are calculated from real data — not surveys, not estimates. Resource Graph, Resource Health, Service Health, Azure Advisor — all feeding this in real time."

**Point out**: Azure Service Health panel showing real incidents. Click one — "This App Service incident in West US 2 — which of OUR resources are at risk? What should we fund to improve resilience?"

---

## Act 2: Ops Center (2 minutes)

**Do**: Click **Ops Center** tab.

**Say**: "Same data, different lens. This is what Christopher's team uses day-to-day."

**Point out**:
- Flare Stack Live Scan findings (orphaned disks, public IPs, insecure storage, architecture smells)
- Each finding has an "Investigate" button
- Auto-refresh timestamp updating every 60 seconds

---

## Act 3: Meet the Crew (2 minutes)

**Do**: Click **The Crew** tab. Walk through quickly.

- ⚡ **Pipeline** — coordinates everything
- 🛢️ **Barrel Counter** — every dollar is a barrel
- 🔧 **The Roughneck** — knows why things are built this way
- 🔄 **Turnaround** — diagnoses issues at 3 AM without anyone needing admin access
- 🔥 **Flare Stack** — early warning system
- 📋 **The Inspector** — **NEW** — continuous compliance, classifies violations, recommends PR or PBI

**Say**: "The tension between Barrel Counter and The Roughneck is the whole point. One wants to save money. The other knows why you spent it. Pipeline makes sure you see both sides. And The Inspector — our newest crew member — is the direct response to what Rick and Shane asked for: continuous compliance intelligence."

---

## Act 4: The Debate (4 minutes)

**Do**: Click **"Why is this VM so big?"** demo scenario. Watch the crew debate in real-time.

**Point out as messages stream in**:
1. Barrel Counter leads with the dollar figure
2. The Roughneck pushes back — "this VM peaks at 94% for SAP batch"
3. Round 2 rebuttals fly back and forth
4. Pipeline synthesizes with tradeoffs

**After synthesis**: Click **"🔧 Generate Terraform / CLI Fix"**

**Say**: "That's not just a recommendation — that's production-ready Terraform following OGE standards. From 'why is this big?' to 'here's the code to fix it' in under a minute."

---

## Act 5: Compliance Inspection (3 minutes) — RICK/SHANE'S ASK

**Do**: Click the **"📋 Compliance inspection"** demo scenario.

**Say**: "This is what Rick and Shane asked about — continuous compliance. The Inspector scans Azure Policy state, finds non-compliant resources, and classifies every violation."

**Point out as messages stream in**:
1. The Inspector leads — classifies each violation: policy bug, misconfiguration, valid exemption, or workaround abuse
2. The Roughneck pushes back on one — "that storage account needs HTTP for the legacy SFTP gateway, the policy doesn't account for that pattern"
3. Barrel Counter quantifies the cost of remediating vs the risk of not remediating

**After synthesis**: 

**Say**: "Watch what Pipeline did: for the storage account, he classified it as a **policy bug** — the built-in policy doesn't handle the SFTP gateway pattern. Fix: create a custom policy with an exemption tag. For the AKS cluster with the expired exemption, he classified it as **workaround abuse** — the CI/CD pipeline should have moved to workload identity six months ago. Fix: create a PBI, priority high, this sprint."

**Key message**: "This is what you asked for: an AI that doesn't just flag non-compliance — it REASONS about whether the policy is wrong or the resource is wrong. That's the difference between a compliance dashboard and an AI compliance inspector."

---

## Act 6: Chaos Demo (2 minutes) — THE SHOWSTOPPER

**Do**: Go back to **Ops Center**. Click **"💥 Do Something Stupid"**.

**Say**: "I'm about to open SSH to the entire internet on a real NSG. This is the kind of mistake that happens with a fat-fingered Terraform apply or a compromised service principal. Watch how fast the Ops Council catches it."

**Wait 10 seconds.** Badge flashes "⚡ CHANGE DETECTED". Crew auto-dispatches with security analysis.

**Do**: Click **"🧹 Clean Up"** to restore.

**Say**: "10 seconds. That's how fast this detects a security breach. Resource Graph is free. The AI analysis cost 3 cents. Try doing that with ServiceNow."

---

## Act 7: Morning Briefing (1 minute)

**Do**: Click **"☀️ Morning Briefing"**.

**Say**: "Every morning, the crew scans overnight and delivers a briefing: what's the top priority, what's on the watch list, what's clear. No dashboards to check. No tickets to review. The crew already did it."

---

## Closing: What Was Accomplished (1 minute)

**Say**: "Let's go back to those three asks."

**Ask #1: Diagnosing without elevated access.**
"Turnaround just diagnosed a deployment failure from Activity Logs and Resource Health — the team never needed more than Reader. The Managed Identity reads on their behalf. Zero access changes."

**Ask #2: AI that reinforces standards and governance.**
"Barrel Counter wanted to downsize that VM. The Roughneck said no — it peaks at 94% on month-end batch. The system didn't just give an answer, it gave you both sides and let you decide. That IS governance reinforcement."

**Ask #3: Continuous compliance with policy reasoning.**
"The Inspector classified five violations in under a minute. Two were policy bugs — the built-in definitions don't account for your architecture patterns. One was an expired exemption that's now workaround abuse. He didn't just flag red — he told you WHY and WHAT to do: PR for the policy fix, PBI for the workaround."

**Close**: "ServiceNow automates your help desk. Kiro writes your code. This gives your Cloud Ops team a crew of AI specialists that know your environment, respect your standards, debate each other, and produce remediation code — all without anyone needing more access than they have today. And we built the compliance scenario you asked about in your email — it's live, right now, at that URL."

---

## Q&A Prep

| Question | Answer |
|----------|--------|
| "What does this cost?" | ~$0.15-0.75/day. Scanning is free (Resource Graph). Reasoning models cost a few cents per query. The token cost is visible on every interaction. |
| "What access does it need?" | 5 read-only roles on 1 Managed Identity. Zero write permissions. Zero changes to anyone's existing access. See the RBAC guide. |
| "Can this scale to all our subscriptions?" | Yes — grant the 3 Reader roles per subscription. Resource Graph queries across all subs natively. |
| "Can we feed it our Terraform standards?" | Yes — The Roughneck's system prompt is where org knowledge lives. Ground it with your naming conventions, module library, tagging standards. |
| "How does this compare to Azure Copilot Agents?" | Copilot Agents require tenant-scope preview enablement (Mandy's team). This runs today, no preview needed. Complementary if the preview becomes available. |
| "Can it take action?" | By design, it recommends but never acts — governance first. Phase 2 could add approval workflows (Terraform PR → normal change process). |
| "How does the compliance piece connect to ADO?" | The Inspector classifies violations and outputs the exact PR or PBI content. Phase 2 connects to Azure DevOps APIs to create the branch/PR automatically for policy bug fixes, or create the PBI for workaround abuse. The reasoning is the hard part — the plumbing is straightforward. |
| "Can it handle custom policies, not just built-in?" | Yes. The Inspector reads whatever Azure Policy is assigned — built-in, custom, or initiative. If you have custom policy definitions in a repo, we can ground it with your policy-as-code patterns. |
| "What happens when an exemption expires?" | The Inspector checks exemption status as part of every scan. Expired exemptions are flagged as workaround abuse with a recommendation to either renew with justification or remediate the underlying issue. |
| "What about the RBAC role classification Christopher mentioned?" | Great Phase 2 candidate. The architecture supports it — add a new crew member that specializes in RBAC analysis. |
| "How fast does it detect changes?" | Resource Graph: 5-15 seconds. Activity Log: 1-2 minutes. Advisor: ~24 hours. Our chaos demo proves the speed live. |
| "Is the executive score real?" | Yes — calculated from Resource Health, Service Health, security drift, tagging compliance, architecture analysis. All sourced from Azure APIs, not AI opinions. |
