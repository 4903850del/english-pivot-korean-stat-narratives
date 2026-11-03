# -*- coding: utf-8 -*-
"""템플릿(P1~P5, E1~E5, C1~C5) → questions.csv (50문항) + questions_preview.md   [v2: 난이도 상향판]

실행: py -3.11 data/build_questions.py [--data-dir data] [--out data/questions.csv] [--preview data/questions_preview.md]

규칙(SPEC §2, 검증보고서 1-8/1-9, analysis_plan_v1 §6 분기 적용):
- 프롬프트에는 원 셀만. v2 부터 context 는 표의 '관련 슬라이스 전체'(pop 54/68, emp 63, cpi 39 셀).
  * context_cells  = 프롬프트에 보이는 셀 전부
  * target_cells   = 핵심값·극값 계산에 실제 쓰인 셀만(⊂ context_cells). gold_aux 자동 확장은 target_cells 에만 적용.
- 모든 문항에 표 전체를 훑어야 하는 값 1~2개 추가(순위 RANK: 동률 min, 방향은 질문에 명시 / 극값). gold_required ≤ 6.
- 파생값(gold)은 context_cells.value 만으로 계산. 반올림 decimal.ROUND_HALF_UP. exact 미반올림, display 반올림.
- 증감률·비중·%p 격차 1자리, 배율 2자리, 지수 격차 2자리, 인원 0자리(성비는 1자리), 순위 정수.
- smblChk 부호 셀·비숫자 셀은 슬라이스에서 행 단위로 제외(순위 모집단도 같이 줄어들며 질문 문구의 개수에 반영).
- 지역·연령·항목 선택은 random.Random(42) 로 결정적.
- gold_aux = 템플릿 지정값(2~4) + expand_aux(target_cells·gold_required 쌍) ≤ 12. 쌍 차이값은 basis "exact"/"display" 두 변형.
- gold_hash = sha256(json(gold_required, context_cells)) — gold_aux·target_cells·gold_pool 은 해시에 포함하지 않음.
- gold_pool(v2.1 추가 열): context 의 모든 행에 대한 정당한 파생값 전수(build_gold_pool 참조), 상한 없음, `row` 필드 포함.
"""
import argparse
import hashlib
import json
import random
import sys
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
getcontext().prec = 34
SEED = 42
YEARS = ["2022", "2023", "2024"]
AUX_CAP = 12
MAX_MD_CHARS = 2500

# (table_key, template_id, 문항 수) — 합계 50 = pop 17 / emp 17 / cpi 16
TEMPLATE_PLAN = [
    ("pop", "P1", 4), ("pop", "P2", 4), ("pop", "P3", 3), ("pop", "P4", 3), ("pop", "P5", 3),
    ("emp", "E1", 4), ("emp", "E2", 4), ("emp", "E3", 3), ("emp", "E4", 3), ("emp", "E5", 3),
    ("cpi", "C1", 4), ("cpi", "C2", 3), ("cpi", "C3", 3), ("cpi", "C4", 3), ("cpi", "C5", 3),
]

ROUNDING_RULE = ("증감률·상승률·비중·격차(%p)는 소수 첫째 자리, 배율은 소수 둘째 자리, 지수 격차(포인트)는 소수 둘째 자리, "
                 "성비는 소수 첫째 자리, 인원(명)은 정수, 순위는 정수(동률은 같은 순위, 높은 순 1위). "
                 "반올림은 ROUND_HALF_UP(사사오입). 비율의 차이는 %p로 표기.")

CSV_COLUMNS = [
    "qid", "table_key", "template_id", "org_id", "tbl_id", "itm_id", "obj_codes", "prd_se", "periods",
    "unit_nm", "fetch_date", "lst_chn_de", "source_citation", "table_title_ko", "context_cells", "target_cells",
    "context_markdown", "question_ko", "required_list_text", "gold_required", "gold_aux",
    "rounding_rule_text", "gold_hash", "gold_pool",
]


# ---------------------------------------------------------------- 셀 저장소
class Table:
    def __init__(self, key, cfg, df):
        self.key = key
        self.cfg = cfg
        self.unit = cfg["unit"]
        self.df = df
        self.cells = {}
        for r in df.to_dict("records"):
            self.cells[(r["row_label"], r["col_label"], str(r["period"]))] = r
        self.rows = list(dict.fromkeys(df["row_label"].tolist()))
        self.cols = list(dict.fromkeys(df["col_label"].tolist()))
        self.periods = sorted(set(str(p) for p in df["period"].tolist()))

    def ok(self, k):
        r = self.cells.get(k)
        if r is None:
            return False
        if r["value"] is None or (isinstance(r["value"], float) and r["value"] != r["value"]):
            return False
        flag = r["flag"]
        if flag is None or (isinstance(flag, float) and flag != flag):
            flag = ""
        return str(flag).strip() == ""

    def all_ok(self, keys):
        return all(self.ok(k) for k in keys)

    def dec(self, k):
        return Decimal(str(self.cells[k]["dt_raw"]).replace(",", ""))

    def cell(self, k):
        r = self.cells[k]
        flag = r["flag"]
        if flag is None or (isinstance(flag, float) and flag != flag):
            flag = ""
        return {"row_label": k[0], "col_label": k[1], "period": k[2],
                "dt_raw": str(r["dt_raw"]), "value": float(r["value"]), "flag": str(flag)}

    def ok_rows(self, rows, cols, periods):
        """슬라이스의 모든 셀이 사용 가능한 행만 남긴다(부호·결측 행 제외)."""
        return [r for r in rows if self.all_ok([(r, c, p) for c in cols for p in periods])]


# ---------------------------------------------------------------- gold 항목
def q(exact, decimals):
    return exact.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def gold(name, typ, exact, decimals, formula, basis="exact"):
    exact = Decimal(exact)
    rounded = q(exact, decimals)
    if typ == "COUNT" and decimals == 0:
        display = f"{int(rounded):,}"
    elif typ == "RANK":
        display = str(int(rounded))
    else:
        display = f"{rounded:.{decimals}f}"
    sign = 1 if exact > 0 else (-1 if exact < 0 else 0)
    return {"name": name, "type": typ, "exact": float(exact), "display": display,
            "decimals": decimals, "sign": sign, "formula": formula, "basis": basis}


def pct_change(new, old):
    return (new / old - Decimal(1)) * Decimal(100)


def rank_desc(values, target_idx):
    """내림차순 순위, 동률 min(같은 값은 같은 순위, 1위=가장 큼)."""
    v = values[target_idx]
    return 1 + sum(1 for x in values if x > v)


def rank_of(d, key):
    vals = list(d.values())
    return rank_desc(vals, list(d.keys()).index(key))


def argmax(d):
    return max(d, key=lambda k: d[k])


def argmin(d):
    return min(d, key=lambda k: d[k])


def fmt_value(tbl, k):
    r = tbl.cells[k]
    if tbl.unit == "명":
        return f"{int(round(float(r['value']))):,}"
    return str(r["dt_raw"])


def markdown_table(tbl, keys, row_header):
    rows = list(dict.fromkeys(k[0] for k in keys))
    cols = list(dict.fromkeys((k[1], k[2]) for k in keys))
    col_labels = list(dict.fromkeys(c[0] for c in cols))
    periods = list(dict.fromkeys(c[1] for c in cols))

    def head(c):
        if len(col_labels) == 1 and len(periods) > 1:
            return f"{c[1]}년"
        return f"{c[0]}({c[1]}년)"

    lines = [f"단위: {tbl.unit}",
             "| " + row_header + " | " + " | ".join(head(c) for c in cols) + " |",
             "|" + "---|" * (len(cols) + 1)]
    for r in rows:
        vals = []
        for c in cols:
            k = (r, c[0], c[1])
            vals.append(fmt_value(tbl, k) if k in tbl.cells else "")
        lines.append(f"| {r} | " + " | ".join(vals) + " |")
    return "\n".join(lines)


def required_list_text(items):
    return "  ".join(f"{i + 1}) {g['name']}" for i, g in enumerate(items))


def uniq(keys):
    return list(dict.fromkeys(keys))


# ---------------------------------------------------------------- gold_aux 자동 확장
def unit_type(unit):
    """셀 단위 → 주장 타입 (채점기 규칙과 동일: % → PCT, 명 → COUNT, 그 외 → INDEX)."""
    if unit == "%":
        return "PCT"
    if unit == "명":
        return "COUNT"
    return "INDEX"


