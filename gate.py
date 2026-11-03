#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gate.py — 피벗/직접 무결성 게이트 (SPEC §6, 검토보고 1-6).

검사(두 조건 모두):
  (1) format_ok : ===ANALYSIS=== / ===NARRATIVE=== / ===END=== 세 마커가 이 순서로 존재
  (2) lang_ok   : ANALYSIS 절 문자 중 지정 언어 비율 >= 0.90
                  pivot: latin/(latin+hangul), direct: hangul/(latin+hangul). 숫자·기호 제외.
                  표의 행/열 라벨·제목 문자열, 단위 약어(%p, p.p., CPI, KOSIS …), 마커 단어는 제거 후 계산.
  (3) num_ok    : ANALYSIS 절의 COUNT/INDEX 숫자(단위 없는 수, 명/원/천명/만명, 만·억 합성,
                  영문 9,386,034 / 9.39 million / thousand) 가 context_cells ∪ gold_aux ∪ gold_required
                  허용오차 내 비율 >= --num-thresh (기본 0.90, 검토보고 1-6; 1.0 = SPEC "전부") 이고,
                  근사어(약·about)가 붙은 수는 max(허용오차, 1%·|gold|). 연령 라벨(30 - 39세, 60세이상, ages 30-39, 20대)
                  과 1~2자리 구간(15-19), 순위·개수 마커(17위, #3, 2nd of 17, 17개, 12 categories, 표 첫 칸 "| 11 |"),
                  1..max(10, 행 수) 범위의 맨 정수는 숫자로 보지 않음. 표의 행/열 라벨·제목 문자열은 언어 비율·숫자 검사
                  전에 제거. 선택 열 gold_pool(행별 파생값 전체) 은 풀에 합산, target_cells → 부수 지표 target_restated.
                  context_cells 중 최소 1개가 재진술됨.
부수: en_leak_ratio (NARRATIVE 절 라틴 문자 비율, 화이트리스트 제외), analysis_len_chars.
분석은 ITT, 게이트 통과 부분집합은 민감도용.

사용: py -3.11 gate.py [--results results/results.jsonl] [--questions data/questions.csv]
                       [--out results/gate.csv] [--num-thresh 1.0] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DEFAULT_RESULTS = HERE / "results" / "results.jsonl"
DEFAULT_QUESTIONS = HERE / "data" / "questions.csv"
DEFAULT_OUT = HERE / "results" / "gate.csv"

LANG_THRESH = 0.90
MARK_RE = re.compile(r"===ANALYSIS===(.*?)===NARRATIVE===(.*?)===END===", re.S)
HANGUL_RE = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
LATIN_RE = re.compile(r"[A-Za-z]")
# NARRATIVE 영어 혼입률 계산에서 제외할 단위·약어
EN_WHITELIST = [r"%\s*[pP](?![A-Za-z])", r"\bp\.p\.", r"\bpp\b", r"\bCPI\b", r"\bKOSIS\b", r"\bGDP\b",
                r"\bOECD\b", r"\bIMF\b", r"\bUN\b", r"\bWHO\b", r"\bILO\b", r"\bNA\b", r"\bN/A\b"]
# 연령 라벨 (KOSIS "30 - 39세", "60세이상", "30~39세", "20대", "ages 30-39", "aged 60 and over", "age 60+", "30–39 years")
AGE_PATTERNS = [   # 공통: 앞에 숫자·소수점·쉼표가 없고(37.9 의 9 보호), 구간 안은 줄바꿈 없이 공백만
    r"(?<![\d.,])\d{1,3}[ ]*[-–−~][ ]*\d{1,3}[ ]*세(?:[ ]*(?:이상|미만|이하))?",
    r"(?<![\d.,])\d{1,3}[ ]*세(?:[ ]*(?:이상|미만|이하))?",
    r"(?<![\d.,])\d{1,2}대(?!비)",
    r"\b(?:ages?d?|years?)[ ]*(?:of[ ]*)?\d{1,3}[ ]*(?:[-–−~]|to|and)[ ]*(?:\d{1,3}|over|older|above|under|below|up)",
    r"\b(?:ages?d?)[ ]*\d{1,3}[ ]*\+?",
    r"(?<![\d.,])\d{1,3}[ ]*[-–−~][ ]*\d{1,3}[ ]*(?:years?|yrs|year-olds?|and[ ]+over|and[ ]+older|or[ ]+older|\+(?![ ]*[\d(]))",
    r"(?<![\d.,])\d{1,3}(?:[ ]*(?:and[ ]+over|and[ ]+older|or[ ]+older|years?[ ]+old)|\+(?![ ]*\d))",   # 60+ (붙여 쓴 +만)
    r"(?<![\d.,])\b\d{1,2}[ ]*[-–~][ ]*\d{1,2}\b(?![.,]?\d)",     # 1~2자리 구간 (15-19, 40–49) — 연령 구간으로 간주
]
AGE_RE = re.compile("|".join(AGE_PATTERNS), re.I)
APPROX_RE = re.compile(r"(?:약|대략|거의|가량|정도|쯤|안팎|남짓|about|approx(?:imately)?|roughly|around|nearly|some|circa|~)\s*$", re.I)
EN_WHITELIST_RE = re.compile("|".join(EN_WHITELIST))
MARKER_WORD_RE = re.compile(r"={2,}\s*(?:ANALYSIS|NARRATIVE|END)\s*={2,}|(?:ANALYSIS|NARRATIVE)")

SIGN = r"(?:[-−–]\s?)?"
NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+"   # 쉼표는 3자리 묶음일 때만
KMULT = r"억|천만|백만|십만|만|천"
KMULT_VAL = {"억": 1e8, "천만": 1e7, "백만": 1e6, "십만": 1e5, "만": 1e4, "천": 1e3}
# 숫자 뒤 단위 → 타입 판정. PCT/PP/RATIO 는 게이트 대상 아님(채점기 영역).
PP_UNIT = r"%\s*[pP]\b|%\s*포인트|퍼센트\s*포인트|percentage\s+points?|\bp\.p\.|\bpp\b|\bpps?\b|포인트"
PCT_UNIT = r"%|퍼센트|프로\b|percent\b|pct\b"
RATIO_UNIT = r"배\b|times\b|x\b|fold\b"
RANK_UNIT = r"위\b|번째|th\b|st\b|nd\b|rd\b"
EXCL_UNIT = r"년|월|분기|세\b|개\b|곳\b|종\b|가지|단계|번\b|회\b|차례|시|분\b|일\b|차\b|년도|year|month|quarter|age"
COUNT_UNIT = r"천\s*명|만\s*명|억\s*원|만\s*원|천\s*원|명|원|people|persons|won|KRW"
EN_SCALE = r"million|billion|thousand|trillion|mn\b|bn\b|k\b"

TOKEN_RE = re.compile(
    rf"""
    (?P<pre>^|[^\d.,])
    (?P<sign>{SIGN})
    (?:
      (?P<comp>(?:(?:{NUM})\s*(?:{KMULT})\s*)+(?:\d{{1,3}}(?:,\d{{3}})*|\d+)?)
      |
      (?P<num>{NUM})
    )
    \s*(?P<unit>{EN_SCALE}|{PP_UNIT}|{PCT_UNIT}|{RATIO_UNIT}|{RANK_UNIT}|{EXCL_UNIT}|{COUNT_UNIT})?
    """,
    re.X | re.I | re.M,
)
# 순위·개수 마커: 17위, #3, 2nd of 17, ranks 10th, 17개, 12 categories, 17 provinces/regions/시도 — 숫자가 아님
RANK_PATTERNS = [
    r"(?<![\d.,])\d{1,3}\s*(?:위|번째|등)",
    r"#\s*\d{1,3}\b",
    r"\b\d{1,3}(?:st|nd|rd|th)\b(?:\s+(?:of|out\s+of)\s+\d{1,3}\b)?",
    r"\b(?:ranks?|ranked|ranking|place|position)\s*(?:#|no\.?)?\s*\d{1,3}\b(?:\s+(?:of|out\s+of)\s+\d{1,3}\b)?",
    r"\b(?:of|out\s+of|among(?:\s+the)?|all|across(?:\s+the)?)\s+\d{1,3}\s+(?:provinces?|regions?|categories|categor(?:y|ies)|cities|items?|groups?|rows?|areas?|sido|시도|시·도|지역|항목|품목|부문)",
    r"(?<![\d.,])\d{1,3}\s*(?:개|곳|종|가지|개\s*시도|개\s*지역|개\s*항목|개\s*품목)",
    r"(?<![\d.,])\d{1,3}\s+(?:provinces?|regions?|categories|cities|items?|groups?|rows?|areas?|sido|expenditure\s+categories|age\s+groups?|시도|지역|항목|품목|부문)\b",
    r"^\s*[*#>\-|]*\s*\(?\d{1,3}\)\s",            # 줄머리 "1)" "(1)"
    r"^\s*\|\s*\d{1,3}\s*\|",                      # 마크다운 표 첫 칸의 순위 번호 "| 11 |"
]
RANK_RE = re.compile("|".join(RANK_PATTERNS), re.I | re.M)
LIST_RE = re.compile(r"^\s*[*#>\-]*\s*(?:step|단계)?\s*\d{1,2}\s*[.)]\s", re.I | re.M)
BRACKET_REF_RE = re.compile(r"\[\d+\]")
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def _decimals(s: str) -> int:
    s = s.replace(",", "")
    return len(s.split(".", 1)[1]) if "." in s else 0


def strip_labels(text: str, labels) -> str:
    """표의 행/열 라벨·제목 문자열을 공백으로 치환 (고유명사가 언어 비율·숫자 검사에 들어가지 않게)."""
    for lab in sorted({unicodedata.normalize("NFKC", str(x)).strip() for x in labels if x}, key=len, reverse=True):
        if len(lab) < 2 or re.fullmatch(r"[\d.,\s%]+", lab):
            continue
        text = text.replace(lab, " " * len(lab))
    return text


def extract_count_index(text: str, n_rows: int = 0, labels=()) -> list[dict]:
    """ANALYSIS 절에서 COUNT/INDEX 후보 숫자 추출.
    반환 [{span, value, tol, raw, approx}] — tol 은 모델이 쓴 해상도의 절반(0.5×10^-d, 합성·scale 반영).
    n_rows: 표의 행 수 — 1..max(10, n_rows) 범위의 맨 정수(쉼표·소수 없음)는 순위/목록/행수 상수로 보고 제외."""
    text = unicodedata.normalize("NFKC", text)
    text = strip_labels(text, labels)
    text = AGE_RE.sub(lambda m: " " * len(m.group(0)), text)         # 연령 라벨 (숫자 아님)
    text = RANK_RE.sub(lambda m: " " * len(m.group(0)), text)        # 순위·개수 마커
    text = LIST_RE.sub(lambda m: " " * len(m.group(0)), text)        # 목록 번호 / Step n
    small_int_max = max(10, int(n_rows or 0))
    text = BRACKET_REF_RE.sub(lambda m: " " * len(m.group(0)), text)
    out = []
    for m in TOKEN_RE.finditer(text):
        unit = (m.group("unit") or "").lower()
        span = m.group(0)[len(m.group("pre")):].strip()
        approx = bool(APPROX_RE.search(text[max(0, m.start() - 12):m.start() + len(m.group("pre"))]))
        if m.group("comp"):
            # 만/억/천 합성 (예: 938만 6,034명, 943.4만명, 1억 2천만원). 해상도 = 마지막 단위어/10^d, 끝자리 수가 있으면 1
            if unit and re.fullmatch(PP_UNIT + "|" + PCT_UNIT + "|" + RATIO_UNIT + "|" + RANK_UNIT + "|" + EXCL_UNIT, unit, re.I):
                continue
            v, res = 0.0, 1.0
            comp = m.group("comp")
            for cm in re.finditer(rf"({NUM})\s*({KMULT})", comp):
                v += _to_float(cm.group(1)) * KMULT_VAL[cm.group(2)]
                res = KMULT_VAL[cm.group(2)] / 10 ** _decimals(cm.group(1))
            tail = re.search(rf"(?:{KMULT})\s*(\d{{1,3}}(?:,\d{{3}})*|\d+)\s*$", comp)
            if tail:
                v += _to_float(tail.group(1))
                res = 1.0
            out.append({"span": span, "value": v, "tol": 0.5 * res, "raw": m.group(0), "approx": approx})
            continue
        raw = m.group("num")
        if not raw:
            continue
        if unit and re.fullmatch(PP_UNIT + "|" + PCT_UNIT + "|" + RATIO_UNIT + "|" + RANK_UNIT + "|" + EXCL_UNIT, unit, re.I):
            continue
        d = _decimals(raw)
        v = _to_float(raw)
        res = 10.0 ** (-d)
        scale = 1.0
        if unit:
            u = unit.replace(" ", "")
            if re.fullmatch(EN_SCALE, unit, re.I):
                scale = {"million": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9, "thousand": 1e3, "k": 1e3,
                         "trillion": 1e12}[unit]
            elif u in ("천명", "천원"):
                scale = 1e3
            elif u in ("만명", "만원"):
                scale = 1e4
            elif u == "억원":
                scale = 1e8
        if scale == 1.0:
            # 중간 계산값·상수 제외: 소수 3자리 이상(0.02321 등), 연도, 0~10 정수, 100/1000 상수
            if d >= 3:
                continue
            if YEAR_RE.match(raw.replace(",", "")) and d == 0:
                continue
            if d == 0 and "," not in raw and (v <= small_int_max or v in (100.0, 1000.0)):
                continue
        out.append({"span": span, "value": v * scale, "tol": 0.5 * res * scale, "raw": m.group(0), "approx": approx})
    return out


def char_ratio(text: str, target: str) -> tuple[float, int, int]:
    h, l = len(HANGUL_RE.findall(text)), len(LATIN_RE.findall(text))
    den = h + l
    if den == 0:
        return float("nan"), h, l
    return (l / den if target == "latin" else h / den), h, l


def split_sections(resp: str) -> tuple[bool, str, str]:
    """(format_ok, analysis, narrative). 마커 실패 시 best-effort 분리 (ITT: 전체를 NARRATIVE 로)."""
    if not isinstance(resp, str):
        return False, "", ""
    m = MARK_RE.search(resp)
    if m:
        return True, m.group(1), m.group(2)
    a, n = resp.find("===ANALYSIS==="), resp.find("===NARRATIVE===")
    if a >= 0 and n > a:
        return False, resp[a + len("===ANALYSIS==="):n], resp[n + len("===NARRATIVE==="):]
    if a >= 0:
        return False, resp[a + len("===ANALYSIS==="):], resp
    return False, "", resp


def _cell_values(js) -> list[float]:
    try:
        cells = json.loads(js) if js else []
    except (json.JSONDecodeError, TypeError):
        return []
    return [float(c["value"]) for c in cells if c.get("value") not in (None, "")]


def load_gold_pool(qrow) -> dict:
    """{cell_vals, pool, target_vals, labels, n_rows}.
    풀 = context_cells ∪ gold_aux ∪ gold_required ∪ gold_pool(선택 열: 행별 파생값 전체, 숫자 또는 {exact|value} dict).
    target_cells(선택 열)는 부수 지표 target_restated 에만 사용."""
    try:
        cells = json.loads(qrow.get("context_cells") or "[]")
    except json.JSONDecodeError:
        cells = []
    cell_vals = [float(c["value"]) for c in cells if c.get("value") not in (None, "")]
    target_vals = _cell_values(qrow.get("target_cells"))
    labels = [qrow.get("table_title_ko")] + [c.get("row_label") for c in cells] + [c.get("col_label") for c in cells]
    n_rows = len({c.get("row_label") for c in cells})
    pool = list(cell_vals)
    for col in ("gold_aux", "gold_required", "gold_pool"):
        try:
            for g in json.loads(qrow[col]) if qrow.get(col) else []:
                if isinstance(g, dict):
                    x = g.get("exact", g.get("value"))
                else:
                    x = g
                if x not in (None, ""):
                    pool.append(float(x))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"cell_vals": cell_vals, "pool": pool, "target_vals": target_vals, "labels": labels, "n_rows": n_rows}


