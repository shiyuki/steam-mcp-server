# Stack Research

**Domain:** MCP server -- game market research tool (Steam API + SteamDB/Gamalytic scraping)
**Researched:** 2026-02-05
**Confidence:** HIGH (core MCP + scraping stack verified via official sources); MEDIUM (Steam API wrapper selection -- small ecosystem, limited adoption signals)

---

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| `@modelcontextprotocol/sdk` | 1.26.0 (v1.x branch) | MCP server/client protocol implementation | Official SDK from the MCP org. v1.x is explicitly the production-stable line. v2 is pre-alpha and not expected stable until Q1 2026. 23,000+ projects depend on this package. Use v1.x until v2 stabilizes. |
| `zod` | 4.3.5 | Schema validation for all tool input/output contracts | Required peer dependency of `@modelcontextprotocol/sdk`. Not optional. v4 shipped July 2025 with 14x faster string parsing vs v3. Use v4 -- the MCP SDK requires it. |
| `playwright` | 1.58.1 | Browser automation for SteamDB scraping | SteamDB is behind Cloudflare Bot Management. Only headless browser automation can reliably pass JS challenges and TLS fingerprint checks. Playwright is the correct choice over Puppeteer: multi-browser, actively maintained, first-class TS support. Note: `puppeteer-extra-stealth` was deprecated Feb 2025 -- do not use it. |
| `typescript` | 5.x (latest) | Language | Already chosen. Zod v4 requires TS 5.5+. Pin to 5.5 or later. |
| `node` | 20 LTS or 22 LTS | Runtime | Native `fetch` is stable from Node 18+. Node 20 LTS gives the longest support window. Required for Playwright browser binaries. |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `cheerio` | 1.x (latest stable) | HTML parsing after Playwright fetches SteamDB pages | Use to extract structured data from the raw HTML that Playwright returns. Cheerio is the jQuery-style parser -- fast, lightweight, does not spin up a second browser. Do NOT use it to fetch pages directly; Playwright handles that. |
| `@j4ckofalltrades/steam-webapi-ts` | 1.2.2 | Typed wrapper around the Steam Web API | Use if you want typed method calls (e.g. `usersApi.getPlayerSummaries()`). Pure TypeScript, isomorphic, MIT licensed, actively maintained (Aug 2025 release). LOW adoption (6 GitHub stars) -- see Alternatives section for the tradeoff. |
| `dotenv` | 16.x | Environment variable management | Steam API requires an API key obtained from `http://steamcommunity.com/dev/apikey`. Store it in `.env`, never in code. |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| `tsx` or `ts-node` | Run TypeScript directly without a compile step during development | `tsx` is faster (esbuild-based). Use for `npm run dev`. For production, compile with `tsc` first. |
| `vitest` | Unit test runner | Zero-config, fast, excellent TS support. Use to test Steam API response parsing and Gamalytic data normalization logic. |
| `tsc` | TypeScript compiler | Configure with `"target": "ES2022"`, `"module": "Node16"`, `"moduleResolution": "Node16"`, `"strict": true`. These are the settings the MCP SDK documentation specifies. |

---

## Installation

```bash
# Core -- MCP server + validation
npm install @modelcontextprotocol/sdk zod

# Browser automation + HTML parsing (SteamDB scraping)
npm install playwright cheerio

# Install Playwright browsers (one-time, downloads Chromium/Firefox/WebKit)
npx playwright install

# Environment
npm install dotenv

# Optional: typed Steam API wrapper (see Alternatives section before choosing)
npm install @j4ckofalltrades/steam-webapi-ts

# Dev dependencies
npm install -D typescript @types/node tsx vitest
```

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| Native `fetch` (Node 18+) for Steam API and Gamalytic calls | `axios` (v1.13.2) | Use axios only if you need request/response interceptors or automatic retry logic. For a personal research tool hitting two well-behaved REST APIs, native fetch is sufficient and removes a dependency. Axios adds ~5KB and complexity you do not need here. |
| `@j4ckofalltrades/steam-webapi-ts` | Write your own typed fetch calls against `https://api.steampowered.com` | If the wrapper library stops being maintained or does not cover an endpoint you need, the Steam Web API is a simple REST API. Writing typed fetch calls is 10 lines of code per endpoint. The wrapper is convenient but not irreplaceable. |
| `@j4ckofalltrades/steam-webapi-ts` | `steamapi` (node-steamapi) | `steamapi` is more popular on npm but is primarily JavaScript, not TypeScript-native. If you value community support over type safety, use `steamapi`. For a TypeScript-first project, the typed wrapper is the better fit. |
| Playwright (for SteamDB) | Puppeteer | Puppeteer is Chrome-only. Playwright supports Chromium, Firefox, and WebKit. More importantly, `puppeteer-extra-stealth` (the standard anti-detection plugin) was deprecated in February 2025 and is no longer maintained. Do not start a new project on it. |
| Playwright (for SteamDB) | Crawlee (Apify) | Crawlee is a batteries-included scraping framework that wraps Playwright. It adds queue management, proxy rotation, retry logic. Overkill for a personal tool hitting one site. Use raw Playwright. If you later need multi-site crawling at scale, Crawlee becomes worth it. |
| Gamalytic REST API (direct) | Scraping Gamalytic website | Gamalytic exposes a FREE, unauthenticated REST API at `api.gamalytic.com`. There is no reason to scrape their website. Use the API directly. |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `puppeteer-extra-stealth` | Deprecated February 2025. No longer maintained. Will not receive updates for new Cloudflare detection methods. | Playwright with manual stealth configuration (see Pitfalls). |
| MCP SDK v2 (pre-alpha) | Pre-alpha as of 2026-02-05. The GitHub repo explicitly states "v1.x remains the recommended version for production use." v2 stable is expected Q1 2026. | `@modelcontextprotocol/sdk` v1.26.0 (v1.x branch). |
| `@types/axios` | Unnecessary. Axios ships its own TypeScript definitions since v1.x. Installing `@types/axios` separately causes type conflicts. | Just `npm install axios` if you use axios at all. |
| Zod v3 | The MCP SDK requires Zod v4 as a peer dependency. Using v3 will cause runtime errors. | `zod` v4.3.5. |
| Scraping Steam Web API responses with a browser | The Steam Web API (`api.steampowered.com`) returns JSON. No browser needed. Using Playwright to hit it adds latency and complexity for no reason. | Native `fetch` or a typed wrapper library. Reserve Playwright exclusively for SteamDB. |