def expand_aux(req, aux, target_cells, unit):
    """모델이 정당하게 언급할 수 있는 일반 파생값을 gold_aux 에 보강한다(거짓양성 방지). target_cells 에만 적용.
    (a) PCT 쌍(gold_required 끼리, 셀 끼리) → 차이 PP(1자리); gold_required 끼리는 COUNT/PP/INDEX/RATIO 동일 타입 쌍도 차이값 생성(RANK 제외)
    (b) 같은 행 또는 같은 열 라벨을 공유하는 COUNT 셀 쌍 → 차이 COUNT, 배율 RATIO(2자리), 비율 PCT(a/b*100, 1자리)
    (c) INDEX 쌍(gold_required 끼리, 셀 끼리) → 차이 INDEX(2자리), 변화율 PCT(1자리)
    차이값은 exact 기준(basis=exact)과, 표시값(반올림값)끼리 뺀 값(basis=display)이 다르면 둘 다 넣는다.
    쌍은 (뒤 원소 − 앞 원소)/(뒤/앞) 방향. (type, |display|) 중복 제거. 상한 AUX_CAP:
    템플릿 지정 aux → gold_required 끼리 쌍 → 셀끼리 쌍(같은 시점 → 같은 열 라벨 → 그 외) 순."""
    ctype = unit_type(unit)
    pool = []
    for g in req:
        pool.append({"name": g["name"], "type": g["type"], "exact": Decimal(str(g["exact"])), "disp": Decimal(g["display"].replace(",", "")),
                     "dec": g["decimals"], "row": None, "col": None, "period": None, "from_req": True})
    for c in target_cells:
        d = Decimal(str(c["dt_raw"]).replace(",", ""))
        pool.append({"name": f"{c['row_label']} {c['col_label']} {c['period']}년", "type": ctype, "exact": d, "disp": d,
                     "row": c["row_label"], "col": c["col_label"], "period": c["period"], "from_req": False})

    def dkey(g):  # exact 가 다르면(표시값이 같아도) 별개 항목으로 유지: 모델이 "6.45%" 처럼 더 많은 자릿수를 쓸 수 있음. 부호는 무시(|claim| vs |gold|)
        return (g["type"], round(abs(g["exact"]), 6))

    seen = {dkey(g) for g in req}
    base = []
    for g in aux:  # 템플릿 지정 aux 도 gold_required·서로 간 중복 제거
        key = dkey(g)
        if key not in seen:
            seen.add(key)
            base.append(g)
    aux = base
    with_req, cells_only = [], []

    def add(item, involves_req, prio=0):
        key = dkey(item)
        if key in seen:
            return
        seen.add(key)
        if involves_req:
            with_req.append(item)
        else:
            cells_only.append((prio, len(cells_only), item))

    def add_diff(name, typ, a, b, decimals, formula, inv, prio):
        add(gold(name, typ, a["exact"] - b["exact"], decimals, formula, "exact"), inv, prio)
        if a["from_req"] or b["from_req"]:
            d_item = gold(name, typ, a["disp"] - b["disp"], decimals, formula + " [display 기준]", "display")
            add(d_item, inv, prio)  # exact 가 같으면 dkey 중복으로 자동 제거

    n = len(pool)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = pool[j], pool[i]  # a: 뒤, b: 앞
            if a["type"] != b["type"] or a["from_req"] != b["from_req"]:
                # gold_required 값과 원 셀(수준값)을 섞은 쌍은 의미 없는 값을 만들어 환각 주장을 MATCH 로 둔갑시킬 수 있어 제외
                continue
            inv = a["from_req"]
            prio = 0 if (a["period"] is not None and a["period"] == b["period"]) else (1 if a["col"] == b["col"] else 2)
            t = a["type"]
            if inv:
                # gold_required 끼리: 같은 타입이면 모두 차이값 생성 (PCT→PP 1자리, COUNT→COUNT, PP→PP, INDEX→INDEX, RATIO→RATIO 2자리; RANK 제외)
                if t == "RANK" or (t == "COUNT" and a["dec"] != b["dec"]):
                    continue  # 성비(1자리)와 인원 차이(명, 0자리)처럼 종류가 다른 COUNT 는 섞지 않는다
                dtyp = "PP" if t == "PCT" else t
                ddec = {"PCT": 1, "PP": 1, "COUNT": max(a["dec"], b["dec"]), "INDEX": 2, "RATIO": 2}[t]
                unit_word = {"PP": "%p", "COUNT": "명" if ddec == 0 else "", "INDEX": "포인트", "RATIO": "배"}[dtyp]
                add_diff(f"{a['name']}-{b['name']} 차이({unit_word})" if unit_word else f"{a['name']}-{b['name']} 차이", dtyp, a, b, ddec, "auto(a): a-b", inv, prio)
                continue
            if t == "PCT":
                add_diff(f"{a['name']}-{b['name']} 차이(%p)", "PP", a, b, 1, "auto(a): a-b", inv, prio)
            elif t == "COUNT":
                if a["row"] is None or b["row"] is None or not (a["row"] == b["row"] or a["col"] == b["col"]):
                    continue
                if b["exact"] == 0:
                    continue
                add_diff(f"{a['name']}-{b['name']} 차이(명)", "COUNT", a, b, 0, "auto(b): a-b", inv, prio)
                add(gold(f"{a['name']}/{b['name']} 배율(배)", "RATIO", a["exact"] / b["exact"], 2, "auto(b): a/b"), inv, prio)
                add(gold(f"{a['name']}/{b['name']} 비율(%)", "PCT", a["exact"] / b["exact"] * 100, 1, "auto(b): a/b*100"), inv, prio)
            elif t == "INDEX":
                if b["exact"] == 0:
                    continue
                add_diff(f"{a['name']}-{b['name']} 차이(포인트)", "INDEX", a, b, 2, "auto(c): a-b", inv, prio)
                add(gold(f"{a['name']} 대비 {b['name']} 변화율(%)", "PCT", (a["exact"] / b["exact"] - 1) * 100, 1, "auto(c): (a/b-1)*100"), inv, prio)
    cells_sorted = [it for _, _, it in sorted(cells_only, key=lambda x: (x[0], x[1]))]
    return (list(aux) + with_req + cells_sorted)[:AUX_CAP]


# ---------------------------------------------------------------- 슬라이스·표 전체 통계 (pop)
T = "총인구수"


def pop_regions(tbl):
    return tbl.ok_rows([r for r in tbl.rows if r != "전국"], [T], YEARS)


def pop_total_slice(tbl):
    rows = (["전국"] if tbl.all_ok([("전국", T, y) for y in YEARS]) else []) + pop_regions(tbl)
    return [(r, T, y) for r in rows for y in YEARS]


def pop_sex_regions(tbl):
    return tbl.ok_rows([r for r in tbl.rows if r != "전국"], ["남자인구수", "여자인구수"], ["2023", "2024"])


def pop_sex_slice(tbl):
    return [(r, c, y) for r in pop_sex_regions(tbl) for y in ("2023", "2024") for c in ("남자인구수", "여자인구수")]


def pop_growth(tbl, y1, y0):
    return {r: pct_change(tbl.dec((r, T, y1)), tbl.dec((r, T, y0))) for r in pop_regions(tbl)}


