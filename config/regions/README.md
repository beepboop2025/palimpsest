# Region packs

Palimpsest's method (deletion as data, DDTI selectivity and novelty) is
country-agnostic. A new authoritarian information space is a config pack, not
a rewrite.

## Minimum pack

1. A gazetteer JSON at `config/regions/<code>/gazetteer.json`.
2. A registry row in `config/regions.json`.

Iran (`ir`) is the working example. China (`cn`) remains the default.

### Gazetteer

```json
{
  "_meta": {
    "purpose": "Human-authored sensitive terms for this information space.",
    "note": "Never delegated to a state-aligned model."
  },
  "categories": {
    "example_category": [
      {
        "fa": "native term",
        "en": "analyst gloss",
        "type": "direct",
        "evidence": "documented event and date"
      }
    ]
  }
}
```

Use the language key named by `term_key` in the registry (`zh`, `fa`, or
`term`). `mutation_of` is optional phylogeny. `evidence` is required for any
term you want a reviewer to trust.

### Registry row

```json
"xx": {
  "name": "Country name",
  "lang": "xx",
  "term_key": "xx",
  "gazetteer": "config/regions/xx/gazetteer.json",
  "sources": ["public publisher 1", "public publisher 2"],
  "note": "Starter lexicon. Human-authored. Public sources only."
}
```

## Collectors

A region pack does not have to ship a new collector on day one. When it does:

- Public sources only (RSS, JSON, open APIs, keyless archives).
- Fail soft: a blocked source is an abstention, never a silent zero.
- Consult `core.governance.KillSwitch` and a `RateCeiling`.
- Emit the uniform envelope in `core/collector_artifact.py` (source receipt,
  freshness, coverage, abstention, payload hash).
- No in-country actor, no non-public data, no person-level collection.

Open an issue before writing a collector that leaves this list.

## Tests

Add a stdlib-only test that loads the gazetteer through
`processors.regions.load_region_terms("<code>")` and checks that every native
term is non-empty. Do not commit live collected content in the pack PR.
