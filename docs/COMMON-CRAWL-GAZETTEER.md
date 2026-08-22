# Common Crawl gazetteer differential

Historical public web text is a bulk baseline for what used to be sayable.
This method matches the human gazetteer against Common Crawl URL Index rows.
It does not fetch page bodies and does not contact origin hosts.

## What it is

`processors/common_crawl_gazetteer.py` takes a list of URL-index rows
(`url`, `crawl`, `timestamp`) and reports gazetteer terms that appear in the
decoded URL path or query. Optionally it annotates whether the term is also on
the current DDTI list.

## What it is not

- A deletion detector. A hit in crawl C and silence in crawl D is a coverage
  or URL-shape fact, not a takedown.
- A widening of the live Common Crawl warehouse. That warehouse stays
  allowlisted to reviewed institutional hosts for financial evidence. See
  [COMMON-CRAWL-LAKE.md](COMMON-CRAWL-LAKE.md).
- A sensitivity classifier. Gazetteer membership is human-authored.

## Public today

The code and offline tests ship. A live gazetteer-scale Common Crawl export is
not admitted until an operator provides a public URL-index extract that stays
inside Palimpsest's public-source and rights rules. Until then the processor
abstains rather than crawling.

## Reproduce

```bash
PYTHONPATH=. python3 -m pytest tests/test_common_crawl_gazetteer.py -q
```
