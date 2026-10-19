"""Vocabulary of filing signals: weight, meaning, how long it stays relevant.

kind: pos = story-building, red = red flag, info = context only.
kill = remove the company from the hunt (cockroach theory: problems cluster).
Edit weights here or override them in config.yaml (hunt.tag_weights).
"""

TAG_INFO = {
    # ---- story-building ----
    "order_win":             dict(w=1.0, kind="pos", label="order win", window=180),
    "capex_announced":       dict(w=1.5, kind="pos", label="capex / capacity expansion announced", window=365),
    "commissioning":         dict(w=2.5, kind="pos", label="commissioning / commercial production", window=270),
    "new_product":           dict(w=1.0, kind="pos", label="new product / regulatory approval", window=180),
    "new_geography":         dict(w=1.0, kind="pos", label="new geography / export", window=180),
    "first_concall":         dict(w=2.5, kind="pos", label="FIRST-EVER earnings call", window=150),
    "warrants_preferential": dict(w=1.0, kind="pos", label="warrants / preferential issue", window=270),
    "change_in_control":     dict(w=1.5, kind="pos", label="change in control / open offer", window=270),
    "rating_upgrade":        dict(w=1.0, kind="pos", label="credit rating upgrade", window=180),
    "buyback":               dict(w=1.0, kind="pos", label="buyback", window=180),
    "voluntary_update":      dict(w=0.5, kind="pos", label="voluntary business update (candor)", window=120),
    "new_leadership":        dict(w=1.0, kind="pos", label="new CEO / MD appointed", window=180),
    "strong_result":         dict(w=2.0, kind="pos", label="strong quarterly result", window=120),
    "pledge_released":       dict(w=0.5, kind="pos", label="promoter pledge released", window=180),
    # ---- context ----
    "concall":               dict(w=0.2, kind="info", label="earnings call", window=90),
    "investor_meet":         dict(w=0.2, kind="info", label="investor / analyst meet", window=60),
    
    "acquisition":           dict(w=0.5, kind="info", label="acquisition", window=180),
    "fund_raise":            dict(w=0.2, kind="info", label="QIP / rights / fund raise", window=180),
    "results":               dict(w=0.0, kind="info", label="results filed", window=30),
    # ---- red flags ----
    "auditor_exit":          dict(w=-4.0, kind="red", label="statutory auditor resigned", window=365, kill=True),
    "kmp_exit":              dict(w=-2.0, kind="red", label="CEO / MD / CFO exit", window=270),
    "cs_exit":               dict(w=-0.5, kind="red", label="company secretary exit", window=180),
    "independent_director_exit": dict(w=-1.5, kind="red", label="independent director resigned", window=270),
    "pledge_created":        dict(w=-2.0, kind="red", label="promoter pledge created / invoked", window=270),
    "default":               dict(w=-5.0, kind="red", label="default / payment delay", window=365, kill=True),
    "rating_downgrade":      dict(w=-2.0, kind="red", label="credit rating downgrade", window=270),
    "regulatory_action":     dict(w=-3.0, kind="red", label="regulator action / search / SEBI order", window=365),
    "tax_or_penalty_order":  dict(w=-0.5, kind="red", label="tax demand / penalty order", window=180),
    "fraud_disclosure":      dict(w=-3.0, kind="red", label="fraud disclosed", window=365),
    "forensic_audit":        dict(w=-5.0, kind="red", label="forensic audit", window=730, kill=True),
    "insolvency":            dict(w=-3.0, kind="red", label="insolvency / NCLT matter", window=365),
    "weak_result":           dict(w=-1.0, kind="red", label="weak quarterly result", window=120),
}

KILL_TAGS = {t for t, i in TAG_INFO.items() if i.get("kill")}
RED_TAGS = {t for t, i in TAG_INFO.items() if i["kind"] == "red"}
DOC_TAGS = ["warrants_preferential", "change_in_control", "commissioning", "capex_announced", "order_win"]


def info(tag: str, overrides: dict | None = None) -> dict:
    base = dict(TAG_INFO.get(tag, dict(w=0.0, kind="info", label=tag, window=90)))
    if overrides and tag in overrides:
        base["w"] = float(overrides[tag])
    return base
