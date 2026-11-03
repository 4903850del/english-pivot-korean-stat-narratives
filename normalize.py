# -*- coding: utf-8 -*-
"""normalize.py — 수치 주장 추출·정규화 (SPEC §5).

공개 API
  parse_sections(text) -> (analysis, narrative, format_ok)
  extract_claims(narrative) -> list[dict]

주장(claim) dict 필드
  span        : 정규화(NFKC) 텍스트에서 잘라낸 원 표기 (부호·근사어·단위 포함)
  type        : PCT | PP | RATIO | COUNT | INDEX | RANK | UNK   (단위 없음 → UNK, 채점기에서 INDEX 우선 폴백)
  value       : 정규화된 수치(float). 만/억/조 합성, 천명·만명·억원 배수 반영. 부호는 포함하지 않음(절대값).
  sign        : 명시 부호 (-1 | 1 | 0=없음)
  decimals    : 유효 소수 자릿수 d (합성 표기는 유효 해상도 기준; 5천1백만 → -6, 939.4만 → -3)
  resolution  : 10 ** (-decimals)  — 허용오차 계산용
  approx      : 근사어(약·대략·거의·가량·남짓·정도·쯤·안팎·여) 동반 여부
  direction   : 증감 방향 (+1 | -1 | None). 명시 부호가 있으면 부호, 없으면 숫자 뒤 15자(다음 숫자 전까지)의 증감 어휘
  sentence    : 주장을 포함한 문장
  start, end  : NFKC 텍스트 내 위치

설계상 한계(문서화): 순한글 수사("두 배", "세 번째")·형태소 분석 없음. 하이픈은 두 숫자 사이에 공백 없이 올 때만 범위로 본다.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 1. 구획 파싱
# ---------------------------------------------------------------------------
_SECTION_RE = re.compile(r"===ANALYSIS===(.*?)===NARRATIVE===(.*?)===END===", re.DOTALL)
_SECTION_NOEND_RE = re.compile(r"(?:===ANALYSIS===(.*?))?===NARRATIVE===(.*)$", re.DOTALL)
_MARKER_RE = re.compile(r"===\s*(ANALYSIS|NARRATIVE|END)\s*===")


def parse_sections(text: str) -> Tuple[str, str, bool]:
    """세 마커를 순서대로 찾으면 (analysis, narrative, True).
    실패하면 (\"\", 전체 텍스트(마커 문자열 제거), False) — ITT: 전체를 NARRATIVE로 간주."""
    if text is None:
        text = ""
    m = _SECTION_RE.search(text)
    if m:
        return m.group(1).strip(), m.group(2).strip(), True
    m = _SECTION_NOEND_RE.search(text)
    if m:  # ===END=== 누락: NARRATIVE 마커 뒤 전체를 서술로 (format_fail 유지)
        return (m.group(1) or "").strip(), _MARKER_RE.sub(" ", m.group(2)).strip(), False
    stripped = _MARKER_RE.sub(" ", text).strip()  # NARRATIVE 마커 자체가 없음 → 전체를 서술로
    return "", stripped, False


# ---------------------------------------------------------------------------
# 2. 정규식 구성 요소
# ---------------------------------------------------------------------------
NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
# 한 세그먼트의 단위어: (십|백|천)(만|억|조)? 또는 (만|억|조); 뒤에 '여'(근사) 허용
SEG_UNIT = r"(?:[십백천]\s?[만억조]?|[만억조])"
# 숫자 표현: (NUM 단위어)* NUM (단위어)? 여?
NUMEXPR = rf"(?:{NUM}\s?{SEG_UNIT}\s?)*{NUM}(?:\s?{SEG_UNIT})?여?"
NUMEXPR_RE = re.compile(NUMEXPR)
RANGE_SEP_RE = re.compile(r"(?:\s?[~∼〜]\s?|[-–](?=\d))")

_SMALL = {"십": 10, "백": 100, "천": 1000}
_BIG = {"만": 10**4, "억": 10**8, "조": 10**12}
_TOKEN_RE = re.compile(rf"({NUM})|([십백천만억조])|(여)")

# 숫자 바로 뒤 접미사(타입). 검사 순서가 중요: PP → PCT → RATIO → RANK → 제외 → COUNT
PP_RE = re.compile(r"\s?(?:%\s?(?:p|P|포인트|pt)(?![A-Za-z])|퍼센트\s?포인트|p\.p\.?|pp(?![A-Za-z]))")
PT_RE = re.compile(r"\s?포인트")  # % 없이 단독 '포인트' = 지수 격차(INDEX point) → PT
PCT_RE = re.compile(r"\s?(?:%|퍼센트|프로(?![그세])|percent(?![A-Za-z]))")
RATIO_RE = re.compile(r"\s?배(?![경출분달당])")
RANK_RE = re.compile(r"\s?(?:위(?![해한])|번째|등(?![급]))")
EXCL_RE = re.compile(
    r"\s?(?:년|월|분기|개월|세|개|곳|종|가지|단계|번(?!째)|회|차례|차|일|주|시간|명당|호|건당)"
)
COUNT_RE = re.compile(r"\s?(?:명|원|가구|건|호|세대)(?![당])")
EQ_AFTER_RE = re.compile(r"\s?=")
EQ_BEFORE_RE = re.compile(r"=\s?$")
PER_RE = re.compile(r"(?i)\bper\s*$")

SIGN_BEFORE_RE = re.compile(r"(?:(?<![\d.,])[-−–+]|마이너스|플러스)\s?$")
APPROX_BEFORE_RE = re.compile(r"(?<![가-힣])(?:약|대략|거의)\s?$")
APPROX_AFTER_RE = re.compile(r"\s?(?:가량|남짓|정도|쯤|안팎)")
DIR_RE = re.compile(r"(증가|늘|상승|올라|높아|플러스|감소|줄|하락|떨어|낮아|마이너스)")  # SPEC 목록만; 확대/축소는 크기(절댓값) 추세어라 제외
CLAUSE_BREAK_RE = re.compile(r"[,，;.。]|이며|이고|이지만|으며|고,|지만")
SUPERLATIVE_RE = re.compile(r"(?:가장|더|보다|제일)\s*(?:높아|낮아|높은|낮은|커|작아)")
PARTICLE_RO_RE = re.compile(r"^\s*(?:으로|로)(?![가-힣])")
_DIR_POS = {"증가", "늘", "상승", "올라", "높아", "플러스"}
CITATION_RE = re.compile(r"\[\d+\]")
# 연령 표현(사전 블랭킹): '15 - 19세', '40 – 49세', '60세이상', '65세 미만', '30대', '60대 이상', '20~30대'
_AGE_SUFFIX = r"(?:\s*(?:이상|미만|이하|초과))?"
AGE_RE = re.compile(
    r"\d+\s*[-–−~〜∼]\s*\d+\s*(?:세|대(?!\d))" + _AGE_SUFFIX
    + r"|\d+\s*세" + _AGE_SUFFIX
    + r"|\d+대(?![\d비])" + _AGE_SUFFIX
)
SENT_SPLIT_RE = re.compile(r"(?<=[.!?。])\s+|\n+")

DIRECTION_WINDOW = 15

# 마크다운 표 / 목록 번호 / 괄호 안 산술식 (사전 블랭킹, 위치 보존)
TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.MULTILINE)
TABLE_SEP_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}")
TABLE_FIRST_CELL_RE = re.compile(r"^([ \t]*\|)([^|\n]*)")
LIST_MARKER_RE = re.compile(r"^[ \t]*(?:[-*•][ \t]*)?(?:\*\*)?\(?\d{1,2}[.)](?=[ \t*]|$)", re.MULTILINE)
PAREN_RE = re.compile(r"\(([^()\n]*)\)")
ARITH_OP_RE = re.compile(r"[×*÷/=]")
ARITH_ADJ_RE = re.compile(rf"(?:(?<=[×*÷/=+−-])[ \t]*{NUM}|{NUM}[ \t]*(?=[×*÷/=+−-]))")
# 괄호 밖: × ÷ 와 공백으로 둘러싸인 * / 만 연산자로 본다 (마크다운 **굵게**, 날짜 등 오탐 방지)
ARITH_OUT_OP = r"(?:×|÷|(?<=\s)\*(?=\s)|(?<=[\s\d])/(?=[\s\d]))"
ARITH_OUT_ADJ_RE = re.compile(rf"(?:(?<={ARITH_OUT_OP})[ \t]*{NUM}|{NUM}[ \t]*(?={ARITH_OUT_OP}))")
BOLD_RE = re.compile(r"\*\*")
YEAR_MIN, YEAR_MAX = 2000, 2030
SMALL_INT_MAX = 20


