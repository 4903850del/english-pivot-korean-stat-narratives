# -*- coding: utf-8 -*-
"""합성 픽스처 생성기 — SPEC §2/§4 스키마를 따르는 questions_fixture.csv + results_fixture.jsonl.

3문항(pop/emp/cpi) × 2조건 × 2모델 × 3회 = 36응답. 심어 둔 오류(PLANTED 참조):
  K4  %↔%p 교체        : q_emp_001 | pivot  | opus  | rep1  "격차는 16.2%"(정답 16.2%p)
  K2  반올림 오류       : q_pop_001 | direct | haiku | rep2  "-0.5%"(정답 -0.6, exact -0.5776)
  K1  날조 수치         : q_pop_001 | direct | opus  | rep3  "인천은 3,000,000명"
  K3  계산 오류         : q_cpi_001 | direct | haiku | rep1  "식료품 상승률 5.3%"(정답 4.1)
  K6  방향 오류         : q_pop_001 | pivot  | haiku | rep2  "0.6% 증가"(정답 -0.6 감소)
  format_fail 마커 누락 : q_emp_001 | direct | haiku | rep3  ===END=== 없음
실행:  PYTHONUTF8=1 py -3.11 tests/fixtures/make_fixture.py
"""
import csv
import hashlib
import json
import os
from decimal import ROUND_HALF_UP, Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-5-5"


