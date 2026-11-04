# -*- coding: utf-8 -*-
"""score.py — 주장 채점 (SPEC §5).

입력:  results/results.jsonl, data/questions.csv
출력:  results/claims.jsonl, results/per_question.csv (응답 단위), results/per_cell_question.csv (문항×조건×모델, rep 합산/평균)

실행:  PYTHONUTF8=1 py -3.11 score.py [--results PATH] [--questions PATH] [--outdir DIR]

매칭 규칙(의사코드는 SPEC §5). 스펙 모호점 해소(README 겸 보고용):
  * context_cells 의 타입은 unit_nm 으로 결정: '%' → PCT, 명/원/가구/건 계열 → COUNT, 그 외(2020=100 등) → INDEX.
    unit_nm 에 천/만/억 배수가 있으면 셀 값을 기본 단위(명·원)로 환산해 gold 로 둔다(주장도 기본 단위로 정규화되므로).
  * UNK(단위 없음) 주장의 후보 순서: INDEX → PCT → PP → RATIO → COUNT (SPEC 의 4개 뒤에 COUNT 를 덧붙임: '9,386,034로' 처럼 단위 생략 셀 인용 대비).
  * 후보가 여럿이면 주장의 명시 부호/방향과 gold.sign 이 일치하는 후보를 먼저 고르고 그 안에서 가장 가까운 것을 택한다
    (−1.1% 가 +1.076 보다 −1.136 에 배정). 일치 후보가 없으면 가장 가까운 것 + K6(required PCT/PP 한정).
  * 허용오차 tol = 0.5 × 10^-d (d=주장의 유효 소수 자릿수; 합성 표기는 해상도). approx 면 max(tol, 0.01×|gold_exact|). RANK 는 정확 일치.
  * K6(방향 오류)는 (a) 주장에 명시 부호 또는 증감 어휘가 있고, (b) 매칭된 gold 가 gold_required 의 PCT/PP 이며, (c) gold.sign ≠ 0 이고
    반대 방향일 때만 적용한다. gold_aux·COUNT·셀에는 적용하지 않는다(증감 '폭'의 추세 서술과 충돌하므로).
  * 표시값 차이 폴백(v1.5): PP(또는 UNK) 주장이 모델이 서술 절에 직접 쓴 같은 유형 수치 쌍(한쪽은 같은 문장·직전 문장)의 차이와 허용오차 내이면 MATCH + flag display_diff
    (모델이 반올림된 표시값으로 격차 변화를 계산한 경우). gold_aux 의 basis:"display" 변형 항목은 독립 항목으로 그대로 처리.
  * 선택 열 target_cells(context_cells 와 같은 스키마): 있으면 target_cells ∪ gold_required ∪ gold_aux 를 먼저 매칭하고,
    비대상 context 셀에만 맞는 주장은 MATCH + flag off_target_cell (H·C 에 불포함, n_off_target_cell 로 집계). 열이 없으면 기존과 동일.
  * 선택 열 gold_pool(gold 항목 스키마 + row): 전체 표의 비대상 행 파생값(행별 증감률·절대차·성별 격차·idx-100·RANK 등).
    매칭 순서 = target_cells ∪ gold_required ∪ gold_aux → gold_pool → (display_diff) → 비대상 셀 → K4/K2(pool·비대상 셀 포함) → K3/K1.
    pool 에만 맞는 주장은 MATCH + flag pool (C 불포함, K6 미적용, n_pool 로 집계). 열이 없으면 기존과 동일.
  * pool 귀속 요건: 비대상 행의 pool 항목은 그 row 라벨(또는 별칭: 지역 앞 2글자·충북/전남 등, 연령 20대/60세 이상 등, CPI 첫 명사)이
    주장 문장(없으면 직전 문장)에 있어야 후보가 된다(공백 제거·대시 통일 후 비교). 값은 맞지만 귀속 실패로 K 코드로 간 주장은
    pool_unattributed=True, 응답별 n_pool_unattributed 로 집계(MISMATCH 로 끝난 건만). 대상 행의 pool 항목은 귀속 불요.
  * 커버리지 C 는 1:1 배정과 무관하게 '호환 타입의 어떤 주장이든 허용오차 내 등장' 기준(같은 값의 required 항목 둘을 주장 하나가 모두 커버).
  * H_cell / H_derived 는 주장의 유효 타입(UNK 는 매칭된 gold 의 타입) 기준: COUNT/INDEX → cell, PCT/PP/RATIO/RANK → derived. 매칭 실패한 UNK 는 둘 다 제외(H 에는 포함).
  * UNK 미매칭: gold_required 에 PCT/PP/RATIO/INDEX 가 있으면 K3, 없으면 K1.
  * claims=0 응답: H=1.0, zero_claims=True. ratio-of-sums 용으로 n_claims_eff=1, n_mismatch_eff=1 로 둔다.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional

import re

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from normalize import extract_claims, extract_claims_with_stats, parse_sections  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EPS = 1e-9
COUNT_FACTORS = (1e3, 1e-3, 1e4, 1e-4, 1e8, 1e-8)
UNK_ORDER = ("INDEX", "PCT", "PP", "RATIO", "COUNT")
PT_ORDER = ("INDEX", "PP", "COUNT")  # 단독 '포인트' = 지수(또는 성비) 포인트 격차: INDEX → PP → COUNT 순
CELL_TYPES = {"COUNT", "INDEX"}
DERIVED_TYPES = {"PCT", "PP", "PT", "RATIO", "RANK"}
# gold_pool 귀속용 행 별칭 (코디네이터 지정). 비교 전 공백 제거·대시 통일.
REGION_ALIAS = {"충청북도": ["충북"], "충청남도": ["충남"], "전라남도": ["전남"], "전북특별자치도": ["전북"], "전라북도": ["전북"],
                "경상북도": ["경북"], "경상남도": ["경남"],
                "전국": ["전국", "국내 전체", "전체 인구", "17개 시도 전체", "전체 시도", "시도 합계", "17개 시도 합계"]}
# 로마자 별칭(대소문자 무시)
REGION_ROMAN = {"서울": ["Seoul"], "부산": ["Busan"], "대구": ["Daegu"], "인천": ["Incheon"], "광주": ["Gwangju"], "대전": ["Daejeon"],
                "울산": ["Ulsan"], "세종": ["Sejong"], "경기": ["Gyeonggi"], "강원": ["Gangwon"], "제주": ["Jeju"],
                "충청북도": ["Chungbuk", "Chungcheongbuk"], "충청남도": ["Chungnam", "Chungcheongnam"],
                "전라남도": ["Jeonnam", "Jeollanam"], "전북특별자치도": ["Jeonbuk", "Jeollabuk"], "전라북도": ["Jeonbuk", "Jeollabuk"],
                "경상북도": ["Gyeongbuk", "Gyeongsangbuk"], "경상남도": ["Gyeongnam", "Gyeongsangnam"],
                "전국": ["Nationwide", "National", "Korea total", "Whole country", "All regions"]}
AGE_ROMAN = {"15 - 19세": ["ages 15-19", "15-19", "15 to 19", "aged 15-19"], "20 - 29세": ["ages 20-29", "20-29", "20 to 29", "20s"],
             "30 - 39세": ["ages 30-39", "30-39", "30 to 39", "30s"], "40 - 49세": ["ages 40-49", "40-49", "40 to 49", "40s"],
             "50 - 59세": ["ages 50-59", "50-59", "50 to 59", "50s"],
             "60세이상": ["60 and over", "60+", "60 and above", "aged 60", "60 or older", "60 years and over"],
             "계": ["all ages", "total", "overall"]}
CPI_ROMAN = {"식료품": ["Food and non-alcoholic", "Food & non-alcoholic", "Food"], "주류": ["Alcoholic beverages", "Alcohol", "tobacco"],
             "의류": ["Clothing", "footwear"], "주택": ["Housing", "water", "electricity", "fuel"],
             "가정용품": ["Furnishings", "household"], "보건": ["Health"], "교통": ["Transport"], "통신": ["Communication"],
             "오락": ["Recreation", "culture"], "교육": ["Education"], "음식": ["Restaurants", "hotels", "Food and accommodation", "accommodation"],
             "기타": ["Miscellaneous"], "총지수": ["All items", "Headline", "Total index", "All-items", "Overall index"]}
REGION_PREFIX2 = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "제주")
AGE_ALIAS = {"15 - 19세": ["15-19세", "15~19세", "10대 후반", "15세에서 19세"], "20 - 29세": ["20대", "20-29세", "20~29세"],
             "30 - 39세": ["30대", "30-39세", "30~39세"], "40 - 49세": ["40대", "40-49세", "40~49세"],
             "50 - 59세": ["50대", "50-59세", "50~59세"], "60세이상": ["60세 이상", "60대 이상", "60세이상", "고령층"],
             "계": ["전체", "계", "전 연령", "모든 연령"]}
CPI_NOUNS = ("식료품", "주류", "의류", "주택", "가정용품", "보건", "교통", "통신", "오락", "교육", "음식", "기타")
CPI_ALIAS = {"총지수": ["총지수", "전체 물가", "총 지수"]}


def _norm_label(s: str) -> str:
    s = str(s or "")
    s = s.replace("\u2013", "-").replace("\u2212", "-").replace("\uff0d", "-").replace("\u301c", "~").replace("\u223c", "~")
    return "".join(s.split()).lower()


def row_aliases(row: str) -> List[str]:
    row = str(row or "").strip()
    out = {row}
    out.update(REGION_ALIAS.get(row, []))
    if row[:2] in REGION_PREFIX2:
        out.add(row[:2])
    out.update(AGE_ALIAS.get(row, []))
    out.update(CPI_ALIAS.get(row, []))
    out.update(REGION_ROMAN.get(row, []))
    if row[:2] in REGION_ROMAN and row[:2] in REGION_PREFIX2:
        out.update(REGION_ROMAN[row[:2]])
    out.update(AGE_ROMAN.get(row, []))
    for n in CPI_NOUNS + ("총지수",):
        if row.startswith(n):
            out.add(n)
            out.update(CPI_ROMAN.get(n, []))
    return sorted({_norm_label(a) for a in out if a}, key=len, reverse=True)


ABS_NAME_RE = re.compile(r"절댓값\s*기준|절대값\s*기준")
ABS_CLAIM_RE = re.compile(r"(?i)절댓값|절대값|절대치|절대\s*격차|절대적|부호를\s*무시|부호\s*무시|크기\s*기준|크기\s*순|magnitude|absolute")


def abs_criterion_ok(claim: dict, g: dict) -> bool:
    """'절댓값 기준' aux/pool 항목은 주장 문장(또는 직전 문장)에 절댓값 기준이 명시된 경우에만 후보가 된다."""
    if not ABS_NAME_RE.search(str(g.get("name", ""))):
        return True
    for key in ("sentence", "prev_sentence"):
        if ABS_CLAIM_RE.search(str(claim.get(key, "") or "")):
            return True
    return False


def is_attributed(claim: dict, row: str) -> bool:
    """pool 항목의 row 라벨(또는 별칭)이 주장 문장(없으면 직전 문장)에 나타나는가."""
    aliases = row_aliases(row)
    if not aliases:
        return False
    for key in ("sentence", "prev_sentence"):
        txt = _norm_label(claim.get(key, ""))
        if txt and any(a in txt for a in aliases):
            return True
    return False


def is_rank_predecessor(claim: dict, row: str) -> bool:
    """순위 주장에서 'X에 이어 N위'의 X 행이면 True (v1.5). X 는 주어가 아니라 앞 순위 항목이다."""
    if claim.get("type") != "RANK":
        return False
    sent, span = str(claim.get("sentence", "") or ""), str(claim.get("span", "") or "")
    i = sent.find(span) if span else -1
    prefix = _norm_label(sent[:i] if i >= 0 else sent)
    j = prefix.rfind("에이어")
    if j < 0:
        return False
    head = re.sub(r"\([^)]*\)$", "", prefix[:j]).rstrip("'\"’”")
    return any(head.endswith(a) for a in row_aliases(row))


CHANGE_WORDS = ("증감", "변화", "격차", "차이", "증가", "감소", "변동", "change", "diff", "gap", "growth")


# ---------------------------------------------------------------------------
# 입력 로딩
# ---------------------------------------------------------------------------
def _loads(x):
    if x is None:
        return []
    if isinstance(x, (list, dict)):
        return x
    if isinstance(x, float) and math.isnan(x):
        return []
    s = str(x).strip()
    if not s:
        return []
    return json.loads(s)


def unit_multiplier(unit_nm: str) -> float:
    u = (unit_nm or "").replace(" ", "")
    if "천" in u:
        return 1e3
    if "만" in u:
        return 1e4
    if "억" in u:
        return 1e8
    return 1.0


def cell_type(unit_nm: str) -> str:
    u = (unit_nm or "").replace(" ", "")
    if "%" in u or "퍼센트" in u:
        return "PCT"
    if any(k in u for k in ("명", "원", "가구", "건", "호", "세대", "인")):
        return "COUNT"
    return "INDEX"


def _decimals_of(x) -> int:
    s = str(x).strip().replace(",", "")
    if "." in s:
        return len(s.split(".")[1].rstrip("0"))
    return 0


def load_questions(path: str) -> Dict[str, dict]:
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    out = {}
    for _, r in df.iterrows():
        q = dict(r)
        q["context_cells"] = _loads(q.get("context_cells"))
        q["gold_required"] = _loads(q.get("gold_required"))
        q["gold_aux"] = _loads(q.get("gold_aux"))
        q["target_cells"] = _loads(q.get("target_cells")) if "target_cells" in q else []
        q["gold_pool"] = _loads(q.get("gold_pool")) if "gold_pool" in q else []
        out[str(q["qid"])] = q
    return out


def _display_value(g) -> Optional[float]:
    d = g.get("display")
    if d is None:
        return None
    t = str(d).replace(",", "").replace("%p", "").replace("%", "").replace("배", "").replace("위", "").strip()
    t = t.replace("−", "-").replace("－", "-")
    try:
        return float(t)
    except ValueError:
        return None


def _cell_key(c) -> tuple:
    return tuple(str(c.get(k, "")).strip() for k in ("row_label", "col_label", "period"))


def build_gold(q: dict) -> List[dict]:
    """gold 목록. kind: cell(대상 셀) | offcell(target_cells 가 있을 때 비대상 context 셀) | required | aux."""
    golds = []
    ctype = cell_type(q.get("unit_nm", ""))
    mult = unit_multiplier(q.get("unit_nm", ""))
    targets = {_cell_key(c) for c in (q.get("target_cells") or [])}
    target_rows = {str(c.get("row_label", "")).strip() for c in (q.get("target_cells") or [])}
    seen_cells = set()
    for c in list(q.get("target_cells") or []) + list(q["context_cells"]):
        try:
            v = float(c.get("value"))
        except (TypeError, ValueError):
            continue
        key = _cell_key(c)
        if key in seen_cells:
            continue
        seen_cells.add(key)
        name = "cell:" + "|".join(key)
        golds.append(
            {
                "name": name,
                "type": ctype,
                "exact": v * mult,
                "display": c.get("dt_raw"),
                "decimals": _decimals_of(c.get("dt_raw", c.get("value"))),
                "sign": 1 if v > 0 else (-1 if v < 0 else 0),
                "kind": "cell" if (not targets or key in targets) else "offcell",
                "formula": "",
                "is_change": False,
            }
        )
    for kind, key in (("required", "gold_required"), ("aux", "gold_aux"), ("pool", "gold_pool")):
        for g in q.get(key) or []:
            try:
                exact = float(g["exact"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(g.get("name", ""))
            formula = str(g.get("formula", ""))
            sign = g.get("sign")
            try:
                sign = int(sign)
            except (TypeError, ValueError):
                sign = 1 if exact > 0 else (-1 if exact < 0 else 0)
            golds.append(
                {
                    "name": (f"pool[{g.get('row')}]:" if kind == "pool" else "") + name,
                    "row": g.get("row"),
                    "needs_attr": kind == "pool" and str(g.get("row", "")).strip() not in target_rows,
                    "type": str(g.get("type", "UNK")).upper(),
                    "exact": exact,
                    "display": g.get("display"),
                    "basis": g.get("basis", "exact"),
                    "decimals": int(g.get("decimals", 1) or 0),
                    "sign": sign,
                    "kind": kind,
                    "formula": formula,
                    "is_change": ("-" in formula) or any(w in name.lower() for w in CHANGE_WORDS),
                }
            )
    return golds


# ---------------------------------------------------------------------------
# 매칭
# ---------------------------------------------------------------------------
def tolerance(claim: dict, gold: dict) -> float:
    if claim["type"] == "RANK" or gold["type"] == "RANK":
        return 0.0
    tol = 0.5 * claim["resolution"]
    if claim.get("approx"):
        tol = max(tol, 0.01 * abs(gold["exact"]))
    return tol


def within(claim_val: float, gold: dict, tol: float) -> bool:
    return abs(abs(claim_val) - abs(gold["exact"])) <= tol + EPS


def candidate_types(ctype: str):
    if ctype == "UNK":
        return UNK_ORDER
    if ctype == "PT":
        return PT_ORDER
    return (ctype,)


def _ref_kind_of(kind: Optional[str]) -> str:
    if kind in ("required", "aux", "pool"):
        return kind
    if kind in ("cell", "offcell"):
        return "cell"
    return "none"


def match_claim(claim: dict, golds: List[dict]) -> dict:
    """verdict, code, matched_gold(name/kind/type), flags, ref_kind 반환.
    ref_kind = 매칭된 gold 의 종류(required/aux/pool/cell), MISMATCH 면 비교 대상이 된 같은 타입의 최근접 gold 종류, 없으면 none."""
    res = _match_claim_core(claim, golds)
    if res["matched_gold_kind"] == "display_diff":
        res["ref_kind"] = res.get("display_diff_kind", "aux")
    elif res["matched_gold_kind"] is not None:
        res["ref_kind"] = _ref_kind_of(res["matched_gold_kind"])
    else:
        # K1/K3: 비교 대상(귀속된 후보) 중 같은 타입(UNK/PT 는 폴백 순서) 최근접 gold
        cands = [g for g in golds if g["kind"] != "pool" or not g.get("needs_attr") or is_attributed(claim, g.get("row"))]
        cands = [g for g in cands if abs_criterion_ok(claim, g)]
        best = None
        for t in candidate_types(claim["type"]):
            same = [g for g in cands if g["type"] == t]
            if same:
                best = min(same, key=lambda g: abs(abs(claim["value"]) - abs(g["exact"])))
                break
        res["ref_kind"] = _ref_kind_of(best["kind"]) if best else "none"
    return res


def attach_neighbors(claims: List[dict]) -> None:
    """표시값 차이 폴백용: 각 주장에 같은 문장·직전 문장에 쓰인 수치를 연결한다.
    claims 는 응답 안에서 중복 제거되어 있으므로 해당 두 문장을 다시 추출해 쓴다(앞서 나온 같은 값도 피연산자로 인정)."""
    cache = {}
    for c in claims:
        key = (c.get("prev_sentence") or "", c.get("sentence") or "")
        if key not in cache:
            cache[key] = extract_claims((key[0] + " " + key[1]).strip())
        c["_neighbors"] = [o for o in cache[key] if not (o.get("span") == c.get("span") and o.get("value") == c.get("value"))]
        c["_all_written"] = [o for o in claims if o is not c]


def _match_claim_core(claim: dict, golds: List[dict]) -> dict:
    """verdict, code, matched_gold(name/kind/type), flags 반환."""
    res = {"verdict": "MISMATCH", "code": None, "matched_gold_name": None, "matched_gold_kind": None,
           "matched_gold_type": None, "precision_loss": False, "unit_missing": False, "display_diff": False,
           "off_target_cell": False, "pool": False, "pool_row": None, "pool_unattributed": False, "note": "",
           "ref_kind": "none"}
    primary = [g for g in golds if g["kind"] in ("cell", "required", "aux") and abs_criterion_ok(claim, g)]
    pool_all = [g for g in golds if g["kind"] == "pool" and abs_criterion_ok(claim, g)]
    # 귀속 요건: 비대상 행의 pool 항목은 row 라벨(별칭)이 주장 문장/직전 문장에 있어야 후보가 된다
    pool = [g for g in pool_all if (not g.get("needs_attr") or is_attributed(claim, g.get("row")))
            and not (g.get("row") and is_rank_predecessor(claim, g.get("row")))]
    unattributed = [g for g in pool_all if g not in pool]
    offcells = [g for g in golds if g["kind"] == "offcell"]
    golds = primary + pool + offcells  # K4/K2 재시도에서도 비귀속 pool 항목은 제외
    ctype = claim["type"]
    v = claim["value"]

    cdir = claim.get("direction")

    def closest(cands):
        """cands: [(gold, scaled_claim_val, tol)] → 허용오차 내 후보 중, 주장의 명시 부호/방향과 gold.sign 이 일치하는
        후보를 우선하고(없으면 전체), 그 안에서 |차이|가 가장 작은 gold. 없으면 None."""
        inside = []
        for g, cv, tol in cands:
            diff = abs(abs(cv) - abs(g["exact"]))
            if diff <= tol + EPS:
                inside.append((g, diff))
        if not inside:
            return None
        if cdir is not None:
            agree = [x for x in inside if x[0]["sign"] == cdir]
            if agree:
                inside = agree
        return min(inside, key=lambda x: x[1])[0]

    def hit(g, code, note):
        res.update(code=code, matched_gold_name=g["name"], matched_gold_kind=g["kind"], matched_gold_type=g["type"], note=note)
        return res

    # 1) 같은 타입(UNK 는 폴백 순서). 같은 타입 안에서는 가장 가까운 gold 를 택한다. (비대상 셀 제외)
    for t in candidate_types(ctype):
        g = closest([(g, v, tolerance(claim, g)) for g in primary if g["type"] == t])
        if g is not None:
            res.update(matched_gold_name=g["name"], matched_gold_kind=g["kind"], matched_gold_type=g["type"])
            res["unit_missing"] = ctype == "UNK"
            res["precision_loss"] = claim["resolution"] > (10.0 ** (-g["decimals"])) * (1 + 1e-9)
            # K6 방향 오류: 명시 부호/증감 어휘가 있고, gold 가 gold_required 의 PCT/PP(변화율·격차)이며 부호가 반대일 때만.
            # gold_aux·COUNT 에는 적용하지 않는다(증감 '폭'의 추세를 서술하는 문장과 충돌).
            d = claim.get("direction")
            if (d is not None and g["kind"] == "required" and g["type"] in ("PCT", "PP")
                    and g["sign"] != 0 and d != g["sign"]):
                res.update(verdict="MISMATCH", code="K6", note="direction_error")
            else:
                res.update(verdict="MATCH", code=None)
            return res

    # 1a) gold_pool (전체 표의 비대상 행 파생값): MATCH + flag pool (C 에 불포함, K6 미적용)
    if pool:
        for t in candidate_types(ctype):
            g = closest([(g, v, tolerance(claim, g)) for g in pool if g["type"] == t])
            if g is not None:
                res.update(verdict="MATCH", code=None, pool=True, pool_row=g.get("row"), matched_gold_name=g["name"],
                           matched_gold_kind="pool", matched_gold_type=g["type"], note="pool")
                res["unit_missing"] = ctype == "UNK"
                res["precision_loss"] = claim["resolution"] > (10.0 ** (-g["decimals"])) * (1 + 1e-9)
                return res
    # 귀속된 pool 매칭이 없을 때만: 값은 맞지만 행 귀속 실패한 pool 항목이 있으면 플래그(아래 K 코드로 진행)
    if unattributed:
        for t in candidate_types(ctype):
            if closest([(g, v, tolerance(claim, g)) for g in unattributed if g["type"] == t]) is not None:
                res["pool_unattributed"] = True
                break

    # 1b) 표시값 차이 폴백(v1.5): 모델이 서술 절에 직접 쓴 같은 유형 수치 쌍(한쪽은 같은 문장·직전 문장)의 차이와 일치하면 MATCH.
    # 정답 값끼리의 차이는 쓰지 않는다(문장과 무관한 값의 우연 일치 방지).
    dd_plan = {"PP": ("PCT", "PP"), "UNK": ("PCT", "PP"), "COUNT": ("COUNT", "UNK"), "PT": ("INDEX", "PT", "COUNT", "PP", "UNK")}
    if ctype in dd_plan:
        def _signed(o):
            sg = o.get("sign") or o.get("direction") or 1
            return o["value"] * (-1 if sg < 0 else 1)
        tol = 0.5 * claim["resolution"]
        for t in dd_plan[ctype]:
            near = [_signed(o) for o in claim.get("_neighbors", []) if o.get("type") == t]
            anyw = [_signed(o) for o in claim.get("_all_written", []) if o.get("type") == t]
            best = None
            for a in near:          # 한 피연산자는 같은 문장·직전 문장, 다른 피연산자는 같은 서술 절 어디든
                for b in near + anyw:
                    if a == b:
                        continue
                    err = abs(abs(v) - abs(a - b))
                    if err <= tol + EPS and (best is None or err < best[0]):
                        best = (err, a, b)
            if best is not None:
                res.update(verdict="MATCH", code=None, display_diff=True, matched_gold_type=t,
                           matched_gold_kind="display_diff", matched_gold_name=f"display_diff:written {best[1]:g} - {best[2]:g}",
                           note="display_diff", display_diff_kind="aux")
                return res

    # 지수 포인트 격차는 표의 두 셀 값 차이도 허용한다(셀은 표에 있는 값이므로 정답 파생값의 우연 일치 문제가 없다).
    if ctype == "PT":
        cand = [(g, _display_value(g)) for g in primary if g["type"] == "INDEX" and g["kind"] == "cell"]
        cand = [(g, dv) for g, dv in cand if dv is not None]
        tol = 0.5 * claim["resolution"]
        for i in range(len(cand)):
            for j in range(i + 1, len(cand)):
                if abs(abs(v) - abs(cand[i][1] - cand[j][1])) <= tol + EPS:
                    res.update(verdict="MATCH", code=None, display_diff=True, matched_gold_type="INDEX",
                               matched_gold_kind="display_diff", matched_gold_name=f"display_diff:{cand[i][0]['name']} - {cand[j][0]['name']}",
                               note="display_diff", display_diff_kind="aux")
                    return res

    # 1c) 비대상 context 셀 (target_cells 가 있을 때만 존재): MATCH + flag off_target_cell
    if offcells:
        for t in candidate_types(ctype):
            g = closest([(g, v, tolerance(claim, g)) for g in offcells if g["type"] == t])
            if g is not None:
                res.update(verdict="MATCH", code=None, off_target_cell=True, matched_gold_name=g["name"],
                           matched_gold_kind="offcell", matched_gold_type=g["type"], note="off_target_cell")
                res["unit_missing"] = ctype == "UNK"
                return res

    # 2) 교차 타입: %↔%p
    if ctype in ("PCT", "PP"):
        other = "PP" if ctype == "PCT" else "PCT"
        g = closest([(g, v, tolerance(claim, g)) for g in golds if g["type"] == other])
        if g is not None:
            return hit(g, "K4", "unit_error:%<->%p")
    # 2') 교차 단위: COUNT × 10^{±3,±4,±8}
    if ctype in ("COUNT", "UNK"):
        for f in COUNT_FACTORS:
            # 배수 재시도 허용오차: 해상도 기반 오차만 f 배, approx 1% 오차는 gold 단위이므로 그대로
            g = closest([(g, v * f, max(0.5 * claim["resolution"] * f, 0.01 * abs(g["exact"]) if claim.get("approx") else 0.0))
                         for g in golds if g["type"] == "COUNT"])
            if g is not None:
                return hit(g, "K4", f"unit_error:x{f:g}")

    # 3) 같은 타입 10×tol 이내 → 반올림/자릿수 오류
    for t in candidate_types(ctype):
        g = closest([(g, v, 10 * tolerance(claim, g)) for g in golds if g["type"] == t and tolerance(claim, g) > 0])
        if g is not None:
            return hit(g, "K2", "rounding_or_digit")

    # 4) 계산 오류 vs 날조
    req_types = {g["type"] for g in golds if g["kind"] == "required"}
    if ctype in ("PCT", "PP", "RATIO") and ctype in req_types:
        res.update(code="K3", note="calc_error")
    elif ctype == "RANK" and any(g["type"] == "RANK" for g in golds):
        res.update(code="K3", note="calc_error(rank/ordering)")
    elif ctype == "PT" and req_types & {"INDEX", "PP"}:
        res.update(code="K3", note="calc_error(index_points)")
    elif ctype == "UNK" and req_types & {"PCT", "PP", "RATIO", "INDEX"}:
        res.update(code="K3", note="calc_error(unitless)")
    else:
        res.update(code="K1", note="fabricated")
    return res


def effective_type(claim: dict, m: dict) -> str:
    if claim["type"] != "UNK":
        return claim["type"]
    return m.get("matched_gold_type") or "UNK"


# ---------------------------------------------------------------------------
# 응답 단위 채점
# ---------------------------------------------------------------------------
def score_response(row: dict, q: dict, golds: List[dict]) -> (dict, List[dict]):
    text = row.get("response") or ""
    analysis, narrative, ok = parse_sections(text)
    claims, xstats = extract_claims_with_stats(narrative)
    out_claims = []
    n = len(claims)
    n_mis = n_unit = 0
    cell_n = cell_mis = der_n = der_mis = 0
    covered = set()
    n_off = n_ddiff = n_pool = n_pool_unattr = 0
    req_n = req_mis = oth_n = oth_mis = 0
    attach_neighbors(claims)
    for i, c in enumerate(claims, 1):
        m = match_claim(c, golds)
        et = effective_type(c, m)
        mismatch = m["verdict"] == "MISMATCH"
        n_mis += mismatch
        n_unit += m["code"] in ("K2", "K4")
        if et in CELL_TYPES:
            cell_n += 1
            cell_mis += mismatch
        elif et in DERIVED_TYPES:
            der_n += 1
            der_mis += mismatch
        if m["matched_gold_kind"] == "required" and m["code"] in (None, "K6"):
            covered.add(m["matched_gold_name"])
        n_off += m["off_target_cell"]
        n_ddiff += m["display_diff"]
        n_pool += m["pool"]
        n_pool_unattr += (m["pool_unattributed"] and mismatch)
        if m["ref_kind"] == "required":
            req_n += 1
            req_mis += mismatch
        else:
            oth_n += 1
            oth_mis += mismatch
        out_claims.append(
            {
                "qid": row["qid"], "cond": row["cond"], "model": row["model"], "rep": int(row["rep"]),
                "claim_id": f"{row['qid']}|{row['cond']}|{row['model']}|{row['rep']}|{i:02d}",
                "span": c["span"], "type": c["type"], "effective_type": et, "value": c["value"],
                "decimals": c["decimals"], "approx": bool(c["approx"]), "sign": c["sign"],
                "direction": c["direction"], "verdict": m["verdict"], "code": m["code"],
                "matched_gold_name": m["matched_gold_name"], "matched_gold_kind": m["matched_gold_kind"],
                "precision_loss": m["precision_loss"], "unit_missing": m["unit_missing"],
                "display_diff": m["display_diff"], "off_target_cell": m["off_target_cell"], "pool": m["pool"],
                "pool_row": m["pool_row"], "pool_unattributed": m["pool_unattributed"], "ref_kind": m["ref_kind"],
                "note": m["note"],
                "sentence": c["sentence"],
            }
        )
    # 커버리지 C: 1:1 배정과 무관하게, 호환 타입의 어떤 주장이든 허용오차 내이면 그 required 항목은 커버됨
    # (같은 값의 required 항목이 둘일 때 주장 하나로 둘 다 커버). H 배정 로직은 그대로.
    for g in golds:
        if g["kind"] != "required" or g["name"] in covered:
            continue
        for c in claims:
            if g["type"] in candidate_types(c["type"]) and within(c["value"], g, tolerance(c, g)):
                covered.add(g["name"])
                break
    n_req = len(q["gold_required"])
    rec = {
        "qid": row["qid"], "cond": row["cond"], "model": row["model"], "rep": int(row["rep"]),
        "table_key": q.get("table_key", ""), "template_id": q.get("template_id", ""),
        "format_fail": not ok, "zero_claims": n == 0,
        "n_claims": n, "n_mismatch": n_mis, "n_unitdigit": n_unit,
        "n_claims_eff": n if n > 0 else 1, "n_mismatch_eff": n_mis if n > 0 else 1,
        "n_cell_claims": cell_n, "n_cell_mismatch": cell_mis,
        "n_derived_claims": der_n, "n_derived_mismatch": der_mis,
        "n_required": n_req, "n_required_matched": len(covered),
        "n_off_target_cell": n_off, "n_display_diff": n_ddiff, "n_pool": n_pool, "n_pool_unattributed": n_pool_unattr,
        "n_req_claims": req_n, "n_req_mismatch": req_mis, "n_other_claims": oth_n, "n_other_mismatch": oth_mis,
        "H_required": (req_mis / req_n) if req_n > 0 else float("nan"),
        "H_other": (oth_mis / oth_n) if oth_n > 0 else float("nan"),
        "n_excluded_table_list": xstats["table_list"], "n_excluded_arith": xstats["arith"],
        "n_excluded_year": xstats["year"], "n_excluded_small_int": xstats["small_int"],
        "H": (n_mis / n) if n > 0 else 1.0,
        "U": (n_unit / n) if n > 0 else 0.0,
        "C": (len(covered) / n_req) if n_req > 0 else float("nan"),
        "H_cell": (cell_mis / cell_n) if cell_n > 0 else float("nan"),
        "H_derived": (der_mis / der_n) if der_n > 0 else float("nan"),
        "n_K1": sum(c["code"] == "K1" for c in out_claims),
        "n_K2": sum(c["code"] == "K2" for c in out_claims),
        "n_K3": sum(c["code"] == "K3" for c in out_claims),
        "n_K4": sum(c["code"] == "K4" for c in out_claims),
        "n_K6": sum(c["code"] == "K6" for c in out_claims),
        "narrative_chars": len(narrative.strip()),
        "analysis_chars": len(analysis.strip()),
        "output_tokens": row.get("output_tokens"),
        "thinking_tokens": row.get("thinking_tokens"),
        "input_tokens": row.get("input_tokens"),
        "duration_api_ms": row.get("duration_api_ms"),
        "is_error": bool(row.get("is_error", False)),
        "served_model": row.get("served_model", ""),
    }
    return rec, out_claims


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def run(results_path: str, questions_path: str, outdir: str) -> pd.DataFrame:
    os.makedirs(outdir, exist_ok=True)
    questions = load_questions(questions_path)
    golds_by_q = {qid: build_gold(q) for qid, q in questions.items()}
    rows = []
    with open(results_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    # 같은 키가 중복이면 마지막(최신 attempt)만 사용
    latest = {}
    for r in rows:
        latest[(str(r["qid"]), r["cond"], r["model"], int(r["rep"]))] = r
    per_q = []
    all_claims = []
    missing_q = set()
    for key in sorted(latest):
        r = latest[key]
        qid = str(r["qid"])
        if qid not in questions:
            missing_q.add(qid)
            continue
        r["qid"] = qid
        rec, cl = score_response(r, questions[qid], golds_by_q[qid])
        per_q.append(rec)
        all_claims.extend(cl)
    if missing_q:
        print(f"[score] WARNING: {len(missing_q)} qid(s) in results not in questions.csv: {sorted(missing_q)[:10]}")

    with open(os.path.join(outdir, "claims.jsonl"), "w", encoding="utf-8") as f:
        for c in all_claims:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    pq = pd.DataFrame(per_q)
    pq.to_csv(os.path.join(outdir, "per_question.csv"), index=False, encoding="utf-8-sig")

    # 문항×조건×모델 (rep 합산/평균)
    sum_cols = ["n_claims", "n_mismatch", "n_unitdigit", "n_claims_eff", "n_mismatch_eff",
                "n_cell_claims", "n_cell_mismatch", "n_derived_claims", "n_derived_mismatch",
                "n_required", "n_required_matched", "n_off_target_cell", "n_display_diff", "n_pool", "n_pool_unattributed",
                "n_excluded_table_list", "n_excluded_arith", "n_excluded_year", "n_excluded_small_int",
                "n_req_claims", "n_req_mismatch", "n_other_claims", "n_other_mismatch",
                "n_K1", "n_K2", "n_K3", "n_K4", "n_K6"]
    mean_cols = ["H", "U", "C", "H_cell", "H_derived", "H_required", "H_other", "narrative_chars", "analysis_chars"]
    for c in ("output_tokens", "thinking_tokens", "input_tokens"):
        pq[c] = pd.to_numeric(pq[c], errors="coerce")
        mean_cols.append(c)
    g = pq.groupby(["qid", "cond", "model"], sort=True)
    agg = g[sum_cols].sum()
    agg = agg.join(g[mean_cols].mean().add_suffix("_mean"))
    agg["H_sd_rep"] = g["H"].std(ddof=1)
    agg["n_reps"] = g.size()
    agg["n_zero_claims"] = g["zero_claims"].sum()
    agg["n_format_fail"] = g["format_fail"].sum()
    agg["H_ratio"] = agg["n_mismatch_eff"] / agg["n_claims_eff"]
    agg["U_ratio"] = agg["n_unitdigit"] / agg["n_claims_eff"]
    agg["C_ratio"] = agg["n_required_matched"] / agg["n_required"].replace(0, float("nan"))
    agg["H_cell_ratio"] = agg["n_cell_mismatch"] / agg["n_cell_claims"].replace(0, float("nan"))
    agg["H_derived_ratio"] = agg["n_derived_mismatch"] / agg["n_derived_claims"].replace(0, float("nan"))
    agg["H_required_ratio"] = agg["n_req_mismatch"] / agg["n_req_claims"].replace(0, float("nan"))
    agg["H_other_ratio"] = agg["n_other_mismatch"] / agg["n_other_claims"].replace(0, float("nan"))
    agg = agg.reset_index()
    tk = pq.drop_duplicates(["qid"])[["qid", "table_key", "template_id"]]
    agg = agg.merge(tk, on="qid", how="left")
    agg.to_csv(os.path.join(outdir, "per_cell_question.csv"), index=False, encoding="utf-8-sig")

    # 요약 출력
    print(f"[score] responses={len(pq)} claims={len(all_claims)} format_fail={int(pq['format_fail'].sum())} "
          f"zero_claims={int(pq['zero_claims'].sum())}")
    if len(pq):
        summ = pq.groupby(["cond", "model"]).agg(n=("H", "size"), H=("H", "mean"), U=("U", "mean"), C=("C", "mean"),
                                                 claims=("n_claims", "mean"))
        print(summ.round(4).to_string())
        codes = pd.Series([c["code"] for c in all_claims if c["code"]]).value_counts()
        print("[score] code counts:", dict(codes))
    return pq


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(HERE, "results", "results.jsonl"))
    ap.add_argument("--questions", default=os.path.join(HERE, "data", "questions.csv"))
    ap.add_argument("--outdir", default=os.path.join(HERE, "results"))
    a = ap.parse_args(argv)
    run(a.results, a.questions, a.outdir)


if __name__ == "__main__":
    main()