# ---------------------------------------------------------------------------
# 3. 숫자 표현 → 값·해상도
# ---------------------------------------------------------------------------
def _parse_numexpr(expr: str) -> Tuple[float, int, bool]:
    """'5천1백만' → (51_000_000, -6, False). 반환: (value, decimals, has_yeo).
    decimals = 마지막 세그먼트의 소수 자릿수 - log10(마지막 세그먼트 배수)."""
    total = 0.0
    section = 0.0
    cur: Optional[float] = None
    cur_dec = 0
    last_dec = 0
    last_mult = 1
    has_yeo = False
    pending_small = 1  # 마지막 세그먼트의 십/백/천 배수
    for m in _TOKEN_RE.finditer(expr):
        num, unit, yeo = m.group(1), m.group(2), m.group(3)
        if num:
            s = num.replace(",", "")
            cur = float(s)
            cur_dec = len(s.split(".")[1]) if "." in s else 0
            last_dec = cur_dec
            last_mult = 1
            pending_small = 1
        elif unit in _SMALL:
            mult = _SMALL[unit]
            section += (cur if cur is not None else 1) * mult
            pending_small = mult
            last_mult = mult
            cur = None
        elif unit in _BIG:
            big = _BIG[unit]
            if cur is not None:
                section += cur
            total += section * big
            last_mult = pending_small * big
            section = 0.0
            cur = None
            pending_small = 1
        elif yeo:
            has_yeo = True
    if cur is not None:
        section += cur
    total += section
    import math

    decimals = last_dec - int(round(math.log10(last_mult))) if last_mult > 1 else last_dec
    return total, decimals, has_yeo


