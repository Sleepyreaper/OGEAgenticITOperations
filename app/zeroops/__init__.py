"""ZeroOps: Azure SRE Agent (tier 1) + OGE agent squad (tier 2).

The Azure SRE Agent watches repos, logs and monitoring and resolves the
runbook-shaped incidents itself. When it can't, it escalates to the OGE
agent squad over MCP (``app/zeroops/mcp_server.py``). The squad reasons over
grounded evidence, produces a fix/script/resolution, and a human approves it.

See docs/ZEROOPS_SRE_AGENT.md.
"""
