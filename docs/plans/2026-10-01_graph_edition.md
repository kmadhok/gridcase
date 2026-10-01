# MarketCase — graph edition (G1–G5)

Status: proposed 2026-10-01, awaiting the user's go-ahead on the four decisions at the end.

## Why

Three postings ask for a skill set MarketCase does not show yet:
- **Samba TV, Senior Ontologist:** RDF/OWL ontologies, SHACL, SPARQL, event-to-ontology derivation, entity resolution, GraphRAG.
- **Capital One, Senior Knowledge Graph Developer:** graph schemas, SPARQL/Cypher, ontologies, LLM integration, governance.
- **A metadata/ontology role:** W3C standards (RDF, RDFS, OWL, SKOS), ontology tools, SPARQL.

The graph edition adds a governed knowledge graph beside the semantic layer and the dbt warehouse, with real Polymarket data, real standards and tests. The goal is a demo you can open, query and explain. It is not a claim of years of experience.

## What the live data says (VPS, 2026-10-01)

| Thing | Count | Consequence |
|---|---|---|
| Trades | 2.4M in ~2.3 days (≈1.1M/day) | trades stay in the warehouse; ~20 triples each would add ~20M triples a day for nothing a graph does better |
| Markets / events (by slug) | 51k / 14k | the core of the graph |
| Markets with no event title | 24.8k (half) | the event layer must come from the trades' event slug, not only the metadata API |
| Recurring "Up or Down" markets | 6.2k, thousands more each day | model them as rounds of a series, not as unrelated events |
| Outcomes | 75k (markets have 1 or 2 traded outcomes) | an explicit node: odds belong to outcomes, not markets |
| Wallets | 58k (1.8k with $10k+ traded) | nodes with derived facts; no identity linking (pseudonymous; see decisions) |
| Polymarket tags | thousands, pipe-separated ("Sports\|Soccer\|Concacaf Nations League") | already half a taxonomy: map them into SKOS |

## First principles

1. **The graph holds entities, relations and durable derived facts; raw events stay in the warehouse.** A graph earns its place where questions are about relationships: which markets concern the Steelers, what else this event contains, which wallets moved several markets about the same thing. Per-trade sums and counts are columnar work, which DuckDB does well. This is the "materialise versus virtualise" trade-off Samba names, decided by numbers: about 200k entities and roughly 2–5M triples a month, against about 20M a day if trades were included.
2. **Standards over invention.** OWL for the model, SKOS for taxonomies, SHACL for validation, SPARQL for queries, PROV for where a fact came from. Alignment with schema.org (`SportsTeam`, `Person`, `Event`) and Wikidata IDs makes entities linkable.
3. **The model never writes SPARQL**, as it never writes SQL (hard rule 1 extended). The question box gets governed graph checks (fixed query templates with typed inputs). Free-form SPARQL is for people.
4. **The model proposes, deterministic code decides.** Entity links come from a reviewed alias list and deterministic matching. An LLM may suggest new links into a review queue, but never writes them into the graph.
5. **Expressivity versus scale.** Full OWL reasoning over millions of facts is slow and needs Java. So the ontology is authored in OWL and checked with a reasoner (it is small), while the instance data gets only the inferences we use (subclasses, inverse properties, defined classes like whale), materialised with SPARQL rules after each load. SHACL closes the open world where we need guarantees.

## Design

**Store:** Oxigraph (`pyoxigraph`):
- embedded (no new server process)
- SPARQL 1.1 query and update
- persistent (RocksDB)
- fast bulk load

Rejected:
- **rdflib alone:** pure Python, too slow at millions of triples. It is still used for SHACL and ontology checks on small graphs.
- **GraphDB, Stardog or Neptune:** a heavy service or a cloud bill. SPARQL and OWL skills transfer to them as is.