---

## Stack Patterns by Variant

**If Gamalytic data is sufficient for commercial metrics (revenue estimates, review counts):**
- Use native `fetch` against `api.gamalytic.com` endpoints directly
- No scraping layer needed for this data source
- Gamalytic API is free, unauthenticated, and returns JSON

**If you need SteamDB-specific data (historical pricing, concurrent player charts, sale history):**
- Use Playwright to render the page
- Extract HTML with Playwright's `page.content()`
- Parse with Cheerio
- Rate-limit aggressively: one request per 3-5 seconds minimum, or Cloudflare will block you

**If you need Steam community/profile data (friends, achievements, inventory):**
- Use the Steam Web API with your API key
- The typed wrapper (`steam-webapi-ts`) covers the core ISteamUser endpoints
- For endpoints the wrapper does not cover, fall back to native fetch against `https://api.steampowered.com`

**If you want to run the MCP server locally (Claude Desktop / Cursor):**
- Use `stdio` transport (StdioServerTransport)
- This is the standard transport for local MCP servers consumed by desktop clients
- No HTTP server needed

**If you later want to expose the MCP server over the network:**
- Switch to Streamable HTTP transport
- Install `@modelcontextprotocol/node` middleware package
- This is NOT needed for the initial personal-tool use case

---

## Version Compatibility

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| `@modelcontextprotocol/sdk` 1.26.0 | `zod` 4.x | Zod v4 is a required peer dependency. Zod v3 will NOT work. |
| `@modelcontextprotocol/sdk` 1.26.0 | `typescript` 5.5+ | Zod v4 requires TS 5.5+. This is the binding constraint on your TS version. |
| `playwright` 1.58.x | `node` 18+ | Playwright browser binaries require Node 18 or later. |
| `cheerio` 1.x | `typescript` 5.x | Ships its own types. No `@types/cheerio` needed. |
| `axios` 1.13.x | `typescript` 4.7+ with `moduleResolution: "node16"` | Only relevant if you choose axios over native fetch. |

---

## Sources

- [GitHub: modelcontextprotocol/typescript-sdk](https://github.com/modelcontextprotocol/typescript-sdk) -- MCP SDK v1.x vs v2 status, transport options, zod peer dep requirement. HIGH confidence.
- [npm: @modelcontextprotocol/sdk](https://www.npmjs.com/package/@modelcontextprotocol/sdk) -- Version 1.26.0 confirmed, 23,420 dependents. HIGH confidence.
- [GitHub: j4ckofalltrades/steam-webapi-ts](https://github.com/j4ckofalltrades/steam-webapi-ts) -- v1.2.2, pure TS, MIT, active. MEDIUM confidence (low adoption count).
- [Gamalytic API reference](https://api.gamalytic.com/reference/) -- Confirmed FREE, unauthenticated, endpoints at `/game/<appid>`, `/steam-games/list`, `/steam-games/stats`. Verified via [community usage](https://github.com/adamcyounis/Steam-Game-Research). MEDIUM confidence (small community, API stability not guaranteed).
- [npm: playwright](https://www.npmjs.com/package/playwright) -- v1.58.1 confirmed. HIGH confidence.
- [Zod release notes](https://zod.dev/v4) -- v4.3.5, released July 2025, TS 5.5+ required. HIGH confidence.
- [npm: axios](https://www.npmjs.com/package/axios) -- v1.13.2 confirmed. HIGH confidence (referenced for alternatives comparison only).
- [ScrapingBee: Cloudflare bypass 2025](https://www.scrapingbee.com/blog/how-to-bypass-cloudflare-antibot-protection-at-scale/) -- SteamDB is Cloudflare-protected, headless browser is required. MEDIUM confidence (third-party source, but consistent with known Cloudflare behavior).
- [InfoQ: Zod v4](https://www.infoq.com/news/2025/08/zod-v4-available/) -- v4 release confirmed with performance benchmarks. HIGH confidence.

---
*Stack research for: Games market research MCP server (Steam API + SteamDB/Gamalytic)*
*Researched: 2026-02-05*