def tmpl_P1(tbl, rng, used):
    regions = pop_regions(tbl)
    n = len(regions)
    r = rng.choice([x for x in regions if x not in used])
    used.add(r)
    v = {y: tbl.dec((r, T, y)) for y in YEARS}
    g24 = pop_growth(tbl, "2024", "2023")
    best = argmax(g24)
    req = [
        gold("2023년 전년 대비 인구 증감률(%)", "PCT", pct_change(v["2023"], v["2022"]), 1, f"({r} 2023/{r} 2022-1)*100"),
        gold("2024년 전년 대비 인구 증감률(%)", "PCT", pct_change(v["2024"], v["2023"]), 1, f"({r} 2024/{r} 2023-1)*100"),
        gold("2022년 대비 2024년 누적 인구 증감률(%)", "PCT", pct_change(v["2024"], v["2022"]), 1, f"({r} 2024/{r} 2022-1)*100"),
        gold(f"{n}개 시도 중 {r}의 2024년 전년 대비 증감률 순위(높은 순)", "RANK", rank_of(g24, r), 0, f"전국 제외 {n}개 시도 2024년 증감률 내림차순, 동률 min"),
        gold(f"{n}개 시도 중 2024년 전년 대비 증감률이 가장 높은 시도의 증감률(%)", "PCT", g24[best], 1, f"max = {best}"),
    ]
    aux = [
        gold("2023년 전년 대비 인구 증감(명)", "COUNT", v["2023"] - v["2022"], 0, f"{r} 2023-{r} 2022"),
        gold("2024년 전년 대비 인구 증감(명)", "COUNT", v["2024"] - v["2023"], 0, f"{r} 2024-{r} 2023"),
        gold("2022년 대비 2024년 인구 증감(명)", "COUNT", v["2024"] - v["2022"], 0, f"{r} 2024-{r} 2022"),
    ]
    target = [(r, T, y) for y in YEARS] + [(best, T, "2023"), (best, T, "2024")]
    question = (f"표의 '{r}' 행(총인구수)을 사용하여 2022~2024년 인구 변화를 설명하시오. 2023년과 2024년 각각의 전년 대비 증감률(%)과 "
                f"2022년 대비 2024년 누적 증감률(%)을 제시하시오. 또한 전국을 제외한 {n}개 시도 전체의 2024년 전년 대비 증감률을 비교하여, "
                f"{r}의 순위(증감률이 높은 순, 1위가 가장 높음)와 증감률이 가장 높은 시도의 증감률(%)을 제시하시오.")
    ranks = [("2024년 전년 대비 증감률(%)", "PCT", 1, g24)]
    return pop_total_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_P2(tbl, rng, used):
    regions = pop_sex_regions(tbl)
    n = len(regions)
    r = rng.choice([x for x in regions if x not in used])
    used.add(r)
    yrs = ["2023", "2024"]
    m = {y: tbl.dec((r, "남자인구수", y)) for y in yrs}
    f = {y: tbl.dec((r, "여자인구수", y)) for y in yrs}
    ratio24 = {x: tbl.dec((x, "남자인구수", "2024")) / tbl.dec((x, "여자인구수", "2024")) * 100 for x in regions}
    best = argmax(ratio24)
    req = [
        gold("2024년 성비(여성 100명당 남성 수)", "COUNT", m["2024"] / f["2024"] * 100, 1, "남자 2024/여자 2024*100"),
        gold("2023년 성비(여성 100명당 남성 수)", "COUNT", m["2023"] / f["2023"] * 100, 1, "남자 2023/여자 2023*100"),
        gold("2024년 남자 인구 비중(%)", "PCT", m["2024"] / (m["2024"] + f["2024"]) * 100, 1, "남자 2024/(남자 2024+여자 2024)*100"),
        gold("2024년 남자 인구에서 여자 인구를 뺀 차이(명)", "COUNT", m["2024"] - f["2024"], 0, "남자 2024-여자 2024"),
        gold(f"{n}개 시도 중 {r}의 2024년 성비 순위(높은 순)", "RANK", rank_of(ratio24, r), 0, f"{n}개 시도 2024년 성비 내림차순, 동률 min"),
        gold(f"{n}개 시도 중 2024년 성비가 가장 높은 시도의 성비(여성 100명당 남성 수)", "COUNT", ratio24[best], 1, f"max = {best}"),
    ]
    aux = [
        gold("2024년 여자 인구 비중(%)", "PCT", f["2024"] / (m["2024"] + f["2024"]) * 100, 1, "여자 2024/(남자 2024+여자 2024)*100"),
        gold("2024년 남녀 합계 인구(명)", "COUNT", m["2024"] + f["2024"], 0, "남자 2024+여자 2024"),
        gold("2023년 남자 인구에서 여자 인구를 뺀 차이(명)", "COUNT", m["2023"] - f["2023"], 0, "남자 2023-여자 2023"),
    ]
    target = [(r, c, y) for y in yrs for c in ("남자인구수", "여자인구수")] + [(best, c, "2024") for c in ("남자인구수", "여자인구수")]
    question = (f"표의 '{r}' 행을 사용하여 2023년과 2024년의 성별 인구를 비교하시오. 각 연도의 성비(여성 100명당 남성 수), 2024년 남자 인구 비중(%), "
                f"2024년 남자 인구에서 여자 인구를 뺀 차이(명)를 제시하시오. 또한 표의 {n}개 시도 전체의 2024년 성비를 비교하여 {r}의 순위(성비가 높은 순, "
                f"1위가 가장 높음)와 성비가 가장 높은 시도의 성비를 제시하시오.")
    ranks = [("2024년 성비(여성 100명당 남성 수)", "COUNT", 1, ratio24)]
    return pop_sex_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_P3(tbl, rng, used):
    regions = pop_regions(tbl)
    n = len(regions)
    pop24 = {r: tbl.dec((r, T, "2024")) for r in regions}
    for _ in range(1000):
        sel = rng.sample(regions, 5)
        sel = [r for r in regions if r in sel]
        vals = [pop24[r] for r in sel]
        imax = vals.index(max(vals))
        fi = rng.choice([i for i in range(5) if i != imax])
        sig = (tuple(sel), sel[fi])
        if sig not in used:
            used.add(sig)
            break
    f = sel[fi]
    vmax, vmin = max(vals), min(vals)
    rmax, rmin = sel[vals.index(vmax)], sel[vals.index(vmin)]
    top = argmax(pop24)
    nat = tbl.dec(("전국", T, "2024"))
    req = [
        gold(f"전국 제외 {n}개 시도 중 {f}의 2024년 인구 순위(많은 순)", "RANK", rank_of(pop24, f), 0, f"{n}개 시도 2024년 총인구수 내림차순, 동률 min"),
        gold("5개 시도 중 인구 최대 시도와 최소 시도의 차이(명)", "COUNT", vmax - vmin, 0, f"{rmax}-{rmin}"),
        gold("5개 시도 중 최대 시도 인구의 최소 시도 대비 배율(배)", "RATIO", vmax / vmin, 2, f"{rmax}/{rmin}"),
        gold(f"5개 시도 중 최대 시도 인구 대비 {f} 인구 비율(%)", "PCT", vals[fi] / vmax * 100, 1, f"{f}/{rmax}*100"),
        gold(f"{n}개 시도 중 2024년 인구가 가장 많은 시도의 전국 대비 비중(%)", "PCT", pop24[top] / nat * 100, 1, f"{top} 2024/전국 2024*100"),
    ]
    tot = sum(vals)
    aux = [
        gold("5개 시도 인구 합계(명)", "COUNT", tot, 0, "+".join(sel)),
        gold("5개 시도 합계 대비 최대 시도 비중(%)", "PCT", vmax / tot * 100, 1, f"{rmax}/합계*100"),
        gold(f"{f} 인구의 5개 중 최소 시도 대비 배율(배)", "RATIO", vals[fi] / vmin, 2, f"{f}/{rmin}"),
        gold(f"{f}의 전국 대비 비중(%)", "PCT", vals[fi] / nat * 100, 1, f"{f} 2024/전국 2024*100"),
    ]
    target = [(r, T, "2024") for r in sel] + [("전국", T, "2024"), (top, T, "2024")]
    names = ", ".join(f"'{r}'" for r in sel)
    question = (f"표의 2024년 열에서 {names} 5개 시도를 비교하시오. 이 5개 중 인구가 가장 많은 시도와 가장 적은 시도의 차이(명)와 배율(배), "
                f"그리고 가장 많은 시도 대비 {f}의 인구 비율(%)을 제시하시오. 또한 전국을 제외한 {n}개 시도 전체에서 {f}의 2024년 인구 순위(많은 순, "
                f"1위가 가장 많음)와, {n}개 시도 중 인구가 가장 많은 시도의 전국 대비 비중(%)을 제시하시오.")
    ranks = [("2024년 총인구수(명)", "COUNT", 0, pop24)]
    return pop_total_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_P4(tbl, rng, used):
    regions = pop_regions(tbl)
    n = len(regions)
    yrs = ["2022", "2024"]
    for _ in range(1000):
        a, b = rng.sample(regions, 2)
        if tbl.dec((a, T, "2022")) < tbl.dec((b, T, "2022")):
            a, b = b, a
        if (a, b) not in used:
            used.add((a, b))
            break
    va = {y: tbl.dec((a, T, y)) for y in yrs}
    vb = {y: tbl.dec((b, T, y)) for y in yrs}
    g = pop_growth(tbl, "2024", "2022")
    best = argmax(g)
    req = [
        gold(f"2022년 {a} 인구의 {b} 대비 배율(배)", "RATIO", va["2022"] / vb["2022"], 2, f"{a} 2022/{b} 2022"),
        gold(f"2024년 {a} 인구의 {b} 대비 배율(배)", "RATIO", va["2024"] / vb["2024"], 2, f"{a} 2024/{b} 2024"),
        gold(f"{a}의 2022년 대비 2024년 인구 증감률(%)", "PCT", g[a], 1, f"({a} 2024/{a} 2022-1)*100"),
        gold(f"{b}의 2022년 대비 2024년 인구 증감률(%)", "PCT", g[b], 1, f"({b} 2024/{b} 2022-1)*100"),
        gold(f"{n}개 시도 중 {a}의 2022년 대비 2024년 증감률 순위(높은 순)", "RANK", rank_of(g, a), 0, f"전국 제외 {n}개 시도 2022→2024 증감률 내림차순, 동률 min"),
        gold(f"{n}개 시도 중 2022년 대비 2024년 증감률이 가장 높은 시도의 증감률(%)", "PCT", g[best], 1, f"max = {best}"),
    ]
    aux = [
        gold(f"2022년 {a}와 {b}의 인구 차이(명)", "COUNT", va["2022"] - vb["2022"], 0, f"{a} 2022-{b} 2022"),
        gold(f"2024년 {a}와 {b}의 인구 차이(명)", "COUNT", va["2024"] - vb["2024"], 0, f"{a} 2024-{b} 2024"),
        gold(f"{a}의 2022년 대비 2024년 인구 증감(명)", "COUNT", va["2024"] - va["2022"], 0, f"{a} 2024-{a} 2022"),
        gold(f"{b}의 2022년 대비 2024년 인구 증감(명)", "COUNT", vb["2024"] - vb["2022"], 0, f"{b} 2024-{b} 2022"),
    ]
    target = [(r, T, y) for r in (a, b) for y in yrs] + [(best, T, y) for y in yrs]
    question = (f"표의 '{a}' 행과 '{b}' 행(총인구수)을 2022년과 2024년에 비교하시오. 각 연도에 {a} 인구가 {b} 인구의 몇 배인지(배)와 "
                f"두 시도 각각의 2022년 대비 2024년 증감률(%)을 제시하시오. 또한 전국을 제외한 {n}개 시도 전체의 2022년 대비 2024년 증감률을 비교하여 "
                f"{a}의 순위(증감률이 높은 순, 1위가 가장 높음)와 증감률이 가장 높은 시도의 증감률(%)을 제시하시오.")
    ranks = [("2022년 대비 2024년 증감률(%)", "PCT", 1, g)]
    return pop_total_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_P5(tbl, rng, used):
    regions = pop_regions(tbl)
    n = len(regions)
    yrs = ["2022", "2024"]
    if not tbl.all_ok([("전국", T, y) for y in yrs]):
        raise RuntimeError("P5: 전국 셀 사용 불가")
    for _ in range(1000):
        sel = rng.sample(regions, 3)
        sel = [r for r in regions if r in sel]
        if tuple(sel) not in used:
            used.add(tuple(sel))
            break
    nat = {y: tbl.dec(("전국", T, y)) for y in yrs}
    v = {r: {y: tbl.dec((r, T, y)) for y in yrs} for r in sel}
    r1, r2, r3 = sel
    sh = lambda r, y: v[r][y] / nat[y] * 100
    pop24 = {r: tbl.dec((r, T, "2024")) for r in regions}
    req = [
        gold(f"2024년 {r1}의 전국 인구 비중(%)", "PCT", sh(r1, "2024"), 1, f"{r1} 2024/전국 2024*100"),
        gold(f"2024년 {r2}의 전국 인구 비중(%)", "PCT", sh(r2, "2024"), 1, f"{r2} 2024/전국 2024*100"),
        gold(f"2024년 {r3}의 전국 인구 비중(%)", "PCT", sh(r3, "2024"), 1, f"{r3} 2024/전국 2024*100"),
        gold(f"{r1} 비중의 2022년 대비 2024년 변화(%p)", "PP", sh(r1, "2024") - sh(r1, "2022"), 1, f"{r1} 비중 2024-{r1} 비중 2022"),
        gold("전국 인구의 2022년 대비 2024년 증감률(%)", "PCT", pct_change(nat["2024"], nat["2022"]), 1, "(전국 2024/전국 2022-1)*100"),
        gold(f"{n}개 시도 중 {r1}의 2024년 전국 비중 순위(높은 순)", "RANK", rank_of(pop24, r1), 0, f"비중 순위 = 2024년 총인구수 순위, {n}개 시도 내림차순, 동률 min"),
    ]
    aux = [
        gold(f"2022년 {r1}의 전국 인구 비중(%)", "PCT", sh(r1, "2022"), 1, f"{r1} 2022/전국 2022*100"),
        gold(f"2022년 {r2}의 전국 인구 비중(%)", "PCT", sh(r2, "2022"), 1, f"{r2} 2022/전국 2022*100"),
        gold(f"2022년 {r3}의 전국 인구 비중(%)", "PCT", sh(r3, "2022"), 1, f"{r3} 2022/전국 2022*100"),
        gold(f"{r2} 비중의 2022년 대비 2024년 변화(%p)", "PP", sh(r2, "2024") - sh(r2, "2022"), 1, f"{r2} 비중 2024-{r2} 비중 2022"),
        gold(f"{r3} 비중의 2022년 대비 2024년 변화(%p)", "PP", sh(r3, "2024") - sh(r3, "2022"), 1, f"{r3} 비중 2024-{r3} 비중 2022"),
        gold("2024년 3개 시도 비중 합계(%)", "PCT", sum(sh(r, "2024") for r in sel), 1, "3개 시도 비중 합"),
        gold("2024년 3개 시도 인구 합계(명)", "COUNT", sum(v[r]["2024"] for r in sel), 0, "+".join(sel)),
        gold("전국 인구의 2022년 대비 2024년 증감(명)", "COUNT", nat["2024"] - nat["2022"], 0, "전국 2024-전국 2022"),
    ]
    target = [("전국", T, y) for y in yrs] + [(r, T, y) for r in sel for y in yrs]
    question = (f"표의 '전국' 행과 '{r1}', '{r2}', '{r3}' 행(총인구수)의 2022년·2024년 값을 사용하여, 2024년 세 시도 각각의 전국 대비 인구 비중(%), "
                f"{r1} 비중의 2022년 대비 2024년 변화(%p), 전국 인구의 2022년 대비 2024년 증감률(%)을 제시하시오. 또한 전국을 제외한 {n}개 시도 전체에서 "
                f"{r1}의 2024년 전국 비중 순위(비중이 높은 순, 1위가 가장 높음)를 제시하시오.")
    ranks = [("2024년 총인구수(명)", "COUNT", 0, pop24)]
    return pop_total_slice(tbl), uniq(target), question, req, aux, ranks