def _classify_suffix(text: str, pos: int) -> Tuple[str, int]:
    """pos 이후의 접미사를 보고 (type, suffix_end) 반환. type ∈ PCT/PP/RATIO/RANK/COUNT/UNK/EXCL."""
    for typ, rx in (("PP", PP_RE), ("PT", PT_RE), ("PCT", PCT_RE), ("RATIO", RATIO_RE), ("RANK", RANK_RE)):
        m = rx.match(text, pos)
        if m:
            return typ, m.end()
    if EXCL_RE.match(text, pos):
        return "EXCL", pos
    m = COUNT_RE.match(text, pos)
    if m:
        return "COUNT", m.end()
    if EQ_AFTER_RE.match(text, pos):
        return "EXCL", pos
    return "UNK", pos


def _direction_after(text: str, pos: int) -> Optional[int]:
    window = text[pos : pos + DIRECTION_WINDOW]
    # 다음 숫자가 나오면 그 앞까지만 본다 (방향어가 다음 수치에 걸리는 오귀속 방지)
    mnum = re.search(r"\d", window)
    if mnum:
        window = window[: mnum.start()]
    # 같은 절 안에서만 본다 (, ， ; 이며 이고 이지만 …)
    mcb = CLAUSE_BREAK_RE.search(window)
    if mcb:
        window = window[: mcb.start()]
    # 'X에서 Y로 줄어/낮아져/확대' — 조사 로/으로 뒤의 증감어는 추세 서술이지 Y 의 부호가 아님
    if PARTICLE_RO_RE.match(window):
        return None
    # '가장/더/보다/제일 + 높아/낮아' 는 최상급·비교 형용사 → 방향어 아님
    window = SUPERLATIVE_RE.sub(" ", window)
    m = DIR_RE.search(window)
    if not m:
        return None
    return 1 if m.group(1) in _DIR_POS else -1


def split_sentences(text: str) -> List[Tuple[int, int, str]]:
    out = []
    pos = 0
    for part in SENT_SPLIT_RE.split(text):
        if part is None:
            continue
        idx = text.find(part, pos)
        if idx < 0:
            idx = pos
        out.append((idx, idx + len(part), part))
        pos = idx + len(part)
    return out


def _sentence_for(sentences, start: int) -> str:
    return _sentence_pair(sentences, start)[0]


