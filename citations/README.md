# Citation packs

`palimpsest.bib` is generated from `config/public_data_catalog.json`.

```bash
python3 -m scripts.build_citation_pack
python3 -m scripts.build_citation_pack --dataset ddti
python3 -m scripts.build_citation_pack --dataset ddti --day 2026-08-22
```

A day citation abstains if that calendar day is not in the history file. It
does not pick the nearest day.

Also see [cite.html](https://palimpsest.info/cite.html) and
[challenge.html](https://palimpsest.info/challenge.html).