# ---------------------------------------------------------------- emp
SEX = ["계", "남자", "여자"]


def emp_ages(tbl):
    return tbl.ok_rows([r for r in tbl.rows if r != "계"], SEX, YEARS)


def emp_full_slice(tbl):
    rows = (["계"] if tbl.all_ok([("계", s, y) for s in SEX for y in YEARS]) else []) + emp_ages(tbl)
    return [(a, s, y) for a in rows for s in SEX for y in YEARS]


def emp_gap(tbl, y):
    return {a: tbl.dec((a, "남자", y)) - tbl.dec((a, "여자", y)) for a in emp_ages(tbl)}


def tmpl_E1(tbl, rng, used):
    ages = emp_ages(tbl)
    n = len(ages)
    for _ in range(1000):
        a1, a2 = rng.sample(ages, 2)
        if (a1, a2) not in used:
            used.add((a1, a2))
            break
    m1, f1 = tbl.dec((a1, "남자", "2024")), tbl.dec((a1, "여자", "2024"))
    m2, f2 = tbl.dec((a2, "남자", "2024")), tbl.dec((a2, "여자", "2024"))
    gap = emp_gap(tbl, "2024")
    best = argmax(gap)
    req = [
        gold(f"2024년 {a1} 남녀 고용률 격차(남자-여자, %p)", "PP", m1 - f1, 1, f"{a1} 남자-{a1} 여자"),
        gold(f"2024년 {a2} 남녀 고용률 격차(남자-여자, %p)", "PP", m2 - f2, 1, f"{a2} 남자-{a2} 여자"),
        gold(f"{a1} 격차에서 {a2} 격차를 뺀 차이(%p)", "PP", (m1 - f1) - (m2 - f2), 1, f"({a1} 격차)-({a2} 격차)"),
        gold(f"{a1} 남자 고용률의 여자 고용률 대비 배율(배)", "RATIO", m1 / f1, 2, f"{a1} 남자/{a1} 여자"),
        gold(f"{n}개 연령계층 중 {a1}의 2024년 남녀 격차 순위(큰 순)", "RANK", rank_of(gap, a1), 0, f"계 제외 {n}개 연령계층 2024년 (남자-여자) 내림차순, 동률 min"),
        gold(f"{n}개 연령계층 중 2024년 남녀 격차가 가장 큰 연령계층의 격차(%p)", "PP", gap[best], 1, f"max = {best}"),
    ]
    aux = [
        gold(f"{a2} 남자 고용률의 여자 고용률 대비 배율(배)", "RATIO", m2 / f2, 2, f"{a2} 남자/{a2} 여자"),
        gold(f"{a1} 여자 고용률의 남자 대비 비율(%)", "PCT", f1 / m1 * 100, 1, f"{a1} 여자/{a1} 남자*100"),
        gold(f"{a2} 여자 고용률의 남자 대비 비율(%)", "PCT", f2 / m2 * 100, 1, f"{a2} 여자/{a2} 남자*100"),
        gold(f"{a1} 격차 순위(절댓값 기준)", "RANK", rank_of({k: abs(v) for k, v in gap.items()}, a1), 0, f"계 제외 {n}개 연령계층 |남자-여자| 내림차순, 동률 min"),
    ]
    target = [(a, s, "2024") for a in (a1, a2) for s in ("남자", "여자")] + [(best, s, "2024") for s in ("남자", "여자")]
    question = (f"표의 '{a1}' 행과 '{a2}' 행의 남자·여자 2024년 고용률을 사용하여 비교하시오. 각 연령계층의 남녀 격차(남자-여자, %p), 두 격차의 차이(%p), "
                f"{a1}에서 남자 고용률이 여자 고용률의 몇 배인지(배)를 제시하시오. 또한 '계' 행을 제외한 {n}개 연령계층 전체의 2024년 남녀 격차(남자-여자)를 "
                f"비교하여 {a1}의 순위(격차가 큰 순, 1위가 가장 큼)와 격차가 가장 큰 연령계층의 격차(%p)를 제시하시오.")
    ranks = [("2024년 남녀 고용률 격차(남자-여자, %p)", "PP", 1, gap)]
    return emp_full_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_E2(tbl, rng, used):
    ages = emp_ages(tbl)
    n = len(ages)
    a = rng.choice([x for x in ages if x not in used])
    used.add(a)
    v = {y: tbl.dec((a, "계", y)) for y in YEARS}
    chg = {x: tbl.dec((x, "계", "2024")) - tbl.dec((x, "계", "2023")) for x in ages}
    best = argmax(chg)
    req = [
        gold("2023년 고용률의 전년 대비 변화(%p)", "PP", v["2023"] - v["2022"], 1, "2023-2022"),
        gold("2023년 고용률의 전년 대비 상대 변화율(%)", "PCT", pct_change(v["2023"], v["2022"]), 1, "(2023/2022-1)*100"),
        gold("2024년 고용률의 전년 대비 변화(%p)", "PP", v["2024"] - v["2023"], 1, "2024-2023"),
        gold("2024년 고용률의 전년 대비 상대 변화율(%)", "PCT", pct_change(v["2024"], v["2023"]), 1, "(2024/2023-1)*100"),
        gold(f"{n}개 연령계층 중 {a}의 2024년 전년 대비 변화(%p) 순위(높은 순)", "RANK", rank_of(chg, a), 0, f"계 제외 {n}개 연령계층 '계' 열 2024-2023 내림차순, 동률 min"),
        gold(f"{n}개 연령계층 중 2024년 전년 대비 변화가 가장 큰 연령계층의 변화(%p)", "PP", chg[best], 1, f"max = {best}"),
    ]
    aux = [
        gold("2022년 대비 2024년 고용률 변화(%p)", "PP", v["2024"] - v["2022"], 1, "2024-2022"),
        gold("2022년 대비 2024년 고용률 상대 변화율(%)", "PCT", pct_change(v["2024"], v["2022"]), 1, "(2024/2022-1)*100"),
        gold("2022~2024년 고용률 평균(%)", "PCT", (v["2022"] + v["2023"] + v["2024"]) / 3, 1, "(2022+2023+2024)/3"),
    ]
    target = [(a, "계", y) for y in YEARS] + [(best, "계", "2023"), (best, "계", "2024")]
    question = (f"표의 '{a}' 행 가운데 '계'(남녀 전체) 열의 2022~2024년 고용률을 사용하여 추이를 설명하시오. 2023년과 2024년 각각에 대해 전년 대비 변화를 %p로 "
                f"제시하고, 같은 변화를 전년 대비 상대 변화율(%)로도 함께 제시하시오. 또한 '계' 행을 제외한 {n}개 연령계층 전체의 '계' 열 2024년 전년 대비 변화(%p)를 "
                f"비교하여 {a}의 순위(변화가 큰 순, 1위가 가장 큼)와 변화가 가장 큰 연령계층의 변화(%p)를 제시하시오.")
    ranks = [("계 열 2024년 전년 대비 변화(%p)", "PP", 1, chg)]
    return emp_full_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_E3(tbl, rng, used):
    ages = emp_ages(tbl)
    n = len(ages)
    for _ in range(1000):
        f = rng.choice(ages)
        if f not in used:
            used.add(f)
            break
    vals = {a: tbl.dec((a, "계", "2024")) for a in ages}
    amax, amin = argmax(vals), argmin(vals)
    gap = emp_gap(tbl, "2024")
    req = [
        gold(f"{n}개 연령계층 중 2024년 고용률이 가장 높은 연령계층의 고용률(%)", "PCT", vals[amax], 1, f"max = {amax}"),
        gold(f"{n}개 연령계층 중 2024년 고용률이 가장 낮은 연령계층의 고용률(%)", "PCT", vals[amin], 1, f"min = {amin}"),
        gold("최고 연령계층과 최저 연령계층의 고용률 격차(%p)", "PP", vals[amax] - vals[amin], 1, f"{amax}-{amin}"),
        gold(f"{n}개 연령계층 중 {f}의 2024년 고용률 순위(높은 순)", "RANK", rank_of(vals, f), 0, f"계 제외 {n}개 연령계층 '계' 열 2024 내림차순, 동률 min"),
        gold(f"{n}개 연령계층 중 {f}의 2024년 남녀 격차(남자-여자) 순위(큰 순)", "RANK", rank_of(gap, f), 0, f"계 제외 {n}개 연령계층 2024년 (남자-여자) 내림차순, 동률 min"),
    ]
    aux = [
        gold(f"{n}개 연령계층 고용률 단순 평균(%)", "PCT", sum(vals.values()) / n, 1, f"합/{n}"),
        gold("최고 연령계층 고용률의 최저 연령계층 대비 배율(배)", "RATIO", vals[amax] / vals[amin], 2, f"{amax}/{amin}"),
        gold(f"최고 연령계층과 {f}의 고용률 격차(%p)", "PP", vals[amax] - vals[f], 1, f"{amax}-{f}"),
        gold(f"{f}의 2024년 남녀 고용률 격차(남자-여자, %p)", "PP", gap[f], 1, f"{f} 남자-{f} 여자"),
    ]
    target = [(a, "계", "2024") for a in ages] + [(f, s, "2024") for s in ("남자", "여자")]
    question = (f"표에서 '계' 행을 제외한 {n}개 연령계층의 '계'(남녀 전체) 열 2024년 고용률을 비교하시오. 고용률이 가장 높은 연령계층과 가장 낮은 연령계층의 "
                f"고용률(%), 두 값의 격차(%p), {f}의 고용률 순위(높은 순, 1위가 가장 높음)를 제시하시오. 또한 같은 {n}개 연령계층의 2024년 남녀 격차(남자 고용률-여자 고용률)를 "
                f"비교하여 {f}의 격차 순위(격차가 큰 순, 1위가 가장 큼)를 제시하시오.")
    ranks = [("계 열 2024년 고용률(%)", "PCT", 1, vals), ("2024년 남녀 고용률 격차(남자-여자, %p)", "PP", 1, gap)]
    return emp_full_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_E4(tbl, rng, used):
    ages = emp_ages(tbl)
    n = len(ages)
    a = rng.choice([x for x in ages if x not in used])
    used.add(a)
    yrs = ["2022", "2024"]
    m = {y: tbl.dec((a, "남자", y)) for y in yrs}
    f = {y: tbl.dec((a, "여자", y)) for y in yrs}
    gap = emp_gap(tbl, "2024")
    best = argmax(gap)
    req = [
        gold("2022년 남녀 고용률 격차(남자-여자, %p)", "PP", m["2022"] - f["2022"], 1, "남자 2022-여자 2022"),
        gold("2024년 남녀 고용률 격차(남자-여자, %p)", "PP", m["2024"] - f["2024"], 1, "남자 2024-여자 2024"),
        gold("격차의 2022년 대비 2024년 변화(%p)", "PP", (m["2024"] - f["2024"]) - (m["2022"] - f["2022"]), 1, "격차 2024-격차 2022"),
        gold("여자 고용률의 2022년 대비 2024년 상대 변화율(%)", "PCT", pct_change(f["2024"], f["2022"]), 1, "(여자 2024/여자 2022-1)*100"),
        gold(f"{n}개 연령계층 중 {a}의 2024년 남녀 격차 순위(큰 순)", "RANK", rank_of(gap, a), 0, f"계 제외 {n}개 연령계층 2024년 (남자-여자) 내림차순, 동률 min"),
        gold(f"{n}개 연령계층 중 2024년 남녀 격차가 가장 큰 연령계층의 격차(%p)", "PP", gap[best], 1, f"max = {best}"),
    ]
    aux = [
        gold("남자 고용률의 2022년 대비 2024년 상대 변화율(%)", "PCT", pct_change(m["2024"], m["2022"]), 1, "(남자 2024/남자 2022-1)*100"),
        gold("남자 고용률의 2022년 대비 2024년 변화(%p)", "PP", m["2024"] - m["2022"], 1, "남자 2024-남자 2022"),
        gold("여자 고용률의 2022년 대비 2024년 변화(%p)", "PP", f["2024"] - f["2022"], 1, "여자 2024-여자 2022"),
        gold(f"{a} 격차 순위(절댓값 기준)", "RANK", rank_of({k: abs(v) for k, v in gap.items()}, a), 0, f"계 제외 {n}개 연령계층 2024년 |남자-여자| 내림차순, 동률 min"),
    ]
    target = [(a, s, y) for y in yrs for s in ("남자", "여자")] + [(best, s, "2024") for s in ("남자", "여자")]
    question = (f"표의 '{a}' 행의 남자·여자 열(2022년, 2024년)을 사용하여 남녀 고용률 격차의 변화를 설명하시오. 각 연도의 격차(남자-여자, %p), 격차의 변화(%p), "
                f"여자 고용률의 2022년 대비 2024년 상대 변화율(%)을 제시하시오. 또한 '계' 행을 제외한 {n}개 연령계층 전체의 2024년 남녀 격차를 비교하여 "
                f"{a}의 순위(격차가 큰 순, 1위가 가장 큼)와 격차가 가장 큰 연령계층의 격차(%p)를 제시하시오.")
    ranks = [("2024년 남녀 고용률 격차(남자-여자, %p)", "PP", 1, gap)]
    return emp_full_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_E5(tbl, rng, used):
    ages = emp_ages(tbl)
    n = len(ages)
    a = rng.choice([x for x in ages if x not in used])
    used.add(a)
    yrs = ["2023", "2024"]
    m = {y: tbl.dec((a, "남자", y)) for y in yrs}
    f = {y: tbl.dec((a, "여자", y)) for y in yrs}
    gap = emp_gap(tbl, "2024")
    req = [
        gold("남자 고용률의 2023년 대비 2024년 변화(%p)", "PP", m["2024"] - m["2023"], 1, "남자 2024-남자 2023"),
        gold("남자 고용률의 2023년 대비 2024년 상대 변화율(%)", "PCT", pct_change(m["2024"], m["2023"]), 1, "(남자 2024/남자 2023-1)*100"),
        gold("여자 고용률의 2023년 대비 2024년 변화(%p)", "PP", f["2024"] - f["2023"], 1, "여자 2024-여자 2023"),
        gold("여자 고용률의 2023년 대비 2024년 상대 변화율(%)", "PCT", pct_change(f["2024"], f["2023"]), 1, "(여자 2024/여자 2023-1)*100"),
        gold("2024년 남녀 고용률 격차(남자-여자, %p)", "PP", m["2024"] - f["2024"], 1, "남자 2024-여자 2024"),
        gold(f"{n}개 연령계층 중 {a}의 2024년 남녀 격차 순위(큰 순)", "RANK", rank_of(gap, a), 0, f"계 제외 {n}개 연령계층 2024년 (남자-여자) 내림차순, 동률 min"),
    ]
    aux = [
        gold("2023년 남녀 고용률 격차(남자-여자, %p)", "PP", m["2023"] - f["2023"], 1, "남자 2023-여자 2023"),
        gold("격차의 2023년 대비 2024년 변화(%p)", "PP", (m["2024"] - f["2024"]) - (m["2023"] - f["2023"]), 1, "격차 2024-격차 2023"),
        gold("2024년 남자 고용률의 여자 대비 배율(배)", "RATIO", m["2024"] / f["2024"], 2, "남자 2024/여자 2024"),
        gold(f"{a} 격차 순위(절댓값 기준)", "RANK", rank_of({k: abs(v) for k, v in gap.items()}, a), 0, f"계 제외 {n}개 연령계층 2024년 |남자-여자| 내림차순, 동률 min"),
    ]
    target = [(a, s, y) for y in yrs for s in ("남자", "여자")]
    question = (f"표의 '{a}' 행의 남자·여자 열(2023년, 2024년)을 사용하여 2023년 대비 2024년 변화를 설명하시오. 남자와 여자 각각에 대해 변화를 %p와 "
                f"상대 변화율(%)로 함께 제시하고, 2024년의 남녀 격차(남자-여자, %p)도 제시하시오. 또한 '계' 행을 제외한 {n}개 연령계층 전체의 2024년 남녀 격차를 "
                f"비교하여 {a}의 순위(격차가 큰 순, 1위가 가장 큼)를 제시하시오.")
    ranks = [("2024년 남녀 고용률 격차(남자-여자, %p)", "PP", 1, gap)]
    return emp_full_slice(tbl), uniq(target), question, req, aux, ranks


