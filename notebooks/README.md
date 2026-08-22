# Public notebooks

These scripts start from the sealed `readings/*.json` files and recompute a
claimed identity. They are the social-proof path: a researcher who does not
trust the HTML can still check the arithmetic.

No install. From the repository root:

```bash
python3 notebooks/recompute_ddti.py --check
python3 notebooks/recompute_forecast_wis.py --check
```

`recompute_ddti.py` does not rebuild the CDT numerator. It checks that each
published threat score still equals `attention * (1 + 1.5 * novelty)`, the
formula frozen in `processors/ddti_index.py`.

`recompute_forecast_wis.py` reruns the prequential Weighted Interval Score
loop over the committed history JSONLs and compares the headline scores to
`readings/forecast-ledger-latest.json`.

A Jupyter kernel is optional. Each file is a plain Python script so the same
check runs in `scripts/reproduce_all.py --optional`.