def _within(v: float, g: float, tol: float, approx: bool) -> bool:
    t = max(tol, 0.01 * abs(g)) if approx else tol      # 근사어(약·about) → max(tol, 1%·|gold|)
    return abs(v - abs(g)) <= t


def check_numbers(analysis: str, cell_vals, pool, target_vals=(), n_rows=0, labels=()) -> dict:
    cands = extract_count_index(analysis, n_rows=n_rows, labels=labels)
    matched, cell_hit, target_hit, unmatched = 0, False, False, []
    for c in cands:
        v, tol, ap = abs(c["value"]), c["tol"], c.get("approx", False)
        ok_cell = any(_within(v, g, tol, ap) for g in cell_vals)
        ok_pool = ok_cell or any(_within(v, g, tol, ap) for g in pool)
        cell_hit = cell_hit or ok_cell
        target_hit = target_hit or any(_within(v, g, tol, ap) for g in target_vals)
        if ok_pool:
            matched += 1
        else:
            unmatched.append(c["span"])
    return {"num_total": len(cands), "num_matched": matched, "cell_restated": cell_hit, "target_restated": target_hit,
            "num_frac": (matched / len(cands)) if cands else float("nan"), "unmatched": unmatched}


def en_leak(narrative: str) -> float:
    t = unicodedata.normalize("NFKC", narrative)
    t = EN_WHITELIST_RE.sub(" ", t)
    r, h, l = char_ratio(t, "latin")
    return 0.0 if (h + l) == 0 else r


