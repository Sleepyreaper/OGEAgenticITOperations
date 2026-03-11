# OGE Ops Council — Demo Script for 3/19

> **Audience**: Rick + OGE leadership
> **Duration**: ~15 minutes
> **Presenter**: Christopher Smith / Brad Allen
> **Goal**: Show Azure AI delivers immediate, visible operational value that ServiceNow and Kiro can't match

---

## Opening (1 minute)

**Say**: "What you're about to see was purpose-built for OGE Cloud Operations in a day. Five AI specialists — each named after OGE operations concepts — that debate each other and deliver balanced recommendations. Two views: one for leadership, one for engineering."

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

**Say**: "The tension between Barrel Counter and The Roughneck is the whole point. One wants to save money. The other knows why you spent it. Pipeline makes sure you see both sides."

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

## Act 5: Chaos Demo (3 minutes) — THE SHOWSTOPPER

**Do**: Go back to **Ops Center**. Click **"💥 Do Something Stupid"**.

**Say**: "I'm about to open SSH to the entire internet on a real NSG. This is the kind of mistake that happens with a fat-fingered Terraform apply or a compromised service principal. Watch how fast the Ops Council catches it."

**Wait 10 seconds.** Badge flashes "⚡ CHANGE DETECTED". Crew auto-dispatches with security analysis.

**Do**: Click **"🧹 Clean Up"** to restore.

**Say**: "10 seconds. That's how fast this detects a security breach. Resource Graph is free. The AI analysis cost 3 cents. Try doing that with ServiceNow."

---

## Act 6: Morning Briefing (1 minute)

**Do**: Click **"☀️ Morning Briefing"**.

**Say**: "Every morning, the crew scans overnight and delivers a briefing: what's the top priority, what's on the watch list, what's clear. No dashboards to check. No tickets to review. The crew already did it."

---

## Closing (30 seconds)

**Say**: "ServiceNow automates your help desk. Kiro writes your code. This gives your Cloud Ops team a crew of AI specialists that know your environment, respect your standards, debate each other, and produce remediation code — all without anyone needing more access than they have today. And we built it in a day."

---

## Q&A Prep

| Question | Answer |
|----------|--------|
| "What does this cost?" | ~$0.10-0.50/day. Scanning is free (Resource Graph). AI tokens are pennies per interaction. The token cost is visible on every query. |
| "What access does it need?" | 5 read-only roles on 1 Managed Identity. Zero write permissions. Zero changes to anyone's existing access. See the RBAC guide. |
| "Can this scale to all our subscriptions?" | Yes — grant the 3 Reader roles per subscription. Resource Graph queries across all subs natively. |
| "Can we feed it our Terraform standards?" | Yes — The Roughneck's system prompt is where org knowledge lives. Ground it with your naming conventions, module library, tagging standards. |
| "How does this compare to Azure Copilot Agents?" | Copilot Agents require tenant-scope preview enablement (Mandy's team). This runs today, no preview needed. Complementary if the preview becomes available. |
| "Can it take action?" | By design, it recommends but never acts — governance first. Phase 2 could add approval workflows (Terraform PR → normal change process). |
| "What about the RBAC role classification Christopher mentioned?" | Great Phase 2 candidate. The architecture supports it — add a new crew member that specializes in RBAC analysis. |
| "How fast does it detect changes?" | Resource Graph: 5-15 seconds. Activity Log: 1-2 minutes. Advisor: ~24 hours. Our chaos demo proves the speed live. |
| "Is the executive score real?" | Yes — calculated from Resource Health, Service Health, security drift, tagging compliance, architecture analysis. All sourced from Azure APIs, not AI opinions. |
