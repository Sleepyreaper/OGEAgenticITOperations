# Cloud Weather Ops — Decision Log

| # | Date | Decision | Rationale |
|---|------|----------|-----------|
| 1 | 2026-03-11 | Use Tailwind CSS for dashboard UI | Fast iteration, utility-first, custom-branded |
| 2 | 2026-03-11 | Target Azure, reuse existing OpenAI + App Service Plan | Customer alignment, cost efficiency |
| 3 | 2026-03-11 | Build custom agent solution (not wait for Copilot Agents preview) | No dependency on Mandy's team or tenant-scope changes; fully within Cloud Ops control |
| 4 | 2026-03-11 | Use Managed Identity + Reader RBAC | Aligns with organizational least-privilege governance; no credential management |
| 5 | 2026-03-11 | Use foundry-gpt for reasoning agents (cost + diagnostics) | Deep reasoning over operational data at low cost vs. gpt-4o |
| 6 | 2026-03-11 | Name agents after industry operations concepts | Resonate with customer culture — configurable per engagement |
| 7 | 2026-03-11 | Implement multi-round debate system | Agents that argue produce better recommendations than single-agent answers; transparent disagreement builds trust |
| 8 | 2026-03-11 | Split into Executive Reliability view + Ops Center | Two audiences (Rick vs Christopher/Shane) need different perspectives on same data |
| 9 | 2026-03-11 | Use SSE streaming for agent responses | Real-time chat experience — watch the grid team debate as it happens |
| 10 | 2026-03-11 | Auto-refresh every 60s using free Resource Graph queries | Near-real-time monitoring at zero cost; change detection for chaos demo |
| 11 | 2026-03-11 | Build deep intelligence beyond Azure Advisor | Cross-resource correlation, architecture smells, blast radius — things Advisor can't do |
| 12 | 2026-03-11 | Integrate Azure Service Health + Resource Health | Show which of YOUR resources are affected by platform incidents |
| 13 | 2026-03-11 | Add chaos demo ("Do Something Stupid") | Proves near-real-time detection; memorable demo moment |
| 14 | 2026-03-11 | Add Generate Remediation button | Bridges insight → action; produces Terraform/CLI following organizational standards |
| 15 | 2026-03-11 | Skip Chaos Studio, use Resource Graph for detection | Chaos Studio needs HA infra to test against ($200+/mo); our approach costs $0 and detects in 10 seconds |
| 16 | 2026-03-11 | Deploy to West US 2, OpenAI stays in West US 3 | Match customer's existing infrastructure region |
| 17 | 2026-03-17 | Add The Regulator agent for continuous compliance | Addresses Rick/Shane's ask — classifies policy violations as definition bugs, misconfigurations, valid exemptions, or workaround abuse |
| 18 | 2026-03-17 | Upgrade to foundry-gpt + foundry-reasoning from previous models | Significantly better reasoning and synthesis quality; Significantly better reasoning quality |
| 19 | 2026-03-17 | Per-agent endpoint routing (multi-region) | Models aren't all available in one region; agents route to westus3 or eastus2 based on model availability |
| 20 | 2026-03-17 | Phase 2: ADO integration with human-in-the-loop | Inspector classifications → proposals → human approval → ADO work items/PRs. System never auto-creates without human gate. |
| 21 | 2026-03-17 | Proposal-based ADO workflow (not direct creation) | Inspector generates PROPOSALS that humans review. Approved proposals generate ADO REST API payloads. Rejected proposals archived with reason. Separation of AI reasoning from action. |
| 22 | 2026-03-17 | ADO Grid Dispatch with 3-stage deploy (build → staging → prod) | Build auto-triggers on push. Staging auto-deploys on main merge. Production requires human approval via ADO Environment check. |
| 23 | 2026-03-17 | Subscription RBAC as reusable Bicep module | `subscription-rbac.bicep` grants Reader + Log Analytics Reader + Monitoring Reader at subscription scope. Deploy once per monitored subscription. Makes multi-env deployment repeatable. |
| 24 | 2026-03-17 | In-memory proposal store for PoC, Cosmos DB for production | Proposals stored in-memory for demo. Production path: persist to Cosmos DB or blob storage with TTL. |
| 25 | 2026-03-17 | PAT-based ADO auth for PoC, Workload Identity Federation for production | PAT stored in Key Vault for demo. Production: Managed Identity + ADO service connection via Workload Identity Federation — no secrets. |
