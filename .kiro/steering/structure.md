---
inclusion: always
---
# Monorepo Directory Structure & Module Boundaries

```text
backend/
├── config/                      # Django settings, asgi.py (Channels), celery.py, api.py (NinjaAPI root)
├── apps/
│   ├── projects/                # Project model, repo cloning, workspace lifecycle
│   ├── ingestion/               # GitPython branch analyzer, Tree-sitter AST parser, doc extractor
│   ├── genome/                  # IntentNode, EvidenceCitation, ConflictRecord models + Reconciler
│   ├── revival/                 # RevivalStrategy generator, feasibility math scorer, .kiro spec exporter
│   ├── foundry/                 # 4-Agent Foundry loop (Environment, Architecture, Engineering, Verification)
│   ├── rebirth/                 # RebirthArtifact model, PR diff bundle, Continuity Memory, preview server
│   └── telemetry/               # Django Channels WebSocket consumers & event broadcaster
├── mcp_server/                  # Standalone Python MCP server exposing Lazarus Forensic tools to Kiro
└── tests/
    └── fixtures/
        └── build_paypulse_fixture.py  # Generates the Git repo fixture with branches at runtime

frontend/
├── src/
│   ├── app/
│   │   ├── page.tsx             # Landing + 1-Click "Resurrect Patient Zero (PayPulse)" trigger
│   │   └── foundry/[projectId]/
│   │       └── page.tsx         # Unified Command Center (Tabs: Autopsy, Defibrillator, Heartbeat)
│   ├── components/
│   │   ├── autopsy/             # React Flow Genome Graph, Evidence Drawer, Conflict Inspector
│   │   ├── defibrillator/       # Strategy Comparison Cards, Feasibility Radar, Human Approval Gate
│   │   └── heartbeat/           # Live WebSocket Terminal, Test Runner Matrix, Split-Screen Rebirth View
│   └── lib/
│       ├── types.ts             # Shared TypeScript interfaces matching Django Ninja schemas
│       ├── api.ts               # Typed fetch client
│       └── ws.ts                # Reconnecting WebSocket telemetry hook
```