def _sentence_pair(sentences, start: int) -> Tuple[str, str]:
    """(주장 문장, 직전 문장)"""
    for i, (s, e, txt) in enumerate(sentences):
        if s <= start < e:
            return txt.strip(), (sentences[i - 1][2].strip() if i > 0 else "")
    if sentences:
        return sentences[-1][2].strip(), (sentences[-2][2].strip() if len(sentences) > 1 else "")
    return "", ""


# ---------------------------------------------------------------------------
# 4. 주장 추출
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = CITATION_RE.sub(" ", text)
    return text


def blank_ages(text: str) -> str:
    """연령 표현을 같은 길이의 공백으로 치환(위치 보존). 주장 탐색용 텍스트에만 쓴다."""
    return AGE_RE.sub(lambda m: " " * len(m.group(0)), text)


def _blank_spans(text: str, spans) -> Tuple[str, int]:
    """spans=[(s,e)] 를 공백으로 치환하고, 치환 구간(겹침 병합) 안의 숫자 개수를 센다."""
    if not spans:
        return text, 0
    mask = [False] * len(text)
    for s, e in spans:
        for i in range(max(0, s), min(len(text), e)):
            mask[i] = True
    n = 0
    i = 0
    while i < len(text):
        if mask[i]:
            j = i
            while j < len(text) and mask[j]:
                j += 1
            n += len(NUMEXPR_RE.findall(text[i:j]))
            i = j
        else:
            i += 1
    buf = [(" " if (mask[i] and ch != "\n") else ch) for i, ch in enumerate(text)]
    return "".join(buf), n


def blank_table_and_lists(text: str) -> Tuple[str, int]:
    """(a) 마크다운 표: 헤더 행 전체 + 각 행의 첫 셀, (b) 줄머리 목록 번호를 블랭킹. 반환 (text, 제외 숫자 수)."""
    spans = []
    lines = text.split("\n")
    pos = 0
    offsets = []
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    for i, ln in enumerate(lines):
        if TABLE_ROW_RE.match(ln):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if TABLE_SEP_RE.match(nxt):  # 헤더 행
                spans.append((offsets[i], offsets[i] + len(ln)))
            elif not TABLE_SEP_RE.match(ln):
                m = TABLE_FIRST_CELL_RE.match(ln)
                if m:
                    spans.append((offsets[i] + m.start(2), offsets[i] + m.end(2)))
    for m in LIST_MARKER_RE.finditer(text):
        spans.append((m.start(), m.end()))
    return _blank_spans(text, spans)


def blank_arithmetic(text: str) -> Tuple[str, int]:
    """(d) 괄호 안 산술식: 연산자(× * ÷ / = + −)에 바로 붙은 숫자(피연산자·상수)를 블랭킹. '≈ 1.08%' 같은 결과는 유지.
    괄호 밖에서도 'a / b × 100', '× 100', '÷' 의 피연산자·상수를 블랭킹(연산자는 × ÷ 와 공백 둘러싼 * / 만)."""
    text = BOLD_RE.sub("  ", text)  # 마크다운 굵게 표시는 연산자가 아님(길이 보존)
    spans = []
    for m in ARITH_OUT_ADJ_RE.finditer(text):
        spans.append((m.start(), m.end()))
    for pm in PAREN_RE.finditer(text):
        inner = pm.group(1)
        if not ARITH_OP_RE.search(inner):
            continue
        base = pm.start(1)
        for m in ARITH_ADJ_RE.finditer(inner):
            # 숫자 뒤에 단위(명·% 등)가 바로 붙어 있어도 피연산자이면 함께 제외하지 않고 숫자만 블랭킹
            spans.append((base + m.start(), base + m.end()))
    return _blank_spans(text, spans)


def extract_claims(narrative: str) -> List[Dict]:
    return extract_claims_with_stats(narrative)[0]