# ---------------------------------------------------------------- cpi
def cpi_col(tbl):
    return tbl.cols[0]


def cpi_items(tbl):
    return tbl.ok_rows([r for r in tbl.rows if r != "총지수"], [cpi_col(tbl)], YEARS)


def cpi_full_slice(tbl):
    col = cpi_col(tbl)
    rows = (["총지수"] if tbl.all_ok([("총지수", col, y) for y in YEARS]) else []) + cpi_items(tbl)
    return [(i, col, y) for i in rows for y in YEARS]


def cpi_infl(tbl, y1, y0):
    col = cpi_col(tbl)
    return {i: pct_change(tbl.dec((i, col, y1)), tbl.dec((i, col, y0))) for i in cpi_items(tbl)}


def tmpl_C1(tbl, rng, used):
    col = cpi_col(tbl)
    items = cpi_items(tbl)
    n = len(items)
    i = rng.choice([x for x in items if x not in used])
    used.add(i)
    v = {y: tbl.dec((i, col, y)) for y in YEARS}
    infl = cpi_infl(tbl, "2024", "2023")
    best = argmax(infl)
    req = [
        gold("2023년 전년 대비 상승률(%)", "PCT", pct_change(v["2023"], v["2022"]), 1, "(2023/2022-1)*100"),
        gold("2024년 전년 대비 상승률(%)", "PCT", infl[i], 1, "(2024/2023-1)*100"),
        gold("2022년 대비 2024년 누적 상승률(%)", "PCT", pct_change(v["2024"], v["2022"]), 1, "(2024/2022-1)*100"),
        gold(f"{n}개 지출목적 중 '{i}'의 2024년 전년 대비 상승률 순위(높은 순)", "RANK", rank_of(infl, i), 0, f"총지수 제외 {n}개 지출목적 2024년 상승률 내림차순, 동률 min"),
        gold(f"{n}개 지출목적 중 2024년 전년 대비 상승률이 가장 높은 항목의 상승률(%)", "PCT", infl[best], 1, f"max = {best}"),
    ]
    aux = [
        gold("2023년 지수 변화(포인트)", "INDEX", v["2023"] - v["2022"], 2, "2023-2022"),
        gold("2024년 지수 변화(포인트)", "INDEX", v["2024"] - v["2023"], 2, "2024-2023"),
        gold("2020년 대비 2024년 누적 상승률(%)", "PCT", v["2024"] - 100, 1, "2024-100"),
    ]
    target = [(i, col, y) for y in YEARS] + [(best, col, "2023"), (best, col, "2024")]
    question = (f"표의 '{i}' 행의 2022~2024년 지수를 사용하여 변화를 설명하시오. 2023년과 2024년의 전년 대비 상승률(%)과 2022년 대비 2024년의 누적 상승률(%)을 "
                f"제시하시오. 또한 '총지수' 행을 제외한 {n}개 지출목적 전체의 2024년 전년 대비 상승률을 비교하여 '{i}'의 순위(상승률이 높은 순, 1위가 가장 높음)와 "
                f"상승률이 가장 높은 항목의 상승률(%)을 제시하시오.")
    ranks = [("2024년 전년 대비 상승률(%)", "PCT", 1, infl)]
    return cpi_full_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_C2(tbl, rng, used):
    col = cpi_col(tbl)
    items = cpi_items(tbl)
    n = len(items)
    for _ in range(1000):
        sel = rng.sample(items, 3)
        sel = [i for i in items if i in sel]
        if tuple(sel) not in used:
            used.add(tuple(sel))
            break
    v = [tbl.dec((i, col, "2024")) for i in sel]
    idx24 = {i: tbl.dec((i, col, "2024")) for i in items}
    best = argmax(idx24)
    req = [gold(f"'{i}'의 2020년 대비 2024년 누적 상승률(%)", "PCT", v[j] - 100, 1, f"{i} 2024-100") for j, i in enumerate(sel)] + [
        gold(f"{n}개 지출목적 중 '{sel[0]}'의 2024년 지수 순위(높은 순)", "RANK", rank_of(idx24, sel[0]), 0, f"총지수 제외 {n}개 지출목적 2024년 지수 내림차순, 동률 min"),
        gold(f"{n}개 지출목적 중 2024년 지수가 가장 높은 항목의 2020년 대비 누적 상승률(%)", "PCT", idx24[best] - 100, 1, f"max = {best}; {best} 2024-100"),
    ]
    aux = [
        gold(f"'{sel[0]}'와 '{sel[1]}'의 누적 상승률 격차(%p)", "PP", v[0] - v[1], 1, f"{sel[0]}-{sel[1]}"),
        gold(f"'{sel[0]}'와 '{sel[2]}'의 누적 상승률 격차(%p)", "PP", v[0] - v[2], 1, f"{sel[0]}-{sel[2]}"),
        gold("3개 항목 지수 단순 평균(2020=100)", "INDEX", sum(v) / 3, 2, "합/3"),
    ]
    target = [(i, col, "2024") for i in sel] + [(best, col, "2024")]
    question = (f"표의 2024년 열에서 '{sel[0]}', '{sel[1]}', '{sel[2]}' 세 행의 지수(2020=100)를 사용하여 각 항목의 기준연도(2020년) 대비 누적 상승률(%)을 비교하시오. "
                f"또한 '총지수' 행을 제외한 {n}개 지출목적 전체의 2024년 지수를 비교하여 '{sel[0]}'의 순위(지수가 높은 순, 1위가 가장 높음)와 "
                f"지수가 가장 높은 항목의 2020년 대비 누적 상승률(%)을 제시하시오.")
    ranks = [("2024년 지수(2020=100)", "INDEX", 2, idx24)]
    return cpi_full_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_C3(tbl, rng, used):
    col = cpi_col(tbl)
    items = cpi_items(tbl)
    n = len(items)
    yrs = ["2023", "2024"]
    for _ in range(1000):
        a, b = rng.sample(items, 2)
        if (a, b) not in used:
            used.add((a, b))
            break
    va = {y: tbl.dec((a, col, y)) for y in yrs}
    vb = {y: tbl.dec((b, col, y)) for y in yrs}
    infl = cpi_infl(tbl, "2024", "2023")
    ra, rb = infl[a], infl[b]
    worst = argmin(infl)
    req = [
        gold(f"'{a}'의 2024년 전년 대비 상승률(%)", "PCT", ra, 1, f"({a} 2024/{a} 2023-1)*100"),
        gold(f"'{b}'의 2024년 전년 대비 상승률(%)", "PCT", rb, 1, f"({b} 2024/{b} 2023-1)*100"),
        gold(f"두 항목 상승률의 격차('{a}'-'{b}', %p)", "PP", ra - rb, 1, "상승률 a-상승률 b"),
        gold(f"2024년 두 항목의 지수 격차('{a}'-'{b}', 포인트)", "INDEX", va["2024"] - vb["2024"], 2, f"{a} 2024-{b} 2024"),
        gold(f"{n}개 지출목적 중 '{a}'의 2024년 전년 대비 상승률 순위(높은 순)", "RANK", rank_of(infl, a), 0, f"총지수 제외 {n}개 지출목적 2024년 상승률 내림차순, 동률 min"),
        gold(f"{n}개 지출목적 중 2024년 전년 대비 상승률이 가장 낮은 항목의 상승률(%)", "PCT", infl[worst], 1, f"min = {worst}"),
    ]
    aux = [
        gold(f"2023년 두 항목의 지수 격차('{a}'-'{b}', 포인트)", "INDEX", va["2023"] - vb["2023"], 2, f"{a} 2023-{b} 2023"),
        gold(f"'{a}'의 2024년 지수 변화(포인트)", "INDEX", va["2024"] - va["2023"], 2, f"{a} 2024-{a} 2023"),
        gold(f"'{b}'의 2024년 지수 변화(포인트)", "INDEX", vb["2024"] - vb["2023"], 2, f"{b} 2024-{b} 2023"),
    ]
    target = [(i, col, y) for i in (a, b) for y in yrs] + [(worst, col, y) for y in yrs]
    question = (f"표의 '{a}' 행과 '{b}' 행의 2023년·2024년 지수를 사용하여 2024년 상승률을 비교하시오. 각 항목의 전년 대비 상승률(%), 두 상승률의 격차(%p), "
                f"2024년 두 항목의 지수 격차(포인트)를 제시하시오. 또한 '총지수' 행을 제외한 {n}개 지출목적 전체의 2024년 전년 대비 상승률을 비교하여 "
                f"'{a}'의 순위(상승률이 높은 순, 1위가 가장 높음)와 상승률이 가장 낮은 항목의 상승률(%)을 제시하시오.")
    ranks = [("2024년 전년 대비 상승률(%)", "PCT", 1, infl)]
    return cpi_full_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_C4(tbl, rng, used):
    col = cpi_col(tbl)
    items = cpi_items(tbl)
    n = len(items)
    yrs = ["2023", "2024"]
    for _ in range(1000):
        sel = rng.sample(items, 5)
        sel = [i for i in items if i in sel]
        f = rng.choice(sel)
        if (tuple(sel), f) not in used:
            used.add((tuple(sel), f))
            break
    infl = cpi_infl(tbl, "2024", "2023")
    rates = [infl[i] for i in sel]
    rmax, rmin = max(rates), min(rates)
    imax, imin = sel[rates.index(rmax)], sel[rates.index(rmin)]
    best = argmax(infl)
    req = [
        gold("5개 항목 중 상승률이 가장 높은 항목의 2024년 전년 대비 상승률(%)", "PCT", rmax, 1, f"max5 = {imax}"),
        gold("5개 항목 중 상승률이 가장 낮은 항목의 2024년 전년 대비 상승률(%)", "PCT", rmin, 1, f"min5 = {imin}"),
        gold("5개 항목 중 최고 상승률과 최저 상승률의 격차(%p)", "PP", rmax - rmin, 1, f"{imax}-{imin}"),
        gold(f"{n}개 지출목적 중 '{f}'의 2024년 전년 대비 상승률 순위(높은 순)", "RANK", rank_of(infl, f), 0, f"총지수 제외 {n}개 지출목적 2024년 상승률 내림차순, 동률 min"),
        gold(f"{n}개 지출목적 중 2024년 전년 대비 상승률이 가장 높은 항목의 상승률(%)", "PCT", infl[best], 1, f"max = {best}"),
    ]
    aux = [
        gold(f"'{f}'의 2024년 전년 대비 상승률(%)", "PCT", infl[f], 1, f"({f} 2024/{f} 2023-1)*100"),
        gold("5개 항목 상승률 단순 평균(%)", "PCT", sum(rates) / 5, 1, "합/5"),
        gold(f"'{imax}'의 2020년 대비 2024년 누적 상승률(%)", "PCT", tbl.dec((imax, col, "2024")) - 100, 1, f"{imax} 2024-100"),
        gold(f"5개 항목 중 '{f}'의 2024년 상승률 순위(높은 순)", "RANK", rank_desc(rates, sel.index(f)), 0, "질문에 열거된 5개 항목 내 내림차순, 동률 min"),
    ]
    target = [(i, col, y) for i in sel for y in yrs] + [(best, col, y) for y in yrs]
    names = ", ".join(f"'{i}'" for i in sel)
    question = (f"표의 {names} 5개 행의 2023년·2024년 지수를 사용하여 2024년 전년 대비 상승률을 비교하시오. 이 5개 중 상승률이 가장 높은 항목과 가장 낮은 항목의 "
                f"상승률(%), 두 상승률의 격차(%p)를 제시하시오. 또한 '총지수' 행을 제외한 {n}개 지출목적 전체의 2024년 전년 대비 상승률을 비교하여 "
                f"'{f}'의 순위(상승률이 높은 순, 1위가 가장 높음)와 {n}개 중 상승률이 가장 높은 항목의 상승률(%)을 제시하시오.")
    ranks = [("2024년 전년 대비 상승률(%)", "PCT", 1, infl)]
    return cpi_full_slice(tbl), uniq(target), question, req, aux, ranks


