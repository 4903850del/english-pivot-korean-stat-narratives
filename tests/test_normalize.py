# -*- coding: utf-8 -*-
"""normalize.py 라운드트립 테스트 (SPEC §9: 30문장 이상 전부 통과).

실행:  cd experiment && PYTHONUTF8=1 py -3.11 -m pytest tests -q
pytest가 없으면:  PYTHONUTF8=1 py -3.11 tests/test_normalize.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from normalize import extract_claims, parse_sections  # noqa: E402


def _one(text):
    cs = extract_claims(text)
    assert len(cs) == 1, (text, cs)
    return cs[0]


def _tv(text):
    return [(c["type"], c["value"]) for c in extract_claims(text)]


# ---- 합성 수사 / 단위 배수 ------------------------------------------------
def test_01_cheon_baek_man():
    c = _one("서울 인구는 5천1백만 명이다.")
    assert c["type"] == "COUNT" and c["value"] == 51_000_000 and c["decimals"] == -6


def test_02_comma_man():
    c = _one("인구는 5,163만 명으로 집계됐다.")
    assert c["type"] == "COUNT" and c["value"] == 51_630_000 and c["decimals"] == -4


def test_03_approx_man():
    c = _one("약 939만 명이 거주한다.")
    assert c["type"] == "COUNT" and c["value"] == 9_390_000 and c["approx"] is True


def test_04_decimal_man():
    c = _one("인구는 939.4만 명이다.")
    assert c["value"] == 9_394_000 and c["decimals"] == -3 and abs(c["resolution"] - 1000) < 1e-6


def test_05_cheon_myeong_unit():
    c = _one("취업자는 13,630천 명이다.")
    assert c["type"] == "COUNT" and c["value"] == 13_630_000 and c["decimals"] == -3


def test_06_eok_cheon_man_won():
    c = _one("예산은 1억 2천만 원이다.")
    assert c["type"] == "COUNT" and c["value"] == 120_000_000 and c["decimals"] == -7


def test_07_man_cheon_digits():
    c = _one("총 1만 2천500명이 참여했다.")
    assert c["value"] == 12_500 and c["decimals"] == 0


def test_08_yeo_approx():
    c = _one("5천여 명이 늘었다.")
    assert c["type"] == "COUNT" and c["value"] == 5000 and c["approx"] is True and c["direction"] == 1


# ---- PCT / PP ------------------------------------------------------------
def test_09_pct_two_decimals():
    c = _one("고용률은 12.34%였다.")
    assert c["type"] == "PCT" and c["value"] == 12.34 and c["decimals"] == 2


def test_10_pp_symbol():
    c = _one("격차는 3.2%p였다.")
    assert c["type"] == "PP" and c["value"] == 3.2


def test_11_pp_spaced_korean():
    c = _one("격차는 3.2% 포인트였다.")
    assert c["type"] == "PP" and c["value"] == 3.2


def test_12_pp_percent_point_korean():
    c = _one("격차는 3.2 퍼센트포인트 벌어졌다.")
    assert c["type"] == "PP" and c["value"] == 3.2


def test_13_pp_latin_variants():
    assert _tv("상승률 3.2 p.p. 차이") == [("PP", 3.2)]
    assert _tv("차이는 12.3 pp 였다") == [("PP", 12.3)]
    assert _tv("차이는 1.5포인트였다") == [("PT", 1.5)]  # 단독 '포인트' 는 지수 포인트(PT)


def test_14_pct_korean_word():
    assert _tv("비중은 23.5 퍼센트였다.") == [("PCT", 23.5)]
    assert _tv("비중은 23.5프로였다.") == [("PCT", 23.5)]


# ---- 부호·방향 -----------------------------------------------------------
def test_15_unicode_minus():
    c = _one("증감률은 −0.6%였다.")
    assert c["type"] == "PCT" and c["value"] == 0.6 and c["sign"] == -1 and c["direction"] == -1


def test_16_ascii_minus_and_fullwidth():
    c = _one("전년 대비 -0.6%")
    assert c["sign"] == -1
    c = _one("전년 대비 －0.6%")  # NFKC → '-'
    assert c["sign"] == -1 and c["value"] == 0.6


def test_17_direction_word_decrease():
    c = _one("전년 대비 0.6% 감소했다.")
    assert c["direction"] == -1 and c["sign"] == 0


def test_18_direction_word_increase_variants():
    assert _one("3.1% 늘었다")["direction"] == 1
    assert _one("3.1% 상승했다")["direction"] == 1
    assert _one("3.1% 올라갔다")["direction"] == 1
    assert _one("3.1% 떨어졌다")["direction"] == -1
    assert _one("3.1% 줄었다")["direction"] == -1


def test_19_minus_korean_word():
    c = _one("마이너스 1.2%를 기록했다.")
    assert c["sign"] == -1 and c["value"] == 1.2 and c["direction"] == -1


def test_20_direction_window_stops_at_next_number():
    cs = extract_claims("실업률은 3.2%로 전년보다 0.6%p 낮아졌다.")
    assert [(c["type"], c["value"], c["direction"]) for c in cs] == [("PCT", 3.2, None), ("PP", 0.6, -1)]


# ---- 제외 규칙 -----------------------------------------------------------
def test_21_years_months_excluded():
    assert _tv("2023년에는 2024년 12월보다 적었다.") == []
    assert _tv("2024년 3분기") == []


def test_22_year_range_hyphen_excluded():
    assert _tv("2022-2024년 사이") == []


def test_23_counts_of_entities_excluded():
    assert _tv("17개 시도 가운데") == []
    assert _tv("3번 반복, 2회 조사, 5가지 항목, 4단계, 10곳") == []


def test_24_age_excluded():
    assert _tv("15~29세 고용률은 45.1%다.") == [("PCT", 45.1)]
    assert _tv("60세 이상 인구") == []


def test_25_base_year_index_excluded():
    assert _tv("지수는 2020=100 기준이다.") == []
    assert _tv("(2020＝100)") == []


def test_26_per_100_excluded():
    assert _tv("100명당") == []


def test_27_pure_korean_numeral_not_extracted():
    # 문서화된 한계: 순한글 수사는 추출하지 않는다.
    assert _tv("두 배로 늘었다.") == []
    assert _tv("세 번째로 높다.") == []


def test_28_citation_marker_removed():
    assert _tv("인구는 939만 명이다[1].") == [("COUNT", 9_390_000)]


# ---- RANK / RATIO / INDEX(UNK) ------------------------------------------
def test_29_rank():
    assert _tv("17개 시도 가운데 2위를 차지했다.") == [("RANK", 2.0)]
    assert _tv("3번째로 높다") == [("RANK", 3.0)]


def test_30_ratio():
    c = _one("서울은 부산의 1.05배다.")
    assert c["type"] == "RATIO" and c["value"] == 1.05 and c["decimals"] == 2


def test_31_unitless_is_unk():
    c = _one("고용률은 61.3으로 집계됐다.")
    assert c["type"] == "UNK" and c["value"] == 61.3 and c["decimals"] == 1


def test_32_index_value_unk():
    assert _tv("지수는 2020=100 기준 114.2이다.") == [("UNK", 114.2)]


# ---- NFKC / 범위 / 중복 --------------------------------------------------
def test_33_fullwidth_nfkc():
    c = _one("１２．３％로 상승했다.")
    assert c["type"] == "PCT" and c["value"] == 12.3 and c["direction"] is None  # '%로 + 증감어' 는 추세 서술(P1)
    c = _one("１２．３％ 상승했다.")
    assert c["type"] == "PCT" and c["value"] == 12.3 and c["direction"] == 1


def test_34_range_two_claims():
    assert _tv("증가율은 3~4%로 예상된다.") == [("PCT", 3.0), ("PCT", 4.0)]
    assert _tv("증가율은 3∼4%로 예상된다.") == [("PCT", 3.0), ("PCT", 4.0)]


def test_35_dedupe_same_type_value_approx():
    cs = extract_claims("인구는 939만 명이다. 즉 939만 명으로 1위다. 약 939만 명이다.")
    assert [(c["type"], c["value"], c["approx"]) for c in cs] == [
        ("COUNT", 9_390_000, False),
        ("RANK", 1.0, False),
        ("COUNT", 9_390_000, True),
    ]


def test_36_mixed_sentence():
    cs = extract_claims("남성 고용률은 71.8%, 여성은 55.6%로 격차는 16.2%p다.")
    assert [(c["type"], c["value"]) for c in cs] == [("PCT", 71.8), ("PCT", 55.6), ("PP", 16.2)]


def test_37_plain_counts():
    assert _tv("서울 9,386,034명, 부산 3,293,362명") == [("COUNT", 9_386_034), ("COUNT", 3_293_362)]


def test_38_sentence_field():
    cs = extract_claims("첫 문장이다. 인구는 939만 명이다. 끝.")
    assert cs[0]["sentence"] == "인구는 939만 명이다."


# ---- parse_sections ------------------------------------------------------
def test_39_parse_sections_ok():
    a, n, ok = parse_sections("===ANALYSIS===\nA 12.3%\n===NARRATIVE===\n서술 1.0%\n===END===")
    assert ok and a == "A 12.3%" and n == "서술 1.0%"


def test_40_parse_sections_missing_end_marker():
    # ===END=== 누락: NARRATIVE 마커 뒤만 서술로(ANALYSIS 숫자는 들어가지 않음), format_fail 유지
    a, n, ok = parse_sections("===ANALYSIS===\nA 12.3%\n===NARRATIVE===\n서술 1.0%")
    assert ok is False and a == "A 12.3%" and n == "서술 1.0%"
    # NARRATIVE 마커 자체가 없으면 전체를 서술로
    a, n, ok = parse_sections("===ANALYSIS===\nA 12.3%\n서술 1.0%")
    assert ok is False and a == "" and "12.3%" in n and "1.0%" in n and "===" not in n


# ---- 연령 표현 제외 (파일럿 거짓양성 수정) ----------------------------------
def test_41_age_decade_excluded():
    assert _tv("이는 청년기와 30대 사이에 격차가 달라짐을 보여줍니다.") == []
    assert _tv("60대 이상 고용률은 45.1%다.") == [("PCT", 45.1)]
    assert _tv("20~30대 고용률은 70.2%다.") == [("PCT", 70.2)]


def test_42_age_range_with_spaces_and_dashes_excluded():
    assert _tv("같은 해 40 - 49세의 고용률은 남자 90.8%, 여자 66.9%로 격차가 23.9%p였다.") == [("PCT", 90.8), ("PCT", 66.9), ("PP", 23.9)]
    assert _tv("15 – 19세") == []
    assert _tv("15 − 19세") == []
    assert _tv("15 ~ 19세") == []


def test_43_age_bound_words_excluded():
    assert _tv("60세이상 고용률은 45.1%") == [("PCT", 45.1)]
    assert _tv("65세 미만은 2배 높다") == [("RATIO", 2.0)]
    assert _tv("15세 이상 인구") == []


def test_44_ratio_and_counts_still_work_after_age_blanking():
    assert _tv("서울은 부산의 2배다") == [("RATIO", 2.0)]
    assert _tv("30대 여성 고용률 70.2%는 전년보다 1.5%p 높아") == [("PCT", 70.2), ("PP", 1.5)]
    cs = extract_claims("40 - 49세 고용률은 90.8%다.")
    assert cs[0]["sentence"] == "40 - 49세 고용률은 90.8%다."  # 문장 텍스트는 원문 유지


# ---- 마크다운 표·목록 번호·산술식·연도·작은 정수 제외 (본실행 중간 채점 거짓양성 수정) ----
from normalize import extract_claims_with_stats  # noqa: E402

TABLE = """| 순위 | 시도 | 2024 증감률(%) |
|---|---|---|
| 1 | 세종특별자치시 | 1.1 |
| 9 | **서울특별시** | **−0.6** |"""


def test_45_markdown_table_first_cell_and_header_excluded_values_kept():
    cs, st = extract_claims_with_stats(TABLE)
    assert [(c["type"], c["value"], c["sign"]) for c in cs] == [("UNK", 1.1, 0), ("UNK", 0.6, -1)]
    assert st["table_list"] == 3 and st["year"] == 0  # 헤더의 2024 + 첫 셀 1, 9
    assert "서울특별시" in cs[1]["sentence"]  # 표 행이 문장 → 행 라벨 귀속 가능


def test_46_list_markers_excluded():
    cs, st = extract_claims_with_stats("**4.** 인구는 12,614명 감소했다.\n5. 둘째\n(3) 셋째 2.5%\n- 2. 넷째\n7) 다섯째")
    assert [(c["type"], c["value"]) for c in cs] == [("COUNT", 12614.0), ("PCT", 2.5)]
    assert st["table_list"] == 5


def test_47_parenthetical_arithmetic_excluded_result_kept():
    cs, st = extract_claims_with_stats("(세종 계산: 390,685 − 386,525 = 4,160명, 4,160 ÷ 386,525 × 100 ≈ 1.08%)")
    assert [(c["type"], c["value"]) for c in cs] == [("PCT", 1.08)]
    assert st["arith"] == 6
    # 괄호 안이라도 연산자가 없으면 그대로 주장
    assert _tv("인구는 감소했다(−12,614명).") == [("COUNT", 12614.0)]


def test_48_bare_year_without_unit_excluded():
    cs, st = extract_claims_with_stats("2024 증감률은 −1.1%였다. 1999 지수는 다르다.")
    assert [(c["type"], c["value"]) for c in cs] == [("PCT", 1.1), ("UNK", 1999.0)]
    assert st["year"] == 1
    assert _tv("지수는 2024.5다") == [("UNK", 2024.5)]


def test_49_small_bare_integers_excluded():
    cs, st = extract_claims_with_stats("17개 시도 중 2위이며, 격차는 5 수준, 즉 5%p다. 지수는 21로 집계됐다.")
    assert [(c["type"], c["value"]) for c in cs] == [("RANK", 2.0), ("PP", 5.0), ("UNK", 21.0)]
    assert st["small_int"] == 1


# ---- 본실행 감사(audit) 거짓양성 패턴 ----------------------------------------
def test_50_P1_trend_phrase_particle_ro_no_direction():
    cs = extract_claims("전년 대비 상승률은 2023년 5.4%에서 2024년 2.0%로 낮아져 둔화되었습니다.")
    assert [(c["value"], c["direction"]) for c in cs] == [(5.4, None), (2.0, None)]
    assert _one("2024년에는 2.0%로 낮아졌다.")["direction"] is None
    assert _one("2024년에는 0.6%로 줄어들었다.")["direction"] is None
    # 조사 없이 바로 증감어 → 방향 유지
    assert _one("전년 대비 0.6% 감소했다.")["direction"] == -1


def test_51_P1_direction_window_stops_at_clause_boundary():
    assert _one("격차는 3.1%p이며, 이후 감소했다.")["direction"] is None
    assert _one("격차는 3.1%p이고 감소했다.")["direction"] is None
    assert _one("격차는 3.1%p, 감소했다.")["direction"] is None


def test_52_P2_superlative_not_direction():
    cs = extract_claims("통신이 0.3%로 가장 낮아 두 항목의 격차는 3.1%p입니다.")
    assert [(c["type"], c["value"], c["direction"]) for c in cs] == [("PCT", 0.3, None), ("PP", 3.1, None)]
    assert _one("교통이 1.4% 더 낮아")["direction"] is None
    assert _one("교통이 1.4% 제일 높아")["direction"] is None
    assert _one("교통이 1.4% 낮아")["direction"] == -1


def test_53_P3_bare_point_is_PT():
    c = _one("두 항목 간 지수 격차는 −14.76포인트입니다.")
    assert c["type"] == "PT" and c["value"] == 14.76 and c["sign"] == -1
    assert _tv("격차는 3.2% 포인트였다.") == [("PP", 3.2)]
    assert _tv("격차는 3.2%포인트, 1.5 퍼센트포인트, 2.1%p") == [("PP", 3.2), ("PP", 1.5), ("PP", 2.1)]
    assert _tv("1.5포인트 차이") == [("PT", 1.5)]


def test_54_P6_arithmetic_outside_parentheses():
    cs, st = extract_claims_with_stats("Sex ratio (males per 100 females) = Male / Female × 100")
    assert cs == [] and st["arith"] == 1
    assert _tv("- Chungnam 105.4 (1,096,128 / 1,040,446)") == [("UNK", 105.4)]
    assert _tv("성비는 99.1이며 남자 / 여자 × 100 으로 계산") == [("UNK", 99.1)]
    # 마크다운 굵게 '**' 는 연산자가 아님
    assert _tv("**0.6%** 감소") == [("PCT", 0.6)]
    assert _tv("인구는 **9,386,034명**이다") == [("COUNT", 9386034.0)]


if __name__ == "__main__":  # pytest 부재 시 단순 실행
    g = dict(globals())
    names = sorted(k for k in g if k.startswith("test_"))
    failed = 0
    for k in names:
        try:
            g[k]()
            print("PASS", k)
        except AssertionError as e:
            failed += 1
            print("FAIL", k, e)
    print(f"{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)
