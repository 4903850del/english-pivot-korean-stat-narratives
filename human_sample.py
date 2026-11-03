# -*- coding: utf-8 -*-
"""human_sample.py — 사람 채점 표본 추출 및 채점 집계 (SPEC §8).

추출:  PYTHONUTF8=1 py -3.11 human_sample.py [--claims results/claims.jsonl] [--questions data/questions.csv] [--outdir results] [--n 50] [--seed 42]
  → results/human_sheet.csv (블라인드: claim_id, question_ko, context_markdown, gold_required_display, sentence(스팬 **굵게**),
                              human_label, missed_claims, note)
  → results/human_key.csv   (claim_id, qid, cond, model, rep, auto_verdict, auto_code, type, value, matched_gold_name, stratum)
채점 집계:  PYTHONUTF8=1 py -3.11 human_sample.py --score [--outdir results]
  → results/human_eval.json (일치율, Cohen κ(자체 계산), FP/FN, not_a_claim, missed 합계)

층화: 4셀(cond×model) × 자동판정{MATCH, MISMATCH} = 8층. 층당 n//8 (=6), 나머지(=2)는 MISMATCH 층에 우선 배정(불일치 과대표집).
      부족한 층은 전부 넣고, 부족분은 같은 셀의 MATCH 층 → 다른 셀의 MATCH 층 순으로 보충. seed 고정.
사람 라벨: supported | unsupported | unit_error | not_a_claim.
  일치 정의: 자동 MATCH ↔ supported, 자동 MISMATCH ↔ {unsupported, unit_error}. not_a_claim 은 κ 계산에서 제외하고 건수만 보고.
  FP = 자동 MISMATCH ∧ 사람 supported, FN = 자동 MATCH ∧ 사람 ∈ {unsupported, unit_error}.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
from collections import defaultdict

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS = ("supported", "unsupported", "unit_error", "not_a_claim")


def _loads(x):
    if isinstance(x, (list, dict)):
        return x
    s = "" if x is None else str(x).strip()
    return json.loads(s) if s else []


def bold_span(sentence: str, span: str) -> str:
    if not span or span not in sentence:
        return sentence
    return sentence.replace(span, f"**{span}**", 1)


def gold_display(q) -> str:
    parts = []
    for g in _loads(q.get("gold_required")):
        parts.append(f"{g.get('name')}: {g.get('display')} ({g.get('type')})")
    return "; ".join(parts)


def sample(claims_path, questions_path, outdir, n=50, seed=42):
    claims = [json.loads(l) for l in open(claims_path, encoding="utf-8") if l.strip()]
    qdf = pd.read_csv(questions_path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    qmap = {r["qid"]: dict(r) for _, r in qdf.iterrows()}
    rnd = random.Random(seed)

    strata = defaultdict(list)
    for c in claims:
        strata[(c["cond"], c["model"], c["verdict"])].append(c)
    cells = sorted({(c["cond"], c["model"]) for c in claims})
    keys = [(cd, m, v) for (cd, m) in cells for v in ("MATCH", "MISMATCH")]
    for k in keys:
        rnd.shuffle(strata[k])
    base, extra = divmod(n, len(keys)) if keys else (0, 0)
    quota = {k: base for k in keys}
    mism = [k for k in keys if k[2] == "MISMATCH"]
    order = mism + [k for k in keys if k[2] == "MATCH"]
    for i in range(extra):
        quota[order[i % len(order)]] += 1
    chosen, used = [], set()
    deficit = 0
    for k in keys:
        pool = strata[k]
        take = pool[: quota[k]]
        deficit += quota[k] - len(take)
        for c in take:
            chosen.append((c, "|".join(k)))
            used.add(c["claim_id"])
    # 보충: MATCH 층(같은 셀 우선은 층 순회로 자연히 보장되지 않으므로 라운드로빈)
    if deficit > 0:
        match_pools = [[c for c in strata[(cd, m, "MATCH")] if c["claim_id"] not in used] for (cd, m) in cells]
        i = 0
        while deficit > 0 and any(match_pools):
            pool = match_pools[i % len(match_pools)]
            if pool:
                c = pool.pop(0)
                chosen.append((c, "|".join((c["cond"], c["model"], "MATCH")) + "|fill"))
                used.add(c["claim_id"])
                deficit -= 1
            i += 1
    rnd.shuffle(chosen)  # 시트 순서 무작위(블라인드)

    sheet, key = [], []
    for c, stratum in chosen:
        q = qmap.get(c["qid"], {})
        sheet.append({
            "claim_id": c["claim_id"], "question_ko": q.get("question_ko", ""),
            "context_markdown": q.get("context_markdown", ""), "gold_required_display": gold_display(q),
            "sentence": bold_span(c["sentence"], c["span"]), "human_label": "", "missed_claims": "", "note": "",
        })
        key.append({
            "claim_id": c["claim_id"], "qid": c["qid"], "cond": c["cond"], "model": c["model"], "rep": c["rep"],
            "auto_verdict": c["verdict"], "auto_code": c["code"], "type": c["type"], "value": c["value"],
            "matched_gold_name": c["matched_gold_name"], "stratum": stratum,
        })
    os.makedirs(outdir, exist_ok=True)
    pd.DataFrame(sheet).to_csv(os.path.join(outdir, "human_sheet.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(key).to_csv(os.path.join(outdir, "human_key.csv"), index=False, encoding="utf-8-sig")
    counts = pd.Series([s for _, s in chosen]).value_counts().to_dict()
    print(f"[human_sample] wrote {len(sheet)} rows (requested {n}); strata: {counts}; deficit_unfilled={deficit}")
    return sheet, key


def cohen_kappa(pairs):
    """pairs: [(a, b)] 범주 라벨. 자체 계산."""
    n = len(pairs)
    if n == 0:
        return float("nan")
    cats = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    po = sum(a == b for a, b in pairs) / n
    pe = 0.0
    for c in cats:
        pa = sum(a == c for a, _ in pairs) / n
        pb = sum(b == c for _, b in pairs) / n
        pe += pa * pb
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1 - pe)


def score(outdir):
    sheet = pd.read_csv(os.path.join(outdir, "human_sheet.csv"), dtype=str, encoding="utf-8-sig", keep_default_na=False)
    key = pd.read_csv(os.path.join(outdir, "human_key.csv"), dtype=str, encoding="utf-8-sig", keep_default_na=False)
    df = sheet.merge(key, on="claim_id", how="inner")
    df["human_label"] = df["human_label"].str.strip().str.lower()
    unlabeled = df[~df["human_label"].isin(LABELS)]
    df = df[df["human_label"].isin(LABELS)]
    nac = df[df["human_label"] == "not_a_claim"]
    ev = df[df["human_label"] != "not_a_claim"].copy()
    ev["human_bin"] = ev["human_label"].map(lambda s: "MATCH" if s == "supported" else "MISMATCH")
    pairs = list(zip(ev["auto_verdict"], ev["human_bin"]))
    agree = sum(a == b for a, b in pairs) / len(pairs) if pairs else float("nan")
    fp = int(((ev["auto_verdict"] == "MISMATCH") & (ev["human_bin"] == "MATCH")).sum())
    fn = int(((ev["auto_verdict"] == "MATCH") & (ev["human_bin"] == "MISMATCH")).sum())
    missed = pd.to_numeric(df["missed_claims"].replace("", "0"), errors="coerce").fillna(0)
    # 단위 오류 세부: 자동 K2/K4 vs 사람 unit_error
    unit_pairs = list(zip(ev["auto_code"].isin(["K2", "K4"]), ev["human_label"] == "unit_error"))
    out = {
        "n_sheet_rows": int(len(sheet)), "n_labeled": int(len(df)), "n_unlabeled_or_invalid": int(len(unlabeled)),
        "n_evaluable": int(len(ev)), "n_not_a_claim": int(len(nac)),
        "agreement": agree, "cohen_kappa": cohen_kappa(pairs),
        "false_positive_auto_mismatch_human_supported": fp,
        "false_negative_auto_match_human_unsupported": fn,
        "unit_error_agreement": (sum(a == b for a, b in unit_pairs) / len(unit_pairs)) if unit_pairs else None,
        "unit_error_kappa": cohen_kappa([(str(a), str(b)) for a, b in unit_pairs]) if unit_pairs else None,
        "missed_claims_total": int(missed.sum()),
        "missed_claims_responses_with_any": int((missed > 0).sum()),
        "confusion": {f"auto_{a}|human_{b}": int(((ev['auto_verdict'] == a) & (ev['human_label'] == b)).sum())
                      for a in ("MATCH", "MISMATCH") for b in ("supported", "unsupported", "unit_error")},
        "by_cell": {},
    }
    for (cd, m), sub in ev.groupby(["cond", "model"]):
        p = list(zip(sub["auto_verdict"], sub["human_bin"]))
        out["by_cell"][f"{cd}|{m}"] = {"n": len(p), "agreement": sum(a == b for a, b in p) / len(p), "kappa": cohen_kappa(p)}
    with open(os.path.join(outdir, "human_eval.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--claims", default=os.path.join(HERE, "results", "claims.jsonl"))
    ap.add_argument("--questions", default=os.path.join(HERE, "data", "questions.csv"))
    ap.add_argument("--outdir", default=os.path.join(HERE, "results"))
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args(argv)
    if a.score:
        score(a.outdir)
    else:
        sample(a.claims, a.questions, a.outdir, a.n, a.seed)


if __name__ == "__main__":
    main()
