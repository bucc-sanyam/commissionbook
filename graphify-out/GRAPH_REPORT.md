# Graph Report - Commission  (2026-11-24)

## Corpus Check
- 35 files · ~97,058 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1369 nodes · 3404 edges · 71 communities (34 shown, 37 thin omitted)
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 60 edges (avg confidence: 0.55)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ea8291eb`
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
- on
- .St
- .ob
- et
- oi
- c
- .Oc
- N
- .Jp
- .Hs
- .p
- yn
- S
- z
- .Ii
- manifest.json
- vercel.json
- .od
- ci
- gn
- .Fc
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
- .zn
- di

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
- `main()` --calls--> `update_all_stock_prices()`  [EXTRACTED]
  update_prices.py → backend/prices.py
- `create_app()` --calls--> `parse_text()`  [EXTRACTED]
  backend/web.py → trade_parser.py
- `main()` --calls--> `create_app()`  [EXTRACTED]
  update_prices.py → backend/web.py
- `main()` --calls--> `get_db()`  [EXTRACTED]
  update_prices.py → backend/database.py
- `create_app()` --indirect_call--> `secure_response()`  [INFERRED]
  backend/web.py → backend/security.py

## Import Cycles
- None detected.

## Communities (71 total, 37 thin omitted)

### Community 0 - "sn"
Cohesion: 0.06
Nodes (7): Fn, gi(), he(), mn, sn(), Vt(), wn()

### Community 2 - "pn"
Cohesion: 0.06
Nodes (7): d(), gn, lt(), pn, un(), we, ys()

### Community 3 - "se"
Cohesion: 0.06
Nodes (23): ge(), O(), se(), xe, addRow(), authorization(), checkRow(), commitSubmission() (+15 more)

### Community 4 - "zi"
Cohesion: 0.07
Nodes (4): q(), ri, zi, Zs()

### Community 5 - "web.py"
Cohesion: 0.08
Nodes (41): configure_app(), credential_version(), csrf_token(), local_setup_allowed(), _request_is_loopback(), require_csrf(), safe_redirect_target(), secure_response() (+33 more)

### Community 6 - "lightweight-charts.js"
Cohesion: 0.07
Nodes (11): ae(), di(), E, ee(), ei, jt, le(), oe() (+3 more)

### Community 7 - "trade_parser.py"
Cohesion: 0.11
Nodes (43): _anchors(), _bad_status(), _card_blocks(), _date_info(), detect_platform(), _execution_text(), extract_orders(), _extract_records() (+35 more)

### Community 9 - "ps"
Cohesion: 0.12
Nodes (12): drawEarnings(), monthLabel(), svgElement(), bs(), gs(), hs(), ms(), ps (+4 more)

### Community 16 - "prices.py"
Cohesion: 0.10
Nodes (22): DailyPriceScheduler, fetch_stock_candles(), fetch_stock_quote(), format_ist_timestamp(), get_all_held_stocks(), get_all_ledger_stocks_summary(), get_cached_prices(), invalidate_prices_cache() (+14 more)

### Community 17 - "get_db"
Cohesion: 0.16
Nodes (13): close_db(), complete_request(), Database, get_db(), init_local(), install_database(), _params(), Result (+5 more)

### Community 18 - "u"
Cohesion: 0.11
Nodes (6): dt, G(), Hi(), ki, vs(), _()

### Community 19 - "bn"
Cohesion: 0.12
Nodes (4): bn, ji, qi(), T()

### Community 20 - "SupabaseStorage"
Cohesion: 0.22
Nodes (6): get_storage(), LocalStorage, safe_path(), StorageUnavailable, SupabaseStorage, validate_image()

### Community 21 - "app.js"
Cohesion: 0.15
Nodes (15): applyLiveQuoteUpdates(), cleanPhoneNumberValue(), initPhoneInputs(), initPrivacy(), initStockAutocomplete(), isMarketOpenIST(), loadStockSuggestions(), pollLiveQuotes() (+7 more)

### Community 22 - "ye"
Cohesion: 0.05
Nodes (7): c(), Pe(), qn(), R(), xn(), ye, Zn

### Community 24 - "apply_sale_fifo"
Cohesion: 0.12
Nodes (21): apply_sale_fifo(), cleanup_stock_aliases_table(), ensure_stock_aliases_table(), get_alias_mapping(), get_client(), get_client_calculated_holdings(), get_client_entered_stocks(), get_clients_holding_stock() (+13 more)

### Community 26 - "create_app"
Cohesion: 0.33
Nodes (16): Vercel's WSGI entrypoint; all state lives in PostgreSQL and private Storage., inspect_local(), migrate_local(), Explicit, preflighted migration from a read-only SQLite source to an empty cloud, client_name(), identifier(), number(), rule_values() (+8 more)

### Community 27 - "mi"
Cohesion: 0.12
Nodes (7): bi(), fi(), mi, pi(), ui(), vi(), wi()

### Community 28 - "j"
Cohesion: 0.14
Nodes (3): j, qt, xi

### Community 33 - "on"
Cohesion: 0.10
Nodes (5): as(), on, os(), ut, wt()

### Community 39 - ".Oc"
Cohesion: 0.19
Nodes (5): ci(), hn(), ln(), rn(), tn()

### Community 41 - ".Jp"
Cohesion: 0.27
Nodes (3): cs(), fs(), Ts()

### Community 42 - ".Hs"
Cohesion: 0.15
Nodes (3): be(), gt, It

### Community 46 - "z"
Cohesion: 0.15
Nodes (6): bt(), ft(), mt(), pt(), tt, y()

### Community 48 - "manifest.json"
Cohesion: 0.25
Nodes (7): background_color, display, icons, name, short_name, start_url, theme_color

### Community 49 - "vercel.json"
Cohesion: 0.29
Nodes (6): bom1, builds, crons, regions, routes, version

### Community 54 - "ct"
Cohesion: 0.20
Nodes (9): 1. Project Overview, 2. Technology Stack, 3. Directory Structure & Key Files, 4. Financial & Business Logic, 5. Security & Authentication, 6. Development & Deployment Guidelines, Commission Book — Project Summary, Commission Rules (+1 more)

### Community 57 - "clean_phone_number"
Cohesion: 0.40
Nodes (5): clean_phone_number(), cleanup_existing_client_phones(), get_or_create_client(), Strip any +91, 91, or 0 country codes and keep only the 10-digit mobile number., Ensure any client phones with +91 prefixes in SQLite/Supabase are normalized to

### Community 58 - "Commission Book"
Cohesion: 0.40
Nodes (4): Commission Book, Features, How screenshot reading works (no AI API calls), Run

### Community 60 - "format_ist"
Cohesion: 0.50
Nodes (4): format_ist(), parse_utc_datetime(), Parse any datetime, string, or timestamp into a timezone-aware UTC datetime., Format UTC timestamp to IST in various formats:     - chip: '09:20 AM' if today,

### Community 66 - ".Bn"
Cohesion: 0.25
Nodes (3): H, X(), z()

## Knowledge Gaps
- **24 isolated node(s):** `name`, `short_name`, `start_url`, `display`, `background_color` (+19 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **37 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `f()` connect `.ob` to `sn`, `f`, `pn`, `zi`, `lightweight-charts.js`, `ht`, `ps`, `Es`, `Ns`, `ds`, `an`, `u`, `ye`, `yi`, `ks`, `j`, `.Gn`, `.qt`, `.Oc`, `.Jp`, `.p`, `S`, `.od`, `ci`, `gn`, `.l`?**
  _High betweenness centrality (0.052) - this node is a cross-community bridge._
- **Why does `sn()` connect `sn` to `lightweight-charts.js`, `.Oc`, `ht`, `Es`, `Ns`, `ci`, `nt`, `.Gn`?**
  _High betweenness centrality (0.046) - this node is a cross-community bridge._
- **Why does `vn` connect `f` to `pn`, `.l`, `lightweight-charts.js`, `.Oc`, `.p`, `bn`, `ci`?**
  _High betweenness centrality (0.046) - this node is a cross-community bridge._
- **What connects `name`, `short_name`, `start_url` to the rest of the system?**
  _24 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `sn` be split into smaller, more focused modules?**
  _Cohesion score 0.055501460564751706 - nodes in this community are weakly interconnected._
- **Should `pn` be split into smaller, more focused modules?**
  _Cohesion score 0.05608322026232474 - nodes in this community are weakly interconnected._
- **Should `se` be split into smaller, more focused modules?**
  _Cohesion score 0.05805515239477504 - nodes in this community are weakly interconnected._