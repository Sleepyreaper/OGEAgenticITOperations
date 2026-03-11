# Decision Log

| # | Date | Decision | Rationale |
|---|------|----------|-----------|
| 1 | 2026-03-11 | Use Tailwind CSS for prototype UI | Fast iteration, utility-first, no custom CSS framework needed |
| 2 | 2026-03-11 | Target Azure for cloud services | Customer alignment with Azure ecosystem |
| 3 | 2026-03-11 | Prioritize Troubleshooting scenario for PoC | Highest org impact per customer; solves access-lobbying problem |
| 4 | 2026-03-11 | Recommend custom agent (Option A) over waiting for Copilot Agents preview | No dependency on Mandy's team or tenant-scope changes; fully within Cloud Ops control |
| 5 | 2026-03-11 | Use Managed Identity + Reader RBAC for agent auth | Aligns with least-privilege governance; no credential management |
