# Games Research Tool

## What This Is

An MCP (Model Context Protocol) server that feeds game market data into Claude/Cursor for genre-focused market analysis. Pulls numerical and qualitative data from multiple sources (Steam, SteamDB, Gamalytic, etc.) to generate data-driven reports on game genres, market opportunities, and competitive landscapes.

## Core Value

Enable rapid, data-backed answers to "Is this genre a gold mine or a graveyard?" — unified data access that powers the full research workflow from market sizing to opportunity identification.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] MCP server skeleton that connects to Claude/Cursor
- [ ] Steam Official API integration (fetch games by tag, basic metadata, AppIDs)
- [ ] Data validation — confirm successful data retrieval each request
- [ ] SteamDB scraping for commercial data (revenue estimates, peak CCU, pricing)
- [ ] Gamalytic integration for revenue triangulation
- [ ] Report generation tooling via MCP

### Out of Scope

- [ ] Multi-user support — personal research tool only
- [ ] Web UI — Claude/Cursor is the interface
- [ ] Real-time dashboards — batch research, not live monitoring
- [ ] Reddit/Twitch/YouTube integration — defer until core market data works

## Context

### Research Framework (4 Phases)

The tool supports a structured research workflow:

1. **Phase 1 - Macro Market Reality:** Total market size, revenue concentration, temporal trends, success benchmarks
2. **Phase 2 - Psychology Engine:** Engagement drivers, genre-specific frameworks, retention analysis (refund window)
3. **Phase 3 - Opportunity Matrix:** Tag multipliers, untapped hybrids, theme gaps (blue oceans)
4. **Phase 4 - Competitive Analysis:** Production multipliers, winner reverse-engineering, failure modes

### Data Ingestion Layers

- **Metadata Layer:** Steam Official API → AppIDs by tag, game metadata
- **Commercial Layer:** SteamDB + Gamalytic → revenue estimates, pricing tiers, triangulation
- **Engagement Layer:** SteamDB → Peak CCU, review multipliers (30x-50x reviews = sales estimate)

### Report Structure

Final output is a structured report:
- Executive Summary (1-page winning formula + recommended opportunity)
- Market Structure (revenue tiers, concentration, trends)
- Psychological Profiling (why the genre works)
- Opportunity Matrix (data-backed game ideas)
- Design & Development Strategy (budgeting, validation roadmap)

## Constraints

- **Tech Stack:** Python — preferred language, good MCP SDK support via `mcp` package
- **Starting Point:** Steam Official API first (stable, provides foundation AppIDs)
- **Scraping Fragility:** SteamDB has no public API — scraping added in Step 2, not Step 1
- **Rate Limits:** Steam API has 200 requests/5 min on some endpoints
- **Data Accuracy:** Revenue estimates are approximations — triangulation improves confidence

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Python for MCP | Preferred language, good SDK support via `mcp` package | — Pending |
| Steam API before SteamDB | Stable foundation, need AppIDs first | — Pending |
| Personal tool, no multi-user | Faster iteration, complexity reduction | — Pending |
| Skip Reddit/Twitch/YouTube initially | Focus on core market data that powers Phase 1-3 | — Pending |

---
*Last updated: 2025-02-05 after initialization*