def load_results(path: Path) -> list[dict]:
    """키별 마지막 is_error=false 레코드(없으면 마지막 레코드)."""
    last_ok, last_any, order = {}, {}, []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = (r["qid"], r["cond"], r["model"], int(r["rep"]))
            if k not in last_any:
                order.append(k)
            last_any[k] = r
            if r.get("is_error") is False:
                last_ok[k] = r
    return [last_ok.get(k, last_any[k]) for k in order]


def gate_record(r: dict, qrow: dict | None, num_thresh: float) -> dict:
    cond = r["cond"]
    row = {"qid": r["qid"], "cond": cond, "model": r["model"], "rep": int(r["rep"]),
           "is_error": bool(r.get("is_error"))}
    fmt, analysis, narrative = split_sections(r.get("response"))
    target = "latin" if cond == "pivot" else "hangul"
    gp = load_gold_pool(qrow) if qrow is not None else None
    # 언어 비율: 표 라벨·제목, 단위 약어(%p, p.p., CPI, KOSIS …), 마커 단어는 어느 언어에도 세지 않음
    lang_text = strip_labels(unicodedata.normalize("NFKC", analysis), gp["labels"]) if gp else analysis
    lang_text = MARKER_WORD_RE.sub(" ", EN_WHITELIST_RE.sub(" ", lang_text))
    ratio, _, _ = char_ratio(lang_text, target)
    lang_ok = (not pd.isna(ratio)) and ratio >= LANG_THRESH
    if gp is not None:
        nc = check_numbers(analysis, gp["cell_vals"], gp["pool"], gp["target_vals"], gp["n_rows"], gp["labels"])
        num_ok = nc["cell_restated"] and nc["num_total"] > 0 and nc["num_frac"] >= num_thresh
    else:
        nc = {"num_total": 0, "num_matched": 0, "cell_restated": False, "target_restated": False,
              "num_frac": float("nan"), "unmatched": []}
        num_ok = False
    if row["is_error"]:
        fmt = lang_ok = num_ok = False
    row.update({
        "format_ok": bool(fmt), "lang_ok": bool(lang_ok), "num_ok": bool(num_ok),
        "gate_pass": bool(fmt and lang_ok and num_ok),
        "en_leak_ratio": round(en_leak(narrative), 4),
        "lang_ratio": None if pd.isna(ratio) else round(ratio, 4),
        "num_total": nc["num_total"], "num_matched": nc["num_matched"], "cell_restated": bool(nc["cell_restated"]),
        "target_restated": bool(nc["target_restated"]),
        "analysis_len_chars": len(analysis.strip()), "narrative_len_chars": len(narrative.strip()),
        "unmatched": "; ".join(nc["unmatched"]),
    })
    return row


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default=str(DEFAULT_RESULTS))
    ap.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--num-thresh", type=float, default=0.90,
                    help="ANALYSIS COUNT/INDEX 숫자 허용오차 내 비율 기준 (검토보고 1-6 규칙 0.90; 1.0 = SPEC §6 '전부')")
    ap.add_argument("--verbose", action="store_true", help="실패 건의 불일치 숫자 출력")
    args = ap.parse_args(argv)

    recs = load_results(Path(args.results))
    qdf = pd.read_csv(args.questions, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    qmap = {row["qid"]: row for row in qdf.to_dict("records")}
    rows = []
    for r in recs:
        q = qmap.get(r["qid"])
        if q is None:
            print(f"[warn] qid {r['qid']} not in questions.csv", file=sys.stderr)
        rows.append(gate_record(r, q, args.num_thresh))
    df = pd.DataFrame(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")

    print(f"[gate] records={len(df)} -> {out}  (num_thresh={args.num_thresh}, lang_thresh={LANG_THRESH})")
    checks = ["format_ok", "lang_ok", "num_ok", "gate_pass"]
    if len(df):
        g = df.groupby(["cond", "model"]).agg(
            n=("qid", "size"), **{c: (c, "mean") for c in checks},
            en_leak=("en_leak_ratio", "mean"), analysis_len=("analysis_len_chars", "mean"),
            cell_restated=("cell_restated", "mean"))
        pd.set_option("display.width", 160)
        print("\n[pass rates by (cond, model)]")
        print(g.round(3).to_string())
        print("\n[per-check overall]")
        for c in checks + ["cell_restated"]:
            print(f"  {c:14s} {df[c].mean():.3f}  ({int(df[c].sum())}/{len(df)})")
        print(f"  en_leak_ratio  mean={df['en_leak_ratio'].mean():.4f}")
        fails = df[~df["num_ok"] & ~df["is_error"]]
        if args.verbose and len(fails):
            print("\n[num_ok failures: unmatched COUNT/INDEX numbers]")
            for _, x in fails.iterrows():
                print(f"  {x['qid']} {x['cond']} {x['model']} r{x['rep']}: total={x['num_total']} matched={x['num_matched']} "
                      f"cell_restated={x['cell_restated']} unmatched=[{x['unmatched']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