**Who writes it:** the server, which is already the single writer, rebuilds the graph hourly at :40 from the dbt warehouse (read-only, after dbt's :20 build). It writes a fresh store, swaps it in atomically, and queries never see half a load.

**The ontology** (`ontology/marketcase.ttl`, OWL in Turtle, opens in Protégé):

```
:Event ──hasMarket──▶ :Market ──hasOutcome──▶ :Outcome
   │                    │  ├─ :EventMarket      (disjoint)
   │                    │  └─ :RecurringMarket ──roundOf──▶ :RecurringSeries
   │                    └──category──▶ skos:Concept (Category scheme)
   │                    └──taggedWith─▶ skos:Concept (Tag scheme, broader → Category)
   └──about──▶ :Entity (schema:SportsTeam | schema:Person | :CryptoAsset | schema:Country | schema:Organization)
                  └──skos:exactMatch──▶ Wikidata item
:Wallet ──traded──▶ :Market      :Whale ≡ :Wallet with :tradedUsd ≥ 10000 in a day (defined class)
:Metric (from ontology/kpis.yaml) ──dct:description, :formula, :definition, prov:wasDerivedFrom
```

**Event-to-ontology derivation** (daily, from the dbt marts, each fact with `prov:wasGeneratedBy` its dbt model and day):
- `:DailyMarketActivity`: volume, trades, traders per market per day (`qb:Observation`, W3C Data Cube)
- `:OddsMove`: an outcome's opening and closing price that day, flagged when it crossed 50¢
- `:WhaleDay`: a wallet with $10k+ traded that day, with the markets it traded

**Validation** (`ontology/shapes.ttl`, SHACL, checked after every load on the new and changed entities, and in full nightly):
- every market belongs to exactly one event, one category and one kind
- a binary market has at most two outcomes
- prices are between 0 and 1
- every entity link has a Wikidata ID and a label
- every derived fact has its provenance

**Entity resolution**, in three levels, each measured:
1. **Market → event** (given by Polymarket's event slug) and **round → series** (title normalisation: "Bitcoin Up or Down - October 1, 6AM ET" → series "Bitcoin Up or Down", hourly). Deterministic; tested exactly.
2. **Event → real-world entity** (team, person, coin, country):
   - candidates come from tags and title mentions, matched against a reviewed alias list (`ontology/entities.yaml`: Wikidata ID, labels, aliases, type)
   - the list is seeded for the entities behind the top ~90% of volume
   - an LLM pass proposes new aliases and links for high-volume unmatched events into a review queue; nothing is applied without review
3. **Same real-world happening across events** (for example "Chiefs vs Bills" and "Will the Chiefs win?" on one date): events sharing an entity and a date window. It is reported as a candidate, not merged.

Measured against a hand-labelled sample of about 200 real markets, committed as an evaluation set: precision ≥ 0.9, recall ≥ 0.7 for level 2.

**Using the graph:**
- **SPARQL endpoint** `/api/sparql` (read-only):
  - only SELECT, ASK and CONSTRUCT
  - each query runs in a worker process killed after 5 s
  - at most 1,000 rows
  - limited per visitor
- **Graph page** `#/graph`:
  - an entity view (the Steelers → events → markets → outcomes and their odds moves this week)
  - curated example queries with live results
  - a SPARQL editor
  - a link to the ontology and the shapes
- **Question box (GraphRAG-lite):** three governed graph checks for the model, each a fixed SPARQL template whose inputs are validated:
  - "what is this market about": its entities and series
  - "what else concerns this entity": its events and markets
  - "the neighbourhood of an event": its markets, outcomes and today's odds moves

  Answers cite them like any receipt and pass the verifier.

## Phases (each: exit test red first, then build, then commit and deploy)

| Phase | Builds | Exit test (`tests/exit/test_gN.py`) |
|---|---|---|
| **G1 Ontology** | `marketcase.ttl` (OWL), the Category and Tag SKOS schemes generated from `categories.yaml` and live tags, KPI metadata graph from `kpis.yaml` | parses; reasoner finds it consistent; every class and property has a label and comment; disjointness catches a market typed both ways; every category and tag is a concept |
| **G2 Load and validate** | warehouse → graph loader, SPARQL inference rules, SHACL shapes, atomic swap, hourly rebuild | on the dbt fixture: graph counts equal warehouse counts; inferences present; SHACL passes on clean data and names each planted violation; rebuild is identical |
| **G3 Query** | `/api/sparql`, curated queries, graph page | SELECT works; UPDATE refused; a runaway query is killed at 5 s; row cap; public limits; plain-language test on the page |
| **G4 Entity resolution** | series parser, `entities.yaml` seed, matcher, review queue, evaluation set | series exact on synthetic titles; level 2 precision ≥ 0.9 and recall ≥ 0.7 on the labelled set; nothing from the queue enters the graph unreviewed |
| **G5 GraphRAG-lite** | three governed graph checks in the question box, the entity view on case pages | an answer about an event cites a graph receipt and passes the verifier; the model cannot send free SPARQL |

**Effort:** G1 ½ day, G2 1 day, G3 ½ day, G4 1–1½ days, G5 1 day.

**Dependencies:** `pyoxigraph`, `rdflib`, `pyshacl`, `owlrl` (all pip, no Java).

## Risks

- **Entity resolution quality** is the part that can embarrass the demo. That is why it is measured on a labelled set and links are reviewed.
- **Public SPARQL** is an easy way to burn CPU. Hence the killable worker, the row cap and the per-visitor limit; the alternative is Tailscale-only (decision 2).
- **Graph rebuild time** at a month of data is estimated at a few million triples, well inside Oxigraph's bulk-load speed. It is measured at G2, with a delta load if it exceeds 5 minutes.

## Decisions for the user

1. **Store:** Oxigraph with RDF and SPARQL (recommended: the Samba and metadata roles require it), or additionally a Neo4j/Cypher copy for Capital One's property-graph line (+1 day).
2. **Public SPARQL:** editable by visitors with the limits above (recommended), or curated queries only in public and free-form over Tailscale.
3. **Wikidata linking:** yes, via the reviewed alias list (recommended), or internal entity IDs only.
4. **Investigations:** add a driver "one real-world entity" (several markets about the same team or person moved together) after G4, or leave investigations unchanged for now (recommended: decide after seeing G4's precision).
