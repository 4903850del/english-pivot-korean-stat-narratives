# -*- coding: utf-8 -*-
"""score.py 매칭 규칙 단위 테스트: K6 적용 범위, 표시값 차이 폴백, target_cells/off_target_cell."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import score  # noqa: E402
from normalize import extract_claims  # noqa: E402

CELLS = [
    {"row_label": "인천광역시", "col_label": "총인구수", "period": "2022", "dt_raw": "2967314", "value": 2967314.0, "flag": ""},
    {"row_label": "인천광역시", "col_label": "총인구수", "period": "2023", "dt_raw": "2997410", "value": 2997410.0, "flag": ""},
    {"row_label": "인천광역시", "col_label": "총인구수", "period": "2024", "dt_raw": "3021010", "value": 3021010.0, "flag": ""},
    {"row_label": "서울특별시", "col_label": "총인구수", "period": "2024", "dt_raw": "9331822", "value": 9331822.0, "flag": ""},
]
REQ = [
    {"name": "2024년 전년 대비 인구 증감률(%)", "type": "PCT", "exact": -0.578, "display": "-0.6", "decimals": 1, "sign": -1, "formula": "(v24/v23-1)*100"},
    {"name": "2023년 전년 대비 인구 증감률(%)", "type": "PCT", "exact": -0.449, "display": "-0.4", "decimals": 1, "sign": -1, "formula": "(v23/v22-1)*100"},
]
AUX = [
    {"name": "2024년 전년 대비 인구 증감(명)", "type": "COUNT", "exact": 23600.0, "display": "23,600", "decimals": 0, "sign": 1, "formula": "v24-v23"},
    {"name": "2023년 전년 대비 인구 증감(명)", "type": "COUNT", "exact": 30096.0, "display": "30,096", "decimals": 0, "sign": 1, "formula": "v23-v22"},
    {"name": "증감률 차이(%p)", "type": "PP", "exact": -0.129, "display": "-0.1", "decimals": 1, "sign": -1, "formula": "r24-r23"},
]


def _q(target=None):
    q = {"qid": "t", "unit_nm": "명", "context_cells": CELLS, "gold_required": REQ, "gold_aux": AUX}
    if target is not None:
        q["target_cells"] = target
    return q


def _score(text, q):
    golds = score.build_gold(q)
    claims = extract_claims(text)
    score.attach_neighbors(claims)
    return [(c["span"], score.match_claim(c, golds)) for c in claims]


def test_k6_not_applied_to_aux_count_trend_sentences():
    r = _score("증가 폭은 2023년 30,096명에서 2024년 23,600명으로 줄어들었습니다.", _q())
    assert [(s, m["verdict"], m["code"]) for s, m in r] == [("30,096명", "MATCH", None), ("23,600명", "MATCH", None)]
    r = _score("감소 인원은 2023년 30,096명에서 2024년 23,600명으로 확대되었다.", _q())
    assert all(m["verdict"] == "MATCH" for _, m in r)


def test_k6_applied_only_to_required_pct_pp():
    # required PCT, 부호 반대 → K6
    r = _score("2024년 인구는 전년 대비 0.6% 증가했다.", _q())
    assert r[0][1]["code"] == "K6"
    # 같은 값을 감소로 쓰면 MATCH
    r = _score("2024년 인구는 전년 대비 0.6% 감소했다.", _q())
    assert r[0][1]["verdict"] == "MATCH"
    # aux PP 는 방향어가 반대여도 K6 없음
    q = _q(); q = dict(q, gold_required=[], gold_aux=AUX + REQ)
    r = _score("2024년 인구는 전년 대비 0.6% 증가했다.", q)
    assert r[0][1]["verdict"] == "MATCH" and r[0][1]["code"] is None


def test_display_diff_fallback_for_pp_claims():
    # exact 차이 0.129 이지만 모델이 쓴 표시값(-0.4, -0.6) 차이 0.2 → MATCH + display_diff (v1.5)
    r = _score("증감률은 -0.4%에서 -0.6%로 감소 폭이 0.2%p 확대되어 인구 감소 속도가 빨라졌다.", _q())
    m = r[-1][1]
    assert m["verdict"] == "MATCH" and m["display_diff"] is True and m["matched_gold_kind"] == "display_diff"
    # 피연산자를 쓰지 않으면 정답 값끼리의 차이로는 일치 처리하지 않는다 (v1.5)
    r = _score("감소 폭이 0.2%p 확대되어 인구 감소 속도가 빨라졌다.", _q())
    assert r[0][1]["verdict"] == "MISMATCH" and r[0][1]["display_diff"] is False
    # 표시값 차이와도 안 맞는 값은 여전히 K2/K3
    r = _score("감소 폭이 0.5%p 확대되었다.", _q())
    assert r[0][1]["verdict"] == "MISMATCH" and r[0][1]["code"] in ("K2", "K3")
    # basis:"display" 변형 aux 항목이 있으면 독립 항목으로 바로 MATCH
    q = _q(); q = dict(q, gold_aux=AUX + [dict(AUX[2], exact=-0.2, display="-0.2", basis="display", name="증감률 차이(%p)[display]")])
    r = _score("감소 폭이 0.2%p 확대되었다.", q)
    assert r[0][1]["verdict"] == "MATCH" and r[0][1]["display_diff"] is False and r[0][1]["matched_gold_kind"] == "aux"


def test_target_cells_off_target_flag():
    target = CELLS[:3]  # 서울 셀은 비대상
    r = _score("인천 인구는 3,021,010명이고 서울은 9,331,822명이다.", _q(target))
    (s1, m1), (s2, m2) = r
    assert m1["verdict"] == "MATCH" and m1["off_target_cell"] is False and m1["matched_gold_kind"] == "cell"
    assert m2["verdict"] == "MATCH" and m2["off_target_cell"] is True and m2["matched_gold_kind"] == "offcell"
    # target_cells 가 없으면 모두 일반 셀
    r = _score("서울은 9,331,822명이다.", _q())
    assert r[0][1]["off_target_cell"] is False and r[0][1]["matched_gold_kind"] == "cell"


def test_off_target_counts_in_response_record():
    q = _q(CELLS[:3])
    golds = score.build_gold(q)
    row = {"qid": "t", "cond": "direct", "model": "m", "rep": 1,
           "response": "===ANALYSIS===\nx\n===NARRATIVE===\n인천 인구는 3,021,010명이고 서울은 9,331,822명이다. 증감률은 -0.4%에서 -0.6%로 감소 폭이 0.2%p 확대되었다.\n===END==="}
    rec, cl = score.score_response(row, q, golds)
    assert rec["n_off_target_cell"] == 1 and rec["n_display_diff"] == 1 and rec["n_mismatch"] == 0 and rec["H"] == 0.0


def test_target_cells_column_absent_or_blank_string():
    import pandas as pd, tempfile, json
    df = pd.DataFrame([{"qid": "t", "unit_nm": "명", "context_cells": json.dumps(CELLS), "gold_required": json.dumps(REQ),
                        "gold_aux": json.dumps(AUX), "target_cells": ""}])
    p = os.path.join(tempfile.mkdtemp(), "q.csv")
    df.to_csv(p, index=False, encoding="utf-8-sig")
    qs = score.load_questions(p)
    assert qs["t"]["target_cells"] == [] and all(g["kind"] != "offcell" for g in score.build_gold(qs["t"]))


# ---- gold_pool ------------------------------------------------------------
POOL = [
    {"row": "15 - 19세", "name": "남녀 격차(%p)", "type": "PP", "exact": -3.08, "display": "-3.1", "decimals": 1, "sign": -1, "formula": "m-f"},
    {"row": "전국", "name": "2024년 전년 대비 인구 증감률(%)", "type": "PCT", "exact": -0.21, "display": "-0.2", "decimals": 1, "sign": -1, "formula": "(v24/v23-1)*100"},
    {"row": "세종특별자치시", "name": "2024년 증감률 순위", "type": "RANK", "exact": 1, "display": "1", "decimals": 0, "sign": 1, "formula": "rank desc"},
]


def _qp():
    q = _q(CELLS[:3])
    q["gold_pool"] = POOL
    return q


def test_pool_match_flagged_not_counted_in_c_or_h():
    r = _score("15 - 19세 격차는 -3.1%p이고 전국 인구는 −0.2% 감소했으며 1위는 세종특별자치시다.", _qp())
    assert [(s, m["verdict"], m["pool"], m["matched_gold_kind"]) for s, m in r] == [
        ("-3.1%p", "MATCH", True, "pool"), ("−0.2%", "MATCH", True, "pool"), ("1위", "MATCH", True, "pool")]
    golds = score.build_gold(_qp())
    row = {"qid": "t", "cond": "direct", "model": "m", "rep": 1,
           "response": "===ANALYSIS===\nx\n===NARRATIVE===\n인천은 전년 대비 0.6% 감소했고 전국은 −0.2% 감소했으며 1위는 세종이다.\n===END==="}
    rec, cl = score.score_response(row, _qp(), golds)
    assert rec["n_pool"] == 2 and rec["n_mismatch"] == 0 and rec["n_required_matched"] == 1  # pool 은 C 에 불포함


def test_pool_k4_cross_type():
    r = _score("15 - 19세 격차는 3.1%다.", _qp())  # pool 에는 PP 만 있음 → %↔%p 단위 오류
    m = r[0][1]
    assert m["verdict"] == "MISMATCH" and m["code"] == "K4" and m["matched_gold_kind"] == "pool"


def test_pool_k6_not_applied():
    r = _score("전국 인구는 0.2% 증가했다.", _qp())  # pool PCT sign=-1, 방향 반대여도 K6 없음
    assert r[0][1]["verdict"] == "MATCH" and r[0][1]["pool"] is True


def test_pool_precedence_required_first():
    q = _qp(); q["gold_pool"] = POOL + [dict(REQ[0], row="인천광역시", name="dup")]
    r = _score("인천은 전년 대비 0.6% 감소했다.", q)
    assert r[0][1]["matched_gold_kind"] == "required" and r[0][1]["pool"] is False


def test_pool_column_absent_behaves_as_before():
    r = _score("15 - 19세 격차는 -3.1%p이고 1위는 세종이다.", _q(CELLS[:3]))
    assert [m["code"] for _, m in r] == ["K1", "K1"]  # PP required 가 없으므로 K1 (SPEC 규칙)
    assert score.load_questions.__name__  # smoke


# ---- gold_pool 귀속(attribution) ---------------------------------------------
def test_row_aliases():
    assert "충북" in score.row_aliases("충청북도") and "서울" in score.row_aliases("서울특별시")
    assert "20대" in score.row_aliases("20 - 29세") and "15-19세" in score.row_aliases("15 - 19세")
    assert "60세이상" in score.row_aliases("60세이상") and "60대이상" in score.row_aliases("60세이상")
    assert "식료품" in score.row_aliases("식료품 및 비주류음료") and "전체물가" in score.row_aliases("총지수")


def test_pool_requires_row_attribution_in_sentence_or_previous():
    # 같은 문장에 행 라벨(별칭) → pool MATCH
    r = _score("15 - 19세 격차는 -3.1%p이다.", _qp())
    assert r[0][1]["pool"] is True and r[0][1]["pool_row"] == "15 - 19세"
    r = _score("10대 후반은 다르다. 격차가 -3.1%p였다.", _qp())  # 직전 문장에 별칭
    assert r[0][1]["pool"] is True
    # 행 언급 없음 → 값이 같아도 pool 불가 → K 코드 + pool_unattributed
    r = _score("여성이 남성보다 높은 집단도 있다. 그 격차는 -3.1%p였다. 서울은 다르다.", _qp())
    m = r[0][1]
    assert m["verdict"] == "MISMATCH" and m["pool"] is False and m["pool_unattributed"] is True
    # 다른 행 라벨만 있음 → 역시 불가
    r = _score("전국 격차는 -3.1%p였다.", _qp())
    assert r[0][1]["pool"] is False and r[0][1]["pool_unattributed"] is True


def test_pool_target_row_items_need_no_attribution():
    q = _qp(); q["gold_pool"] = POOL + [dict(REQ[1], row="인천광역시", name="인천 2023 증감률")]
    q["gold_required"] = [REQ[0]]  # 2023 증감률은 pool 에만 존재, 대상 행(인천)
    r = _score("전년 대비 증감률은 −0.4%였다.", q)
    assert r[0][1]["pool"] is True and r[0][1]["pool_unattributed"] is False


def test_pool_unattributed_counts_per_response():
    golds = score.build_gold(_qp())
    row = {"qid": "t", "cond": "direct", "model": "m", "rep": 1,
           "response": "===ANALYSIS===\nx\n===NARRATIVE===\n전국 인구는 −0.2% 감소했다. 어떤 집단은 격차가 -3.1%p였다.\n===END==="}
    rec, cl = score.score_response(row, _qp(), golds)
    assert rec["n_pool"] == 1 and rec["n_pool_unattributed"] == 1 and rec["n_mismatch"] == 1


def test_coverage_counts_equal_valued_required_items_with_one_claim():
    q = _q()
    q["gold_required"] = [
        {"name": "40 - 49세 격차", "type": "PP", "exact": 23.9, "display": "23.9", "decimals": 1, "sign": 1, "formula": "m-f"},
        {"name": "최대 격차", "type": "PP", "exact": 23.9, "display": "23.9", "decimals": 1, "sign": 1, "formula": "max"},
    ]
    golds = score.build_gold(q)
    row = {"qid": "t", "cond": "direct", "model": "m", "rep": 1,
           "response": "===ANALYSIS===\nx\n===NARRATIVE===\n격차는 23.9%p로 가장 크다.\n===END==="}
    rec, cl = score.score_response(row, q, golds)
    assert rec["n_claims"] == 1 and rec["n_mismatch"] == 0 and rec["n_required_matched"] == 2 and rec["C"] == 1.0


def test_sign_aware_candidate_selection():
    q = _q()
    q["gold_required"] = [
        {"name": "누적 증감률", "type": "PCT", "exact": -1.136, "display": "-1.1", "decimals": 1, "sign": -1, "formula": "(v24/v22-1)*100"},
        {"name": "최고 증감률(세종)", "type": "PCT", "exact": 1.076, "display": "1.1", "decimals": 1, "sign": 1, "formula": "max"},
    ]
    r = _score("2022년 대비 2024년 누적 증감률은 −1.1%이다.", q)
    m = r[0][1]
    assert m["verdict"] == "MATCH" and m["matched_gold_name"] == "누적 증감률"
    r = _score("가장 높은 시도는 1.1% 증가했다.", q)
    assert r[0][1]["verdict"] == "MATCH" and r[0][1]["matched_gold_name"] == "최고 증감률(세종)"
    # 부호 일치 후보가 없으면 가장 가까운 것 + K6
    q["gold_required"] = q["gold_required"][1:]
    r = _score("증감률은 −1.1%였다.", q)
    assert r[0][1]["code"] == "K6"


def test_display_diff_for_count_claims_sex_ratio():
    q = _q()
    q["gold_required"] = [
        {"name": "2023년 성비", "type": "COUNT", "exact": 99.24, "display": "99.2", "decimals": 1, "sign": 1, "formula": "m/f*100"},
        {"name": "2024년 성비", "type": "COUNT", "exact": 99.07, "display": "99.1", "decimals": 1, "sign": 1, "formula": "m/f*100"},
    ]
    r = _score("성비는 2023년 99.2명에서 2024년 99.1명으로 0.1명 줄었다.", q)
    assert [(m["verdict"], m["display_diff"]) for _, m in r] == [("MATCH", False), ("MATCH", False), ("MATCH", True)]


def test_excluded_counts_in_response_record():
    golds = score.build_gold(_q())
    row = {"qid": "t", "cond": "direct", "model": "m", "rep": 1,
           "response": "===ANALYSIS===\nx\n===NARRATIVE===\n| 순위 | 시도 |\n|---|---|\n| 1 | 인천 |\n2. 인천은 3,021,010명이다.\n===END==="}
    rec, cl = score.score_response(row, _q(), golds)
    assert rec["n_excluded_table_list"] == 2 and rec["n_claims"] == 1 and rec["n_mismatch"] == 0



# ---- 본실행 감사(audit) 패턴: P3 PT 매칭, P7 로마자 귀속, P9 K4 허용오차, RANK K3, 전국 별칭, END 누락 ----
def test_P3_PT_matches_index_then_pp():
    q = _q()
    q["gold_aux"] = AUX + [{"name": "교육-음식 지수 격차", "type": "PP", "exact": -14.76, "display": "-14.76", "decimals": 2, "sign": -1, "formula": "a-b"}]
    r = _score("두 항목 간 지수 격차는 −14.76포인트입니다.", q)
    assert r[0][1]["verdict"] == "MATCH" and r[0][1]["matched_gold_type"] == "PP"
    q["gold_aux"] = AUX + [{"name": "지수 격차", "type": "INDEX", "exact": 14.76, "display": "14.76", "decimals": 2, "sign": 1, "formula": "a-b"}]
    r = _score("지수 격차는 14.76포인트다.", q)
    assert r[0][1]["verdict"] == "MATCH" and r[0][1]["matched_gold_type"] == "INDEX"
    # 지수 셀 쌍의 표시값 차이도 허용 (display_diff)
    q = _q(); q["unit_nm"] = "2020=100"
    q["context_cells"] = [{"row_label": "교육", "col_label": "지수", "period": "2024", "dt_raw": "106.01", "value": 106.01, "flag": ""},
                          {"row_label": "음식", "col_label": "지수", "period": "2024", "dt_raw": "120.77", "value": 120.77, "flag": ""}]
    r = _score("교육이 음식보다 14.76포인트 낮아", q)
    assert r[0][1]["verdict"] == "MATCH" and r[0][1]["display_diff"] is True
    # 못 맞으면 K3 (required 에 PP/INDEX 있을 때)
    q = _q(); q["gold_required"] = [dict(REQ[0], type="PP")]
    r = _score("격차는 99.9포인트다.", q)
    assert r[0][1]["code"] == "K3"


def test_P7_romanized_row_aliases_case_insensitive():
    assert "seoul" in score.row_aliases("서울특별시") and "chungnam" in score.row_aliases("충청남도")
    assert "jeonbuk" in score.row_aliases("전북특별자치도") and "nationwide" in score.row_aliases("전국")
    assert "20s" in score.row_aliases("20 - 29세") and "60andover" in score.row_aliases("60세이상")
    assert "transport" in score.row_aliases("교통") and "allitems" in score.row_aliases("총지수")
    pool = [{"row": "충청남도", "name": "성비", "type": "COUNT", "exact": 105.35, "display": "105.4", "decimals": 1, "sign": 1, "formula": "m/f*100"}]
    q = _q(CELLS[:3]); q["gold_pool"] = pool
    r = _score("- Chungnam 105.4 (1,096,128 / 1,040,446)", q)
    assert r[0][1]["pool"] is True and r[0][1]["pool_row"] == "충청남도"
    r = _score("- CHUNGCHEONGNAM 105.4", q)
    assert r[0][1]["pool"] is True


def test_P9_cross_factor_tolerance_not_scaled_for_approx():
    q = _q()
    q["gold_aux"] = AUX + [{"name": "4위와의 인구 차이", "type": "COUNT", "exact": 38012.0, "display": "38,012", "decimals": 0, "sign": 1, "formula": "a-b"}]
    r = _score("인구 차이가 약 3만 8천 명에 불과해", q)
    assert r[0][1]["verdict"] == "MATCH"
    # approx 1% 오차에 배수 f 가 곱해지면 큰 셀 값에 엉뚱하게 K4 가 되던 사례: 이제 MATCH 가 없으면 K1/K2
    q["gold_aux"] = AUX
    r = _score("인구 차이가 약 3만 8천 명에 불과해", q)
    assert r[0][1]["code"] in ("K1", "K2")


def test_rank_mismatch_is_K3_when_rank_gold_exists():
    q = _q(); q["gold_required"] = REQ + [{"name": "순위", "type": "RANK", "exact": 5, "display": "5", "decimals": 0, "sign": 1, "formula": "rank"}]
    r = _score("격차 순위는 6위이다.", q)
    assert r[0][1]["verdict"] == "MISMATCH" and r[0][1]["code"] == "K3"
    r = _score("격차 순위는 6위이다.", _q())  # RANK gold 없음 → K1
    assert r[0][1]["code"] == "K1"


def test_nationwide_aliases_17_sido():
    al = score.row_aliases("전국")
    assert "17개시도전체" in al and "전체시도" in al and "시도합계" in al
    q = _q(CELLS[:3]); q["gold_pool"] = [dict(REQ[0], row="전국", name="전국 증감률", exact=-0.21, display="-0.2")]
    r = _score("17개 시도 전체 증감률은 −0.2%이며", q)
    assert r[0][1]["pool"] is True and r[0][1]["pool_row"] == "전국"


def test_end_missing_scores_only_narrative_part():
    golds = score.build_gold(_q())
    row = {"qid": "t", "cond": "direct", "model": "m", "rep": 1,
           "response": "===ANALYSIS===\n계산: 3,021,010 / 2,997,410 = 1.0079, 즉 0.79%\n===NARRATIVE===\n인천 인구는 3,021,010명으로 전년 대비 −0.6% 변동했다.\n"}
    rec, cl = score.score_response(row, _q(), golds)
    assert rec["format_fail"] is True and rec["n_claims"] == 2 and all(c["verdict"] == "MATCH" for c in cl)


def test_magnitude_words_not_direction_and_PT_sex_ratio_points():
    from normalize import extract_claims
    c = extract_claims("격차는 2022년 -3.0%p에서 2024년 -3.1%p로 0.1%p 확대되어")[-1]
    assert c["value"] == 0.1 and c["direction"] is None  # 확대/축소 는 크기 추세어, 방향어 아님
    q = _q()
    q["gold_required"] = [
        {"name": "2023년 성비", "type": "COUNT", "exact": 102.12, "display": "102.1", "decimals": 1, "sign": 1, "formula": "m/f*100"},
        {"name": "2024년 성비", "type": "COUNT", "exact": 102.41, "display": "102.4", "decimals": 1, "sign": 1, "formula": "m/f*100"},
    ]
    r = _score("성비는 2023년 102.1에서 2024년 102.4로 0.3포인트 상승하였다.", q)
    assert [(m["verdict"], m["display_diff"]) for _, m in r] == [("MATCH", False), ("MATCH", False), ("MATCH", True)]



def test_absolute_rank_aux_requires_stated_criterion():
    q = _q()
    q["gold_required"] = REQ + [{"name": "15-19세 격차 순위(부호 기준)", "type": "RANK", "exact": 5, "display": "5", "decimals": 0, "sign": 1, "formula": "rank"}]
    q["gold_aux"] = AUX + [{"name": "15-19세 격차 순위(절댓값 기준)", "type": "RANK", "exact": 6, "display": "6", "decimals": 0, "sign": 1, "formula": "rank abs"}]
    # 기준 언급 없음 → 절댓값 aux 는 후보 제외 → 부호 기준 5위와 불일치 → K3
    r = _score("15-19세의 격차 순위는 6위이다.", q)
    assert r[0][1]["verdict"] == "MISMATCH" and r[0][1]["code"] == "K3"
    # 절댓값 기준을 명시하면 aux 로 MATCH
    r = _score("격차의 절댓값 기준으로는 15-19세가 6위다.", q)
    assert r[0][1]["verdict"] == "MATCH" and "절댓값" in r[0][1]["matched_gold_name"]
    r = _score("격차 크기 순으로 보면 다르다. 15-19세는 6위다.", q)  # 직전 문장
    assert r[0][1]["verdict"] == "MATCH"
    r = _score("By absolute magnitude, ages 15-19 rank 6위.", q)
    assert r[0][1]["verdict"] == "MATCH"
    q["gold_aux"][-1] = dict(q["gold_aux"][-1], exact=5, display="5"); q["gold_required"][-1] = dict(q["gold_required"][-1], exact=6, display="6")
    r = _score("부호를 무시한 절대 격차 기준으로는 15-19세가 5위이다.", q)  # emp_E5_3|direct|opus|2 (감사 B)
    assert r[0][1]["verdict"] == "MATCH" and "절댓값" in r[0][1]["matched_gold_name"]
    assert _score("절대적 크기로 보면 15-19세는 5위이다.", q)[0][1]["verdict"] == "MATCH"



def test_ref_kind_split():
    q = _q()
    q["gold_pool"] = POOL
    golds = score.build_gold(q)
    row = {"qid": "t", "cond": "direct", "model": "m", "rep": 1,
           "response": "===ANALYSIS===\nx\n===NARRATIVE===\n인천은 전년 대비 0.6% 감소했고 증감률이 1.9%로 틀렸다. 증감 인원은 23,600명, 전국은 −0.2% 감소했다. 인구는 3,021,010명이다. 1위는 세종이다. 성장률은 55.5%다.\n===END==="}
    rec, cl = score.score_response(row, q, golds)
    kinds = {c["span"]: (c["ref_kind"], c["verdict"]) for c in cl}
    assert kinds["0.6%"] == ("required", "MATCH")
    assert kinds["1.9%"][0] == "required" and kinds["1.9%"][1] == "MISMATCH"  # 최근접 같은 타입 gold 가 required
    assert kinds["23,600명"] == ("aux", "MATCH")
    assert kinds["−0.2%"] == ("pool", "MATCH")
    assert kinds["3,021,010명"] == ("cell", "MATCH")
    assert kinds["1위"] == ("pool", "MATCH")
    assert rec["n_req_claims"] == 3 and rec["n_req_mismatch"] == 2 and rec["n_other_claims"] == 4 and rec["n_other_mismatch"] == 0
    assert abs(rec["H_required"] - 2 / 3) < 1e-9 and rec["H_other"] == 0.0


def test_display_diff_requires_written_operands_v15():
    # 실제 감사 사례: 4.1 과 1.7 의 차이는 2.4 인데 2.5 로 쓴 문장은 불일치여야 한다
    r = _score("A의 상승률은 4.1%로 B의 상승률 1.7%보다 2.5%p 높았다.", _q())
    assert r[-1][1]["verdict"] == "MISMATCH" and r[-1][1]["display_diff"] is False
    # 같은 쌍의 올바른 차이는 일치
    r = _score("A의 상승률은 4.1%로 B의 상승률 1.7%보다 2.4%p 높았다.", _q())
    assert r[-1][1]["verdict"] == "MATCH" and r[-1][1]["display_diff"] is True
    # 음수 피연산자의 부호 반영: 1.1 과 -0.9 의 차이 2.0
    r = _score("A는 1.1%, B는 -0.9%로 격차는 2.0%p였다.", _q())
    assert r[-1][1]["verdict"] == "MATCH" and r[-1][1]["display_diff"] is True
    # 직전 문장의 피연산자도 인정
    r = _score("A는 17.3%로 1위였다. 다만 2위 B(16.6%)와의 차이는 0.7%p였다.", _q())
    assert r[-1][1]["verdict"] == "MATCH" and r[-1][1]["display_diff"] is True


def test_percent_point_on_count_operands_is_not_display_diff_v15():
    # 성비(명)의 차이에 %p 를 쓰면 표시값 차이로 인정하지 않는다
    r = _score("성비는 96.6명에서 96.3명으로 0.3%p 낮아졌다.", _q())
    assert r[-1][1]["verdict"] == "MISMATCH" and r[-1][1]["display_diff"] is False


def test_rank_predecessor_row_not_attributed_v15():
    claim = {"type": "RANK", "span": "2위", "sentence": "보건은 5개 항목 중 식료품 및 비주류음료(3.9%)에 이어 2위입니다."}
    assert score.is_rank_predecessor(claim, "식료품 및 비주류음료") is True
    assert score.is_rank_predecessor(claim, "보건") is False
    assert score.is_rank_predecessor(dict(claim, type="PCT"), "식료품 및 비주류음료") is False
