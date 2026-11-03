# -*- coding: utf-8 -*-
"""합성 픽스처로 score.py → analyze.py → human_sample.py 를 끝까지 돌리고, 심어 둔 오류의 K 코드를 확인한다."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import analyze  # noqa: E402
import human_sample  # noqa: E402
import score  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
EXPECTED = {
    ("q_emp_001", "pivot", "claude-opus-5-5", 1): "K4",
    ("q_pop_001", "direct", "claude-haiku-5-5", 2): "K2",
    ("q_pop_001", "direct", "claude-opus-5-5", 3): "K1",
    ("q_cpi_001", "direct", "claude-haiku-5-5", 1): "K3",
    ("q_pop_001", "pivot", "claude-haiku-5-5", 2): "K6",
}


def test_pipeline_end_to_end():
    out = tempfile.mkdtemp(prefix="fx_")
    pq = score.run(os.path.join(FIX, "results_fixture.jsonl"), os.path.join(FIX, "questions_fixture.csv"), out)
    assert len(pq) == 36
    claims = [json.loads(l) for l in open(os.path.join(out, "claims.jsonl"), encoding="utf-8")]
    mism = [c for c in claims if c["verdict"] == "MISMATCH"]
    assert len(mism) == 5, [(c["claim_id"], c["code"]) for c in mism]
    got = {(c["qid"], c["cond"], c["model"], c["rep"]): c["code"] for c in mism}
    assert got == EXPECTED, got
    # 마커 누락 응답 1건 → format_fail
    ff = pq[pq["format_fail"]]
    assert len(ff) == 1 and tuple(ff.iloc[0][["qid", "cond", "model", "rep"]]) == ("q_emp_001", "direct", "claude-haiku-5-5", 3)
    # 정상 응답은 H=0, 커버리지 1
    ok = pq[~pq[["qid", "cond", "model", "rep"]].apply(tuple, axis=1).isin(set(EXPECTED) | {("q_emp_001", "direct", "claude-haiku-5-5", 3)})]
    assert (ok["H"] == 0).all() and (ok["C"] == 1).all()
    assert os.path.exists(os.path.join(out, "per_cell_question.csv"))

    s = analyze.run(out, os.path.join(out, "figures"), B=2000, seed=42)
    assert s["primary"]["ci_method"] == "percentile" and s["primary"]["bca_degenerate"]  # Q=3 → 퇴화 폴백
    assert os.path.exists(os.path.join(out, "figures", "fig2_forest.png"))
    assert os.path.exists(os.path.join(out, "table2.csv")) and os.path.exists(os.path.join(out, "summary.json"))

    sheet, key = human_sample.sample(os.path.join(out, "claims.jsonl"), os.path.join(FIX, "questions_fixture.csv"), out, 50, 42)
    assert len(sheet) == 50 and len(key) == 50
    assert "cond" not in sheet[0] and "auto_verdict" not in sheet[0]  # 블라인드
    assert all("**" in r["sentence"] for r in sheet)


def test_cohen_kappa_by_hand():
    pairs = [("A", "A")] * 20 + [("B", "B")] * 10 + [("A", "B")] * 5 + [("B", "A")] * 5
    # po=0.75, pA_auto=25/40, pA_h=25/40 → pe=0.625²+0.375²=0.53125 → κ=(0.75-0.53125)/(1-0.53125)
    assert abs(human_sample.cohen_kappa(pairs) - (0.75 - 0.53125) / (1 - 0.53125)) < 1e-12
