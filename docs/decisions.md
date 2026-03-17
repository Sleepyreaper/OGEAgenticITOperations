# OGE Ops Council — Decision Log

| # | Date | Decision | Rationale |
|---|------|----------|-----------|
| 1 | 2026-03-11 | Use Tailwind CSS for dashboard UI | Fast iteration, utility-first, OGE-branded |
| 2 | 2026-03-11 | Target Azure, reuse existing OpenAI + App Service Plan | Customer alignment, cost efficiency |
| 3 | 2026-03-11 | Build custom agent solution (not wait for Copilot Agents preview) | No dependency on Mandy's team or tenant-scope changes; fully within Cloud Ops control |
| 4 | 2026-03-11 | Use Managed Identity + Reader RBAC | Aligns with OGE least-privilege governance; no credential management |
| 5 | 2026-03-11 | Use o4-mini for reasoning agents (cost + diagnostics) | Deep reasoning over operational data at low cost vs. gpt-4o |
| 6 | 2026-03-11 | Name agents after OGE oil & gas operations concepts | Resonate with customer culture — Pipeline, Barrel Counter, Roughneck, Turnaround, Flare Stack, The Inspector |
| 7 | 2026-03-11 | Implement multi-round debate system | Agents that argue produce better recommendations than single-agent answers; transparent disagreement builds trust |
| 8 | 2026-03-11 | Split into Executive Reliability view + Ops Center | Two audiences (Rick vs Christopher/Shane) need different perspectives on same data |
| 9 | 2026-03-11 | Use SSE streaming for agent responses | Real-time chat experience — watch the crew debate as it happens |
| 10 | 2026-03-11 | Auto-refresh every 60s using free Resource Graph queries | Near-real-time monitoring at zero cost; change detection for chaos demo |
| 11 | 2026-03-11 | Build deep intelligence beyond Azure Advisor | Cross-resource correlation, architecture smells, blast radius — things Advisor can't do |
| 12 | 2026-03-11 | Integrate Azure Service Health + Resource Health | Show which of YOUR resources are affected by platform incidents |
| 13 | 2026-03-11 | Add chaos demo ("Do Something Stupid") | Proves near-real-time detection; memorable demo moment |
| 14 | 2026-03-11 | Add Generate Remediation button | Bridges insight → action; produces Terraform/CLI following OGE standards |
| 15 | 2026-03-11 | Skip Chaos Studio, use Resource Graph for detection | Chaos Studio needs HA infra to test against ($200+/mo); our approach costs $0 and detects in 10 seconds |
| 16 | 2026-03-11 | Deploy to West US 2, OpenAI stays in West US 3 | Match customer's existing infrastructure region |
| 17 | 2026-03-17 | Add The Inspector agent for continuous compliance | Addresses Rick/Shane's ask — classifies policy violations as definition bugs, misconfigurations, valid exemptions, or workaround abuse |
| 18 | 2026-03-17 | Upgrade to gpt-5.4 + o3 from gpt-4.1 + o4-mini | Significantly better reasoning and synthesis quality; gpt-5.4-pro doesn't support chat completions (Responses API only) |
| 19 | 2026-03-17 | Per-agent endpoint routing (multi-region) | Models aren't all available in one region; agents route to westus3 or eastus2 based on model availability |