def rnd(x, d):
    q = Decimal(1).scaleb(-d)
    return str(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def gold(name, typ, exact, d, formula):
    return {"name": name, "type": typ, "exact": exact, "display": rnd(exact, d), "decimals": d,
            "sign": 1 if exact > 0 else (-1 if exact < 0 else 0), "formula": formula}


def cells(rows, unit):
    out = []
    for row_label, col_label, period, v in rows:
        out.append({"row_label": row_label, "col_label": col_label, "period": period,
                    "dt_raw": f"{v}", "value": float(v), "flag": ""})
    return out


def md_table(title_cols, rows, unit):
    hdr = "| 구분 | " + " | ".join(title_cols) + " |"
    sep = "|---|" + "---|" * len(title_cols)
    body = "\n".join(f"| {k} | " + " | ".join(str(x) for x in v) + " |" for k, v in rows)
    return f"{hdr}\n{sep}\n{body}\n(단위: {unit})"


# ---------------------------------------------------------------------------
# 문항 3개
# ---------------------------------------------------------------------------
S23, S24, B23, B24 = 9386034, 9331822, 3293362, 3266598
E = {"m23": 71.3, "m24": 71.8, "f23": 54.1, "f24": 55.6}
C = {"food23": 118.3, "food24": 123.1, "tr23": 112.0, "tr24": 113.2}

q_pop = {
    "qid": "q_pop_001", "table_key": "pop", "template_id": "P1", "org_id": "101", "tbl_id": "DT_1B040A3",
    "itm_id": "T20", "obj_codes": json.dumps(["11", "26"]), "prd_se": "Y", "periods": json.dumps(["2023", "2024"]),
    "unit_nm": "명", "fetch_date": "2026-11-02", "lst_chn_de": "2025-01-10",
    "source_citation": "KOSIS, 행정안전부 주민등록인구현황", "table_title_ko": "행정구역별 주민등록인구",
    "context_cells": json.dumps(cells([("서울특별시", "총인구", "2023", S23), ("서울특별시", "총인구", "2024", S24),
                                       ("부산광역시", "총인구", "2023", B23), ("부산광역시", "총인구", "2024", B24)], "명"),
                                ensure_ascii=False),
    "context_markdown": md_table(["2023", "2024"], [("서울특별시", [f"{S23:,}", f"{S24:,}"]),
                                                   ("부산광역시", [f"{B23:,}", f"{B24:,}"])], "명"),
    "question_ko": "2024년 서울과 부산의 전년 대비 인구 증감률과 두 도시의 인구 배율을 설명하시오.",
    "required_list_text": "1) 2024년 서울 인구의 전년 대비 증감률(%)  2) 2024년 부산 인구의 전년 대비 증감률(%)  3) 2024년 서울/부산 인구 배율(배)",
    "gold_required": json.dumps([
        gold("서울 2024 증감률", "PCT", (S24 / S23 - 1) * 100, 1, "(v_2024/v_2023-1)*100"),
        gold("부산 2024 증감률", "PCT", (B24 / B23 - 1) * 100, 1, "(v_2024/v_2023-1)*100"),
        gold("서울/부산 배율 2024", "RATIO", S24 / B24, 2, "seoul_2024/busan_2024"),
    ], ensure_ascii=False),
    "gold_aux": json.dumps([
        gold("서울 절대 증감", "COUNT", S24 - S23, 0, "v_2024-v_2023"),
        gold("부산 절대 증감", "COUNT", B24 - B23, 0, "v_2024-v_2023"),
        gold("두 도시 합계 2024", "COUNT", S24 + B24, 0, "seoul_2024+busan_2024"),
    ], ensure_ascii=False),
    "rounding_rule_text": "증감률 소수 첫째 자리, 배율 소수 둘째 자리",
}
q_emp = {
    "qid": "q_emp_001", "table_key": "emp", "template_id": "E2", "org_id": "101", "tbl_id": "DT_1DA7012S",
    "itm_id": "T80", "obj_codes": json.dumps(["M", "F"]), "prd_se": "Y", "periods": json.dumps(["2023", "2024"]),
    "unit_nm": "%", "fetch_date": "2026-11-02", "lst_chn_de": "2025-02-15",
    "source_citation": "KOSIS, 통계청 경제활동인구조사", "table_title_ko": "성별 고용률",
    "context_cells": json.dumps(cells([("남자", "고용률", "2023", E["m23"]), ("남자", "고용률", "2024", E["m24"]),
                                       ("여자", "고용률", "2023", E["f23"]), ("여자", "고용률", "2024", E["f24"])], "%"),
                                ensure_ascii=False),
    "context_markdown": md_table(["2023", "2024"], [("남자", [E["m23"], E["m24"]]), ("여자", [E["f23"], E["f24"]])], "%"),
    "question_ko": "2024년 남녀 고용률 격차와 여성 고용률의 전년 대비 변화를 %p와 %로 각각 설명하시오.",
    "required_list_text": "1) 2024년 남녀 고용률 격차(%p)  2) 여성 고용률 전년 대비 변화(%p)  3) 여성 고용률 전년 대비 상대 변화율(%)",
    "gold_required": json.dumps([
        gold("2024 남녀 격차", "PP", E["m24"] - E["f24"], 1, "m_2024-f_2024"),
        gold("여성 전년 대비 %p 변화", "PP", E["f24"] - E["f23"], 1, "f_2024-f_2023"),
        gold("여성 전년 대비 상대 변화율", "PCT", (E["f24"] / E["f23"] - 1) * 100, 1, "(f_2024/f_2023-1)*100"),
    ], ensure_ascii=False),
    "gold_aux": json.dumps([
        gold("남성 전년 대비 %p 변화", "PP", E["m24"] - E["m23"], 1, "m_2024-m_2023"),
        gold("남성 전년 대비 상대 변화율", "PCT", (E["m24"] / E["m23"] - 1) * 100, 1, "(m_2024/m_2023-1)*100"),
        gold("2023 남녀 격차", "PP", E["m23"] - E["f23"], 1, "m_2023-f_2023"),
        gold("남녀 격차 변화(2024-2023)", "PP", (E["m24"] - E["f24"]) - (E["m23"] - E["f23"]), 1, "gap_2024-gap_2023"),
    ], ensure_ascii=False),
    "rounding_rule_text": "비율·격차 소수 첫째 자리, 비율의 차이는 %p",
}
q_cpi = {
    "qid": "q_cpi_001", "table_key": "cpi", "template_id": "C1", "org_id": "101", "tbl_id": "DT_1J20003",
    "itm_id": "T", "obj_codes": json.dumps(["01", "07"]), "prd_se": "Y", "periods": json.dumps(["2023", "2024"]),
    "unit_nm": "2020=100", "fetch_date": "2026-11-02", "lst_chn_de": "2025-01-03",
    "source_citation": "KOSIS, 통계청 소비자물가조사", "table_title_ko": "지출목적별 소비자물가지수",
    "context_cells": json.dumps(cells([("식료품·비주류음료", "지수", "2023", C["food23"]), ("식료품·비주류음료", "지수", "2024", C["food24"]),
                                       ("교통", "지수", "2023", C["tr23"]), ("교통", "지수", "2024", C["tr24"])], "2020=100"),
                                ensure_ascii=False),
    "context_markdown": md_table(["2023", "2024"], [("식료품·비주류음료", [C["food23"], C["food24"]]), ("교통", [C["tr23"], C["tr24"]])],
                                 "2020=100"),
    "question_ko": "2024년 식료품 물가의 전년 대비 상승률, 기준연도 대비 누적 상승폭, 교통과의 격차, 상승률 순위를 설명하시오.",
    "required_list_text": "1) 식료품 2024 전년 대비 상승률(%)  2) 식료품 2024 기준연도(2020) 대비 누적 상승폭(%)  3) 2024 식료품-교통 지수 격차(%p)  4) 식료품 상승률 순위(위)",
    "gold_required": json.dumps([
        gold("식료품 2024 상승률", "PCT", (C["food24"] / C["food23"] - 1) * 100, 1, "(idx_2024/idx_2023-1)*100"),
        gold("식료품 2024 누적 상승폭", "PCT", C["food24"] - 100, 1, "idx_2024-100"),
        gold("2024 식료품-교통 격차", "PP", C["food24"] - C["tr24"], 1, "food_2024-tr_2024"),
        gold("식료품 상승률 순위", "RANK", 1, 0, "rank desc"),
    ], ensure_ascii=False),
    "gold_aux": json.dumps([
        gold("교통 2024 상승률", "PCT", (C["tr24"] / C["tr23"] - 1) * 100, 1, "(idx_2024/idx_2023-1)*100"),
        gold("교통 2024 누적 상승폭", "PCT", C["tr24"] - 100, 1, "idx_2024-100"),
        gold("2023 식료품-교통 격차", "PP", C["food23"] - C["tr23"], 1, "food_2023-tr_2023"),
    ], ensure_ascii=False),
    "rounding_rule_text": "상승률·격차 소수 첫째 자리",
}
QUESTIONS = [q_pop, q_emp, q_cpi]
for q in QUESTIONS:
    q["gold_hash"] = hashlib.sha256((q["gold_required"] + q["context_cells"]).encode("utf-8")).hexdigest()

COLS = ["qid", "table_key", "template_id", "org_id", "tbl_id", "itm_id", "obj_codes", "prd_se", "periods", "unit_nm",
        "fetch_date", "lst_chn_de", "source_citation", "table_title_ko", "context_cells", "context_markdown",
        "question_ko", "required_list_text", "gold_required", "gold_aux", "rounding_rule_text", "gold_hash"]

# ---------------------------------------------------------------------------
# 응답 본문 (정답 서술 3변형 + 심은 오류)
# ---------------------------------------------------------------------------
NARR = {
    "q_pop_001": [
        f"2024년 서울특별시 인구는 {S24:,}명으로 전년 대비 0.6% 감소했다. 부산광역시 인구는 {B24:,}명으로 전년보다 0.8% 줄었다. "
        f"서울 인구는 부산의 2.86배 수준이다. 두 도시 모두 감소세가 이어졌다.",
        f"서울 인구는 2023년 {S23:,}명에서 2024년 약 933만 명으로 −0.6% 변동했다. 부산은 {B23:,}명에서 {B24:,}명으로 -0.8% 감소했다. "
        f"2024년 서울 인구는 부산의 2.86배다. 서울의 감소 폭은 54,212명이다.",
        f"2024년 서울 인구는 전년 대비 0.6% 감소한 {S24:,}명이며 부산은 0.8% 감소한 약 327만 명이다. "
        f"서울은 부산의 2.86배에 달한다. 두 도시 합계는 12,598,420명이다.",
    ],
    "q_emp_001": [
        "2024년 남성 고용률은 71.8%, 여성 고용률은 55.6%로 남녀 격차는 16.2%p다. 여성 고용률은 전년(54.1%) 대비 1.5%p 상승했다. "
        "이는 상대적으로 2.8% 증가한 수치다. 남성은 0.5%p 올랐다.",
        "남녀 고용률 격차는 2024년 16.2%p로 2023년 17.2%p보다 좁혀졌다. 여성 고용률은 54.1%에서 55.6%로 1.5%p 높아졌으며, "
        "상대 변화율은 2.8%다. 남성 고용률은 71.8로 집계됐다.",
        "여성 고용률은 2024년 55.6%로 전년 대비 1.5%p, 비율로는 2.8% 상승했다. 남성 고용률(71.8%)과의 격차는 16.2%p다. "
        "격차는 전년보다 1.0%p 줄었다.",
    ],
    "q_cpi_001": [
        "2024년 식료품·비주류음료 지수는 123.1로 전년 대비 4.1% 상승했다. 기준연도(2020=100) 대비 누적 상승폭은 23.1%다. "
        "교통 지수(113.2)와의 격차는 9.9%p다. 식료품 상승률은 두 품목 중 1위다.",
        "식료품 물가는 2023년 118.3에서 2024년 123.1로 4.1% 올랐다. 2020년 대비로는 23.1% 높은 수준이다. "
        "교통(1.1% 상승)과 비교하면 지수 격차는 9.9%p이며 식료품이 1위다.",
        "식료품 지수 상승률은 4.1%로 교통(1.1%)을 웃돌아 1위를 기록했다. 2020년 기준 누적 상승폭은 23.1%다. "
        "두 품목의 2024년 지수 격차는 9.9%p다.",
    ],
}
ANALYSIS_KO = {
    "q_pop_001": f"서울 2023 {S23:,} → 2024 {S24:,}, 부산 2023 {B23:,} → 2024 {B24:,}. 증감률 계산: 서울 -0.58%, 부산 -0.81%. 배율 2.857.",
    "q_emp_001": "남 71.3→71.8, 여 54.1→55.6. 격차 71.8-55.6=16.2%p. 여 변화 1.5%p, 상대 2.77%.",
    "q_cpi_001": "식료품 118.3→123.1 (+4.06%), 교통 112.0→113.2 (+1.07%). 누적 23.1, 격차 9.9.",
}
ANALYSIS_EN = {
    "q_pop_001": f"Seoul 2023 {S23:,} -> 2024 {S24:,}; Busan 2023 {B23:,} -> 2024 {B24:,}. Change: Seoul -0.58%, Busan -0.81%. Ratio 2.857.",
    "q_emp_001": "Male 71.3 -> 71.8, female 54.1 -> 55.6. Gap 71.8-55.6 = 16.2 percentage points. Female change 1.5 pp, relative 2.77%.",
    "q_cpi_001": "Food 118.3 -> 123.1 (+4.06%), transport 112.0 -> 113.2 (+1.07%). Cumulative 23.1, gap 9.9.",
}

PLANTED = {
    ("q_emp_001", "pivot", OPUS, 1): ("K4", lambda t: t.replace("남녀 격차는 16.2%p다", "남녀 격차는 16.2%다")),
    ("q_pop_001", "direct", HAIKU, 2): ("K2", lambda t: t.replace("−0.6% 변동", "-0.5% 변동")),
    ("q_pop_001", "direct", OPUS, 3): ("K1", lambda t: t + " 참고로 인천은 3,000,000명이다."),
    ("q_cpi_001", "direct", HAIKU, 1): ("K3", lambda t: t.replace("전년 대비 4.1% 상승", "전년 대비 5.3% 상승")),
    ("q_pop_001", "pivot", HAIKU, 2): ("K6", lambda t: t.replace("−0.6% 변동", "0.6% 증가")),
    ("q_emp_001", "direct", HAIKU, 3): ("FORMAT", None),
}


def build_response(qid, cond, model, rep):
    narr = NARR[qid][rep - 1]
    ana = ANALYSIS_EN[qid] if cond == "pivot" else ANALYSIS_KO[qid]
    plant = PLANTED.get((qid, cond, model, rep))
    if plant and plant[1] is not None:
        new = plant[1](narr)
        assert new != narr, (qid, cond, model, rep)
        narr = new
    if plant and plant[0] == "FORMAT":
        return f"===ANALYSIS===\n{ana}\n===NARRATIVE===\n{narr}\n"  # ===END=== 누락
    return f"===ANALYSIS===\n{ana}\n===NARRATIVE===\n{narr}\n===END==="


def main():
    qpath = os.path.join(HERE, "questions_fixture.csv")
    with open(qpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for q in QUESTIONS:
            w.writerow({k: q.get(k, "") for k in COLS})
    rpath = os.path.join(HERE, "results_fixture.jsonl")
    n = 0
    with open(rpath, "w", encoding="utf-8") as f:
        for q in QUESTIONS:
            for cond in ("direct", "pivot"):
                for model in (OPUS, HAIKU):
                    for rep in (1, 2, 3):
                        resp = build_response(q["qid"], cond, model, rep)
                        rec = {
                            "qid": q["qid"], "cond": cond, "model": model, "rep": rep,
                            "prompt_sha256": hashlib.sha256(f"{q['qid']}{cond}".encode()).hexdigest(),
                            "system_sha256": hashlib.sha256(b"system").hexdigest(),
                            "served_model": model, "response": resp,
                            "input_tokens": 900 + 10 * rep, "cache_creation_input_tokens": 0,
                            "output_tokens": (420 if model == OPUS else 380) + (30 if cond == "pivot" else 0) + rep,
                            "thinking_tokens": 0, "duration_api_ms": 5000 + 100 * rep, "stop_reason": "end_turn",
                            "is_error": False, "api_error_status": None, "uuid": f"fx-{n:04d}", "cli_version": "fixture",
                            "effort": "low", "ts_utc": "2026-11-02T00:00:00Z", "attempt": 1,
                        }
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        n += 1
    print(f"wrote {qpath} ({len(QUESTIONS)} questions) and {rpath} ({n} responses)")


if __name__ == "__main__":
    main()