def tmpl_C5(tbl, rng, used):
    col = cpi_col(tbl)
    items = cpi_items(tbl)
    n = len(items)
    yrs = ["2022", "2024"]
    if not tbl.all_ok([("총지수", col, y) for y in yrs]):
        raise RuntimeError("C5: 총지수 셀 사용 불가")
    for _ in range(1000):
        a, b = rng.sample(items, 2)
        a, b = [i for i in items if i in (a, b)]
        if (a, b) not in used:
            used.add((a, b))
            break
    t = {y: tbl.dec(("총지수", col, y)) for y in yrs}
    va = {y: tbl.dec((a, col, y)) for y in yrs}
    vb = {y: tbl.dec((b, col, y)) for y in yrs}
    cum = cpi_infl(tbl, "2024", "2022")
    rt, ra, rb = pct_change(t["2024"], t["2022"]), cum[a], cum[b]
    req = [
        gold("총지수의 2022년 대비 2024년 누적 상승률(%)", "PCT", rt, 1, "(총지수 2024/총지수 2022-1)*100"),
        gold(f"'{a}'의 2022년 대비 2024년 누적 상승률(%)", "PCT", ra, 1, f"({a} 2024/{a} 2022-1)*100"),
        gold(f"'{b}'의 2022년 대비 2024년 누적 상승률(%)", "PCT", rb, 1, f"({b} 2024/{b} 2022-1)*100"),
        gold(f"'{a}' 누적 상승률의 총지수 대비 격차(%p)", "PP", ra - rt, 1, "상승률 a-상승률 총지수"),
        gold(f"'{b}' 누적 상승률의 총지수 대비 격차(%p)", "PP", rb - rt, 1, "상승률 b-상승률 총지수"),
        gold(f"{n}개 지출목적 중 '{a}'의 2022년 대비 2024년 누적 상승률 순위(높은 순)", "RANK", rank_of(cum, a), 0, f"총지수 제외 {n}개 지출목적 2022→2024 상승률 내림차순, 동률 min"),
    ]
    aux = [
        gold("총지수의 2020년 대비 2024년 누적 상승률(%)", "PCT", t["2024"] - 100, 1, "총지수 2024-100"),
        gold(f"'{a}'의 2020년 대비 2024년 누적 상승률(%)", "PCT", va["2024"] - 100, 1, f"{a} 2024-100"),
        gold(f"'{b}'의 2020년 대비 2024년 누적 상승률(%)", "PCT", vb["2024"] - 100, 1, f"{b} 2024-100"),
        gold("총지수의 2022년 대비 2024년 지수 변화(포인트)", "INDEX", t["2024"] - t["2022"], 2, "총지수 2024-총지수 2022"),
    ]
    target = [("총지수", col, y) for y in yrs] + [(i, col, y) for i in (a, b) for y in yrs]
    question = (f"표의 '총지수', '{a}', '{b}' 세 행의 2022년·2024년 지수를 사용하여 2022년 대비 2024년 변화를 비교하시오. 세 지수 각각의 누적 상승률(%)과 "
                f"총지수 상승률 대비 두 항목 상승률의 격차(%p)를 제시하시오. 또한 '총지수' 행을 제외한 {n}개 지출목적 전체의 2022년 대비 2024년 누적 상승률을 비교하여 "
                f"'{a}'의 순위(상승률이 높은 순, 1위가 가장 높음)를 제시하시오.")
    ranks = [("2022년 대비 2024년 누적 상승률(%)", "PCT", 1, cum)]
    return cpi_full_slice(tbl), uniq(target), question, req, aux, ranks


