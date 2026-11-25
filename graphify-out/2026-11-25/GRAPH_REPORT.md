# Graph Report - Commission  (2026-11-25)

## Corpus Check
- 36 files · ~218,549 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1387 nodes · 3425 edges · 76 communities (42 shown, 34 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 60 edges (avg confidence: 0.55)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `52c86d1a`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- sn
- f
- pn
- se
- zi
- web.py
- lightweight-charts.js
- trade_parser.py
- ht
- ps
- Es
- Ns
- ds
- ki
- an
- te
- prices.py
- get_db
- u
- bn
- SupabaseStorage
- app.js
- ye
- yi
- apply_sale_fifo
- ks
- create_app
- mi
- j
- .Gn
- at
- .Ab
- .qt
- on
- .St
- et
- oi
- .Oc
- N
- .Jp
- .Hs
- .p
- yn
- S
- z
- manifest.json
- vercel.json
- gn
- ct
- kt
- ti
- clean_phone_number
- Commission Book
- nt
- format_ist
- graphify.md
- graphify.md
- __init__.py
- .br
- .Bn
- .l
- qt
- ei
- di
- .Bn
- di
- Fn
- jt
- Alder & Vane redesign brief

## God Nodes (most connected - your core abstractions)
1. `ki` - 81 edges
2. `an()` - 68 edges
3. `sn()` - 65 edges
4. `f()` - 64 edges
5. `vn` - 57 edges
6. `ds()` - 55 edges
7. `yi()` - 49 edges
8. `te()` - 42 edges
9. `Ns` - 40 edges
10. `Es` - 38 edges

## Surprising Connections (you probably didn't know these)
- `main()` --calls--> `get_db()`  [EXTRACTED]
  update_prices.py → backend/database.py
- `create_app()` --calls--> `parse_text()`  [EXTRACTED]
  backend/web.py → trade_parser.py
- `main()` --calls--> `create_app()`  [EXTRACTED]
  update_prices.py → backend/web.py
- `main()` --calls--> `update_all_stock_prices()`  [EXTRACTED]
  update_prices.py → backend/prices.py
- `create_app()` --indirect_call--> `secure_response()`  [INFERRED]
  backend/web.py → backend/security.py

## Import Cycles
- None detected.

## Communities (76 total, 34 thin omitted)

### Community 1 - "f"
Cohesion: 0.06
Nodes (10): en(), f(), hn(), ln(), nn(), rn(), si, ti (+2 more)

### Community 2 - "pn"
Cohesion: 0.06
Nodes (6): d(), lt(), pn, un(), we, ys()

### Community 3 - "se"
Cohesion: 0.06
Nodes (23): ge(), O(), se(), xe, addRow(), authorization(), checkRow(), commitSubmission() (+15 more)

### Community 5 - "web.py"
Cohesion: 0.08
Nodes (36): Vercel's WSGI entrypoint; all state lives in PostgreSQL and private Storage., configure_app(), credential_version(), csrf_token(), local_setup_allowed(), _request_is_loopback(), require_csrf(), safe_redirect_target() (+28 more)

### Community 6 - "lightweight-charts.js"
Cohesion: 0.08
Nodes (10): ae(), bt(), ei, ii, le(), mt(), oe(), pt() (+2 more)

### Community 7 - "trade_parser.py"
Cohesion: 0.11
Nodes (43): _anchors(), _bad_status(), _card_blocks(), _date_info(), detect_platform(), _execution_text(), extract_orders(), _extract_records() (+35 more)

### Community 9 - "ps"
Cohesion: 0.10
Nodes (13): drawEarnings(), monthLabel(), svgElement(), be(), bs(), gs(), hs(), ms() (+5 more)

### Community 16 - "prices.py"
Cohesion: 0.10
Nodes (23): DailyPriceScheduler, fetch_stock_candles(), fetch_stock_quote(), format_ist_timestamp(), get_all_held_stocks(), get_all_ledger_stocks_summary(), get_cached_prices(), is_market_hours() (+15 more)

### Community 17 - "get_db"
Cohesion: 0.15
Nodes (14): close_db(), complete_request(), Database, get_db(), init_local(), install_database(), _params(), Result (+6 more)

### Community 18 - "u"
Cohesion: 0.08
Nodes (7): ft(), gt, ni, tt, u(), y(), _()

### Community 19 - "bn"
Cohesion: 0.08
Nodes (5): bn, gn, ji, qi(), T()

### Community 20 - "SupabaseStorage"
Cohesion: 0.30
Nodes (3): safe_path(), StorageUnavailable, SupabaseStorage

### Community 21 - "app.js"
Cohesion: 0.15
Nodes (15): applyLiveQuoteUpdates(), cleanPhoneNumberValue(), initPhoneInputs(), initPrivacy(), initStockAutocomplete(), isMarketOpenIST(), loadStockSuggestions(), pollLiveQuotes() (+7 more)

### Community 24 - "apply_sale_fifo"
Cohesion: 0.09
Nodes (31): invalidate_prices_cache(), aggregate_clients_sql(), apply_sale_fifo(), dec(), effective_rule(), enrich(), funds_by_client(), get_alias_mapping() (+23 more)

### Community 25 - "ks"
Cohesion: 0.21
Nodes (3): mi, Vt(), wn()

### Community 26 - "create_app"
Cohesion: 0.29
Nodes (18): get_storage(), LocalStorage, validate_image(), inspect_local(), migrate_local(), Explicit, preflighted migration from a read-only SQLite source to an empty cloud, client_name(), identifier() (+10 more)

### Community 27 - "mi"
Cohesion: 0.16
Nodes (6): bi(), fi(), pi(), ui(), vi(), wi()

### Community 28 - "j"
Cohesion: 0.14
Nodes (3): j, qt, xi

### Community 33 - "on"
Cohesion: 0.08
Nodes (4): q(), ri, zi, Zs()

### Community 36 - "et"
Cohesion: 0.10
Nodes (3): et, nt, yt

### Community 42 - ".Hs"
Cohesion: 0.17
Nodes (3): ai(), c(), Ce()

### Community 43 - ".p"
Cohesion: 0.18
Nodes (3): Pe(), xn(), Zn

### Community 45 - "S"
Cohesion: 0.10
Nodes (6): as(), ls(), on, os(), ut, wt()

### Community 48 - "manifest.json"
Cohesion: 0.25
Nodes (7): background_color, display, icons, name, short_name, start_url, theme_color

### Community 49 - "vercel.json"
Cohesion: 0.29
Nodes (6): bom1, builds, crons, regions, routes, version

### Community 52 - "gn"
Cohesion: 0.10
Nodes (7): dt, G(), H, ki, L, vs(), yn

### Community 54 - "ct"
Cohesion: 0.17
Nodes (11): 1. Project Overview, 2. Technology Stack, 3. Directory Structure & Key Files, 4. Financial & Business Logic, 5. Security & Authentication, 6. Development & Deployment Guidelines, Alder & Vane — Project Summary, Commission Rules (+3 more)

### Community 57 - "clean_phone_number"
Cohesion: 0.40
Nodes (5): clean_phone_number(), cleanup_existing_client_phones(), get_or_create_client(), Strip any +91, 91, or 0 country codes and keep only the 10-digit mobile number., Ensure any client phones with +91 prefixes in SQLite/Supabase are normalized to

### Community 58 - "Commission Book"
Cohesion: 0.40
Nodes (4): Alder & Vane, Features, How screenshot reading works (no AI API calls), Run

### Community 60 - "format_ist"
Cohesion: 0.50
Nodes (4): format_ist(), parse_utc_datetime(), Parse any datetime, string, or timestamp into a timezone-aware UTC datetime., Format UTC timestamp to IST in various formats:     - chip: '09:20 AM' if today,

### Community 65 - ".br"
Cohesion: 0.13
Nodes (3): Cn, I, xs()

### Community 68 - "qt"
Cohesion: 0.12
Nodes (4): fs(), ks, ss(), z()

### Community 69 - "ei"
Cohesion: 0.28
Nodes (4): E, ee(), re(), ue()

### Community 71 - ".Bn"
Cohesion: 0.25
Nodes (3): kn, qn(), R()

### Community 77 - "Alder & Vane redesign brief"
Cohesion: 0.20
Nodes (9): Aldervain redesign roadmap, Change the production link, Design decisions, Design references, Implementation reviewed, Original design specification, Prompt for the release model, What existing PWA users should expect (+1 more)

## Knowledge Gaps
- **34 isolated node(s):** `name`, `short_name`, `start_url`, `display`, `background_color` (+29 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **34 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `sn()` connect `sn` to `.qt`, `f`, `pn`, `zi`, `oi`, `lightweight-charts.js`, `et`, `.Jp`, `.od`, `ci`, `.Fc`, `.Gn`?**
  _High betweenness centrality (0.057) - this node is a cross-community bridge._
- **Why does `ki` connect `gn` to `f`, `pn`, `se`, `zi`, `lightweight-charts.js`, `ht`, `ps`, `Es`, `Ns`, `ds`, `ki`, `an`, `te`, `u`, `bn`, `yi`, `mi`, `j`, `.Gn`, `at`, `.Ab`, `.qt`, `on`, `.ob`, `N`, `.Jp`, `.p`, `S`, `.Ii`, `kt`, `ti`, `qt`?**
  _High betweenness centrality (0.052) - this node is a cross-community bridge._
- **Why does `te()` connect `te` to `.Bn`, `pn`, `lightweight-charts.js`, `ps`, `.Hs`, `.p`, `ki`, `u`, `gn`, `.Ab`?**
  _High betweenness centrality (0.043) - this node is a cross-community bridge._
- **What connects `name`, `short_name`, `start_url` to the rest of the system?**
  _34 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `f` be split into smaller, more focused modules?**
  _Cohesion score 0.060674157303370786 - nodes in this community are weakly interconnected._
- **Should `pn` be split into smaller, more focused modules?**
  _Cohesion score 0.06284153005464481 - nodes in this community are weakly interconnected._
- **Should `se` be split into smaller, more focused modules?**
  _Cohesion score 0.05660377358490566 - nodes in this community are weakly interconnected._