def extract_claims_with_stats(narrative: str) -> Tuple[List[Dict], Dict[str, int]]:
    """(claims, stats). stats: table_list(표 첫 셀·헤더·목록 번호), arith(괄호 산술), year(단위 없는 2000~2030), small_int(단위 없는 1~20 정수)"""
    orig = normalize_text(narrative)
    sentences = split_sentences(orig)
    text = blank_ages(orig)  # 길이 동일 → 위치·문장은 orig 기준으로 사용 가능
    text, n_tl = blank_table_and_lists(text)
    text, n_ar = blank_arithmetic(text)
    stats = {"table_list": n_tl, "arith": n_ar, "year": 0, "small_int": 0}
    claims: List[Dict] = []
    seen = set()
    pos = 0
    n = len(text)
    while pos < n:
        m = NUMEXPR_RE.search(text, pos)
        if not m:
            break
        exprs = [(m.start(), m.end(), m.group(0))]
        end = m.end()
        # 범위 a~b
        r = RANGE_SEP_RE.match(text, end)
        if r:
            m2 = NUMEXPR_RE.match(text, r.end())
            if m2:
                exprs.append((m2.start(), m2.end(), m2.group(0)))
                end = m2.end()
        pos = end
        # 숫자 뒤에 글자가 바로 붙어 있는 영문/한글 알파벳 단위 외 처리: 접미사 분류
        typ, suf_end = _classify_suffix(text, end)
        if typ == "EXCL":
            pos = max(pos, suf_end)
            continue
        if typ == "UNK":
            raws = [e[2] for e in exprs]
            if all(re.fullmatch(r"\d{4}", r) and YEAR_MIN <= int(r) <= YEAR_MAX for r in raws):
                stats["year"] += len(raws)  # (c) 단위 없는 4자리 연도(범위 2022-2024 포함)
                continue
            if all(re.fullmatch(r"\d{1,2}", r) and 1 <= int(r) <= SMALL_INT_MAX for r in raws):
                stats["small_int"] += len(raws)  # (e) 단위·소수 없는 1~20 정수(범위 15-19 포함)는 주장이 아님
                continue
        # '=100' 류 제외 (앞쪽)
        prefix = text[: exprs[0][0]]
        if EQ_BEFORE_RE.search(prefix):
            continue
        # 숫자 바로 앞이 한글/영문 문자이면(예: 'A1') 제외
        if prefix and prefix[-1].isascii() and prefix[-1].isalpha():
            continue
        # 영문 'per 100 females' 류(100명당에 해당) 제외
        if PER_RE.search(prefix):
            continue
        # 부호·근사어(앞)
        sign = 0
        pfx = prefix
        ms = SIGN_BEFORE_RE.search(pfx)
        span_start = exprs[0][0]
        if ms:
            tok = ms.group(0).strip()
            sign = -1 if tok in ("-", "−", "–", "마이너스") else 1
            span_start = ms.start()
            pfx = pfx[: ms.start()]
        approx = False
        ma = APPROX_BEFORE_RE.search(pfx)
        if ma:
            approx = True
            span_start = ma.start()
        # 근사어(뒤)
        span_end = suf_end
        maa = APPROX_AFTER_RE.match(text, suf_end)
        if maa:
            approx = True
            span_end = maa.end()
        direction = _direction_after(text, span_end)
        for i, (s, e, expr) in enumerate(exprs):
            value, decimals, yeo = _parse_numexpr(expr)
            this_approx = approx or yeo
            this_sign = sign if i == 0 else 0
            key = (typ, round(value, 9), this_approx)
            if key in seen:
                continue
            seen.add(key)
            d = direction if direction is not None else None
            if this_sign != 0:
                d = this_sign
            claims.append(
                {
                    "span": text[span_start if i == 0 else s : span_end if i == len(exprs) - 1 else e].strip(),
                    "type": typ,
                    "value": value,
                    "sign": this_sign,
                    "decimals": decimals,
                    "resolution": 10.0 ** (-decimals),
                    "approx": this_approx,
                    "direction": d,
                    "sentence": _sentence_pair(sentences, s)[0],
                    "prev_sentence": _sentence_pair(sentences, s)[1],
                    "start": s,
                    "end": e,
                }
            )
        pos = max(pos, span_end)
    return claims, stats


if __name__ == "__main__":  # 간단 수동 확인
    import json, sys

    txt = sys.stdin.read() if not sys.stdin.isatty() else "2024년 서울 인구는 약 939만 명으로 전년 대비 0.6% 감소했다."
    a, nrr, ok = parse_sections(txt)
    for c in extract_claims(nrr if ok else txt):
        print(json.dumps(c, ensure_ascii=False))