# ---------------------------------------------------------------- gold_pool (표의 모든 행에 대한 정당한 파생값, 상한 없음)
def build_gold_pool(tbl, ctx_keys, ranks, target_rows=()):
    """context_markdown 에 보이는 모든 행(전국/계/총지수 포함)에 대해 모델이 정당하게 계산할 수 있는 값을 모두 나열한다.
    (a) 연속 연도 쌍 증감률(PCT, 1자리) + 첫→끝 누적 증감률
    (b) 연속 연도 절대 차이(pop COUNT / cpi INDEX / emp PP) + 첫→끝 차이  [display 변형 포함]
    (c) 성별 표(P2, emp): 남−여 격차(COUNT/PP), 여/남×100·남/여×100(PCT), 남/여 배율(RATIO 2자리), 성비(여성 100명당 남성, COUNT 1자리; pop 만)
    (d) cpi: 각 행·연도 idx−100(PCT)
    (e) 문항의 순위 기준에 따른 각 행의 순위(RANK, '<row> 순위') + 기준값 + 기준의 최대·최소
    항목 스키마는 gold 와 같고 `row` 필드를 더한다. basis 는 exact(차이값의 display 변형만 display)."""
    unit = tbl.unit
    ctype = unit_type(unit)
    diff_type = {"COUNT": "COUNT", "INDEX": "INDEX", "PCT": "PP"}[ctype]
    diff_dec = {"COUNT": 0, "INDEX": 2, "PCT": 1}[ctype]
    pool = []
    seen = set()

    def add(item, row):
        item = dict(item)
        item["row"] = row
        key = (item["row"], item["type"], round(item["exact"], 6))
        if key in seen:
            return
        seen.add(key)
        pool.append(item)

    def add_diff(name, typ, a, b, dec, formula, row):
        base = gold(name, typ, a - b, dec, formula, "exact")
        add(base, row)
        # 셀값은 display == exact 이므로 보통 같지만, 규칙 일관성을 위해 표시값(반올림값) 차이가 다르면 display 변형을 넣는다
        d_item = gold(name, typ, q(a, dec) - q(b, dec), dec, formula + " [display 기준]", "display")
        add(d_item, row)  # exact 가 같으면 중복 제거됨

    rows = list(dict.fromkeys(k[0] for k in ctx_keys))
    cols = list(dict.fromkeys(k[1] for k in ctx_keys))
    periods = sorted(set(k[2] for k in ctx_keys))
    unit_word = {"COUNT": "명", "INDEX": "포인트", "PP": "%p"}[diff_type]
    for r in rows:
        for c in cols:
            ys = [y for y in periods if (r, c, y) in tbl.cells and tbl.ok((r, c, y))]
            lab = f"{r} {c}" if len(cols) > 1 else r
            pairs = [(ys[i], ys[i + 1]) for i in range(len(ys) - 1)]
            if len(ys) > 2:
                pairs.append((ys[0], ys[-1]))
            for y0, y1 in pairs:
                v0, v1 = tbl.dec((r, c, y0)), tbl.dec((r, c, y1))
                tag = "전년 대비" if int(y1) - int(y0) == 1 else "누적"
                if v0 != 0:
                    add(gold(f"{lab} {y0}→{y1} {tag} 증감률(%)", "PCT", pct_change(v1, v0), 1, f"({lab} {y1}/{lab} {y0}-1)*100"), r)
                add_diff(f"{lab} {y0}→{y1} {tag} 차이({unit_word})", diff_type, v1, v0, diff_dec, f"{lab} {y1}-{lab} {y0}", r)
            if tbl.key == "cpi":
                for y in ys:
                    add(gold(f"{lab} {y}년 2020년 대비 누적 상승률(%)", "PCT", tbl.dec((r, c, y)) - 100, 1, f"{lab} {y}-100"), r)
        mc = "남자인구수" if "남자인구수" in cols else ("남자" if "남자" in cols else None)
        fc = "여자인구수" if "여자인구수" in cols else ("여자" if "여자" in cols else None)
        if mc and fc:
            for y in periods:
                if not (tbl.ok((r, mc, y)) and tbl.ok((r, fc, y))):
                    continue
                m, f = tbl.dec((r, mc, y)), tbl.dec((r, fc, y))
                gtyp = "COUNT" if ctype == "COUNT" else "PP"
                gdec = 0 if ctype == "COUNT" else 1
                add_diff(f"{r} {y}년 남녀 격차(남자-여자, {'명' if gtyp == 'COUNT' else '%p'})", gtyp, m, f, gdec, f"{r} 남자 {y}-여자 {y}", r)
                if m != 0 and f != 0:
                    add(gold(f"{r} {y}년 여자/남자 비율(%)", "PCT", f / m * 100, 1, f"{r} 여자 {y}/남자 {y}*100"), r)
                    add(gold(f"{r} {y}년 남자/여자 비율(%)", "PCT", m / f * 100, 1, f"{r} 남자 {y}/여자 {y}*100"), r)
                    add(gold(f"{r} {y}년 남자/여자 배율(배)", "RATIO", m / f, 2, f"{r} 남자 {y}/여자 {y}"), r)
                    add(gold(f"{r} {y}년 여자/남자 배율(배)", "RATIO", f / m, 2, f"{r} 여자 {y}/남자 {y}"), r)
                    if ctype == "COUNT":
                        add(gold(f"{r} {y}년 성비(여성 100명당 남성 수)", "COUNT", m / f * 100, 1, f"{r} 남자 {y}/여자 {y}*100"), r)
                        add(gold(f"{r} {y}년 남자 비중(%)", "PCT", m / (m + f) * 100, 1, f"{r} 남자 {y}/(남자+여자)*100"), r)
                        add(gold(f"{r} {y}년 여자 비중(%)", "PCT", f / (m + f) * 100, 1, f"{r} 여자 {y}/(남자+여자)*100"), r)
                        add(gold(f"{r} {y}년 남녀 합계(명)", "COUNT", m + f, 0, f"{r} 남자 {y}+여자 {y}"), r)
    for crit_name, ctyp, cdec, values in ranks:
        absvals = {k: abs(v) for k, v in values.items()}
        for r, v in values.items():
            add(gold(f"{r} 순위", "RANK", rank_of(values, r), 0, f"기준: {crit_name}, 내림차순 동률 min"), r)
            add(gold(f"{r} {crit_name}", ctyp, v, cdec, f"순위 기준값: {crit_name}"), r)
            if ctyp == "PP":
                add(gold(f"{r} 격차 순위(절댓값 기준)", "RANK", rank_of(absvals, r), 0, f"기준: |{crit_name}|, 내림차순 동률 min"), r)
        if values:
            kmax, kmin = argmax(values), argmin(values)
            add(gold(f"{crit_name} 최대값", ctyp, values[kmax], cdec, f"max = {kmax}"), kmax)
            add(gold(f"{crit_name} 최소값", ctyp, values[kmin], cdec, f"min = {kmin}"), kmin)
        if ctyp == "COUNT":
            # 순위 이웃(k 위 vs k±1 위) 간 인원 차이 — 대상 지역이 쌍에 있으면 그 지역에 귀속
            order = sorted(values, key=lambda k: values[k], reverse=True)
            for i in range(len(order) - 1):
                hi, lo = order[i], order[i + 1]
                row = hi if hi in target_rows else (lo if lo in target_rows else hi)
                add_diff(f"{hi}({i + 1}위)-{lo}({i + 2}위) {crit_name} 차이(명)", "COUNT", values[hi], values[lo], cdec, f"{hi}-{lo}", row)
                add(gold(f"{hi}({i + 1}위)/{lo}({i + 2}위) {crit_name} 배율(배)", "RATIO", values[hi] / values[lo], 2, f"{hi}/{lo}"), row)
    return pool


