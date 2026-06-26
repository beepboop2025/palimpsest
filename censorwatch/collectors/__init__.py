"""Per-source post collectors (each a BasePostCollector + PostSource).

Build order: eastmoney_guba (Step 2) → xueqiu (Step 8) → weibo_search (Step 8,
enabled only once a residential proxy is available).
"""
