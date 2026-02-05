# Roadmap: Games Research Tool

## Overview

This roadmap delivers the foundational MCP server that connects Claude/Cursor to Steam's game market data. Phase 1 establishes the MCP protocol layer, implements two core research tools (genre search and metadata fetching), and builds the shared HTTP infrastructure that all future data sources will use. This phase transforms "manually scraping Steam for genre data" into "ask Claude for a market analysis and get structured, validated results."

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: MCP Foundation & Steam API Integration** - Working MCP server with genre search and metadata tools

## Phase Details

### Phase 1: MCP Foundation & Steam API Integration
**Goal**: Claude/Cursor can search Steam games by genre and retrieve metadata through a working MCP server

**Depends on**: Nothing (first phase)

**Requirements**: MCP-01, MCP-02, MCP-03, MCP-04, MCP-05, TOOL-01, TOOL-02, INFRA-01, INFRA-02, INFRA-03, VAL-01, VAL-02

**Success Criteria** (what must be TRUE):
  1. User can ask Claude "search for roguelike games" and receive a list of Steam AppIDs
  2. User can ask Claude "get metadata for AppID 646570" and receive game name, tags, developer, and description
  3. Invalid requests (malformed AppIDs, missing parameters) return clear error messages, not crashes
  4. Steam API rate limits are respected automatically (no manual delay insertion needed)
  5. All MCP server logs appear in stderr only (stdout shows clean JSON-RPC communication)

**Plans**: TBD (to be defined during planning phase)

Plans:
- [ ] TBD during `/gsd:plan-phase 1`

## Progress

**Execution Order:**
Phases execute in numeric order.

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. MCP Foundation & Steam API Integration | 0/TBD | Not started | - |