TEMPLATES = {
    "P1": tmpl_P1, "P2": tmpl_P2, "P3": tmpl_P3, "P4": tmpl_P4, "P5": tmpl_P5,
    "E1": tmpl_E1, "E2": tmpl_E2, "E3": tmpl_E3, "E4": tmpl_E4, "E5": tmpl_E5,
    "C1": tmpl_C1, "C2": tmpl_C2, "C3": tmpl_C3, "C4": tmpl_C4, "C5": tmpl_C5,
}


# ---------------------------------------------------------------- 조립
def gold_hash(gold_required, context_cells):
    payload = json.dumps({"gold_required": gold_required, "context_cells": context_cells},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_tables_cfg(data_dir):
    with open(Path(data_dir) / "tables.json", encoding="utf-8") as f:
        return json.load(f)


def load_cells(data_dir, key, cfg):
    df = pd.read_csv(Path(data_dir) / f"{key}.csv", encoding="utf-8-sig", dtype=str, keep_default_na=False)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["flag"] = df["flag"].fillna("")
    return Table(key, cfg["tables"][key], df)


def build_all(data_dir, cfg=None):
    data_dir = Path(data_dir)
    cfg = cfg or load_tables_cfg(data_dir)
    tables = {k: load_cells(data_dir, k, cfg) for k in ("pop", "emp", "cpi")}
    rng = random.Random(SEED)
    rows = []
    for table_key, tid, n in TEMPLATE_PLAN:
        tbl = tables[table_key]
        tcfg = tbl.cfg
        used = set()
        for k in range(1, n + 1):
            ctx_keys, target_keys, question, req, aux, ranks = TEMPLATES[tid](tbl, rng, used)
            assert 3 <= len(req) <= 6, (tid, len(req))
            assert 2 <= len(aux) <= 9, (tid, len(aux))
            assert tbl.all_ok(ctx_keys), (tid, "flagged cell in context")
            assert set(target_keys) <= set(ctx_keys), (tid, "target ⊄ context")
            assert any(g["type"] == "RANK" for g in req), (tid, "no table-wide RANK")
            cells = [tbl.cell(kk) for kk in ctx_keys]
            tcells = [tbl.cell(kk) for kk in target_keys]
            aux = expand_aux(req, aux, tcells, tbl.unit)
            assert 2 <= len(aux) <= AUX_CAP, (tid, len(aux))
            recs = [tbl.cells[kk] for kk in ctx_keys]
            periods = sorted(set(c["period"] for c in cells))
            obj_codes = {}
            for oi, obj in enumerate(tcfg["objL"], 1):
                col = f"c{oi}"
                order = [x["code"] for x in obj["codes"]]
                obj_codes[obj["param"]] = sorted(set(str(r[col]) for r in recs), key=lambda c: order.index(c) if c in order else 999)
            itm_ids = sorted(set(str(r["itm_id"]) for r in recs), key=lambda c: tcfg["itmIds"].index(c) if c in tcfg["itmIds"] else 999)
            fetch_date = recs[0]["fetch_date"]
            lst_chn_de = max(str(r["lst_chn_de"]) for r in recs)
            md = markdown_table(tbl, ctx_keys, tcfg["layout"]["row_header"])
            assert len(md) <= MAX_MD_CHARS, (tid, len(md))
            pool = build_gold_pool(tbl, ctx_keys, ranks, target_rows=[k[0] for k in target_keys if k[0] not in ("전국", "계", "총지수")])
            row = {
                "qid": f"{table_key}_{tid}_{k}",
                "table_key": table_key,
                "template_id": tid,
                "org_id": tcfg["orgId"],
                "tbl_id": tcfg["tblId"],
                "itm_id": "+".join(itm_ids),
                "obj_codes": json.dumps(obj_codes, ensure_ascii=False),
                "prd_se": tcfg["prdSe"],
                "periods": json.dumps(periods, ensure_ascii=False),
                "unit_nm": tbl.unit,
                "fetch_date": fetch_date,
                "lst_chn_de": lst_chn_de,
                "source_citation": tcfg["source_citation"].replace("{fetch_date}", fetch_date),
                "table_title_ko": tcfg["table_title_ko"],
                "context_cells": json.dumps(cells, ensure_ascii=False),
                "target_cells": json.dumps(tcells, ensure_ascii=False),
                "context_markdown": md,
                "question_ko": question,
                "required_list_text": required_list_text(req),
                "gold_required": json.dumps(req, ensure_ascii=False),
                "gold_aux": json.dumps(aux, ensure_ascii=False),
                "rounding_rule_text": ROUNDING_RULE,
                "gold_hash": gold_hash(req, cells),
                "gold_pool": json.dumps(pool, ensure_ascii=False),
            }
            rows.append(row)
    return pd.DataFrame(rows, columns=CSV_COLUMNS)


def write_preview(df, path):
    md_len = df["context_markdown"].str.len()
    lines = ["# questions_preview — 템플릿별 예시 1건 (사람 검토용)  [v2 난이도 상향판: 표 전체 슬라이스 + 표 전체 순위/극값]", "",
             f"총 {len(df)}문항: " + ", ".join(f"{k} {v}" for k, v in df["table_key"].value_counts().reindex(['pop', 'emp', 'cpi']).items()),
             f"; context_cells 수: " + ", ".join(f"{t} {sorted(set(df[df.table_key == t]['context_cells'].map(lambda x: len(json.loads(x)))))}" for t in ('pop', 'emp', 'cpi')),
             f"; context_markdown 길이 최대 {md_len.max()}자 / 평균 {md_len.mean():.0f}자", ""]
    for tid in [t[1] for t in TEMPLATE_PLAN]:
        r = df[df["template_id"] == tid].iloc[0]
        cells, tcells = json.loads(r["context_cells"]), json.loads(r["target_cells"])
        lines += [f"## {tid} — {r['qid']} ({r['table_title_ko']})", "",
                  f"**출처**: {r['source_citation']} (LST_CHN_DE {r['lst_chn_de']}) · context {len(cells)}셀 / target {len(tcells)}셀 / markdown {len(r['context_markdown'])}자", "",
                  "**[표] 프롬프트에 들어가는 셀 전부**", "", r["context_markdown"], "",
                  f"**[질문]** {r['question_ko']}", "",
                  f"**[필수 포함 값]** {r['required_list_text']}", "",
                  "**target_cells** (gold 계산에 쓰인 셀): " + ", ".join(f"{c['row_label']}/{c['col_label']}/{c['period']}={c['dt_raw']}" for c in tcells), "",
                  "**gold_required** (채점 전용, 프롬프트에 넣지 않음)", "",
                  "| # | 이름 | type | display | exact | sign | formula |", "|---|---|---|---|---|---|---|"]
        for i, g in enumerate(json.loads(r["gold_required"]), 1):
            lines.append(f"| {i} | {g['name']} | {g['type']} | {g['display']} | {g['exact']:.6g} | {g['sign']} | {g['formula']} |")
        lines += ["", "**gold_aux**", "", "| 이름 | type | display | basis | formula |", "|---|---|---|---|---|"]
        for g in json.loads(r["gold_aux"]):
            lines.append(f"| {g['name']} | {g['type']} | {g['display']} | {g['basis']} | {g['formula']} |")
        lines += ["", f"gold_hash: `{r['gold_hash'][:16]}…`", ""]
    lines += ["## 전체 문항 목록", "", "| qid | 템플릿 | context 셀 | target 셀 | 필수 값 | aux | 질문(앞 70자) |", "|---|---|---|---|---|---|---|"]
    for _, r in df.iterrows():
        lines.append(f"| {r['qid']} | {r['template_id']} | {len(json.loads(r['context_cells']))} | {len(json.loads(r['target_cells']))} | "
                     f"{len(json.loads(r['gold_required']))} | {len(json.loads(r['gold_aux']))} | {r['question_ko'][:70]}… |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(HERE))
    ap.add_argument("--out", default=None)
    ap.add_argument("--preview", default=None)
    a = ap.parse_args(argv)
    out = Path(a.out) if a.out else Path(a.data_dir) / "questions.csv"
    preview = Path(a.preview) if a.preview else Path(a.data_dir) / "questions_preview.md"
    df = build_all(a.data_dir)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    write_preview(df, preview)
    counts = df["table_key"].value_counts().to_dict()
    print(f"questions: {len(df)} rows ({counts}) -> {out}")
    print(f"context_markdown max {df['context_markdown'].str.len().max()} chars; preview -> {preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
