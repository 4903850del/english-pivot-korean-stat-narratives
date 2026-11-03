# -*- coding: utf-8 -*-
"""KOSIS 통계표 조회기: tables.json → raw JSON(원본 그대로) → 정규 셀 CSV.

실행: py -3.11 data/fetch_kosis.py --table pop|emp|cpi|all
      (환경변수 PYTHONUTF8=1 권장)
옵션: --fixture-dir DIR   HTTP 대신 DIR/<key>.json, DIR/<key>_smbl.json 을 읽음(단위 테스트용)
      --raw-dir DIR       raw 저장 위치(기본 data/raw)
      --out-dir DIR       CSV 저장 위치(기본 data)
인증키는 experiment/.env 의 KOSIS_API_KEY 에서만 읽고, 어디에도 출력·저장하지 않는다.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
EXP_DIR = HERE.parent
TABLES_JSON = HERE / "tables.json"
SLEEP_SEC = 0.3

CSV_COLUMNS = [
    "table_key", "tbl_id", "tbl_nm", "prd_se", "period",
    "c1", "c1_nm", "c2", "c2_nm", "itm_id", "itm_nm", "unit_nm",
    "row_label", "col_label", "dt_raw", "dt_smbl", "value", "flag",
    "lst_chn_de", "fetch_date",
]


def load_tables():
    with open(TABLES_JSON, encoding="utf-8") as f:
        return json.load(f)


def load_key():
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        raise SystemExit("python-dotenv 가 필요합니다: py -3.11 -m pip install python-dotenv")
    env_path = EXP_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    key = os.environ.get("KOSIS_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            f"KOSIS_API_KEY 가 없습니다. {env_path} 에 'KOSIS_API_KEY=<키>' 를 넣으세요 (.env.example 참고)."
        )
    return key


def build_query(key, t, smbl):
    params = [("method", "getList"), ("apiKey", key), ("orgId", t["orgId"]), ("tblId", t["tblId"])]
    for obj in t["objL"]:
        params.append((obj["param"], "+".join(c["code"] for c in obj["codes"])))
    params.append(("itmId", "+".join(t["itmIds"])))
    params += [("prdSe", t["prdSe"]), ("startPrdDe", t["startPrdDe"]), ("endPrdDe", t["endPrdDe"]),
               ("format", "json"), ("jsonVD", "Y")]
    if smbl:
        params.append(("smblChk", "Y"))
    # KOSIS 의 URL 생성기와 동일하게 '+' 는 그대로 둔다(복수 코드 구분자)
    return urlencode(params, safe="+")


def call_kosis(base, key, t, smbl):
    """HTTP 200 이어도 본문이 {'err': ..} 이면 오류. 키는 예외 메시지에서도 가린다."""
    q = build_query(key, t, smbl)

    def redact(s):
        return str(s).replace(key, "***KEY***")

    try:
        r = requests.get(base + "?" + q, timeout=60, headers={"User-Agent": "kosis-fetch/1.0"})
    except requests.RequestException as e:
        raise SystemExit("HTTP 오류: " + redact(e))
    if r.status_code != 200:
        raise SystemExit(f"HTTP {r.status_code}: {redact(r.text[:300])}")
    try:
        js = r.json()
    except ValueError:
        raise SystemExit("JSON 파싱 실패: " + redact(r.text[:300]))
    if isinstance(js, dict):
        if "err" in js or "errMsg" in js:
            raise SystemExit(f"KOSIS 오류 응답: err={js.get('err')} errMsg={redact(js.get('errMsg'))}")
        raise SystemExit("예상치 못한 dict 응답: " + redact(json.dumps(js, ensure_ascii=False)[:300]))
    if not isinstance(js, list) or not js:
        raise SystemExit("빈 응답(조회 결과 없음). 파라미터를 확인하세요.")
    return js


def parse_value(dt_str):
    """DT 문자열 → (float|None). 숫자가 아니면 None."""
    if dt_str is None:
        return None
    s = str(dt_str).strip().replace(",", "")
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return float(s)
    return None


def extract_flag(dt_plain, dt_smbl):
    """smblChk=Y 응답의 DT 에서 숫자 외 부호를 추출. 값 자체가 비숫자면 'NA'."""
    if parse_value(dt_plain) is None:
        return "NA"
    if dt_smbl is None:
        return ""
    s = str(dt_smbl).strip()
    # 숫자·쉼표·소수점·선행 부호를 제거한 나머지가 통계부호(p, r, x 등)
    rest = re.sub(r"^-?[\d,]*\.?\d*", "", s).strip()
    return rest


def normalize(table_key, t, rows_plain, rows_smbl, fetch_date):
    def rkey(r):
        return (r.get("C1"), r.get("C2"), r.get("C3"), r.get("ITM_ID"), r.get("PRD_DE"))

    smbl_map = {rkey(r): r.get("DT") for r in rows_smbl} if rows_smbl else {}
    layout = t["layout"]
    out = []
    for r in rows_plain:
        rec = {
            "table_key": table_key,
            "tbl_id": r.get("TBL_ID", t["tblId"]),
            "tbl_nm": r.get("TBL_NM", t["tbl_name_ko"]),
            "prd_se": r.get("PRD_SE", t["prdSe"]),
            "period": str(r.get("PRD_DE")),
            "c1": r.get("C1", ""), "c1_nm": r.get("C1_NM", ""),
            "c2": r.get("C2", ""), "c2_nm": r.get("C2_NM", ""),
            "itm_id": r.get("ITM_ID", ""), "itm_nm": r.get("ITM_NM", ""),
            "unit_nm": r.get("UNIT_NM") or t["unit"],
            "dt_raw": str(r.get("DT")),
            "dt_smbl": str(smbl_map.get(rkey(r), "")),
            "lst_chn_de": str(r.get("LST_CHN_DE", "")),
            "fetch_date": fetch_date,
        }
        rec["value"] = parse_value(rec["dt_raw"])
        rec["flag"] = extract_flag(rec["dt_raw"], smbl_map.get(rkey(r)))
        rec["row_label"] = rec[layout["row"].lower()]
        rec["col_label"] = rec[layout["col"].lower()]
        if layout.get("strip_code_prefix"):
            # KOSIS 분류명 '01 식료품 및 비주류음료' → '식료품 및 비주류음료'
            rec["row_label"] = re.sub(r"^\d+\s+", "", rec["row_label"])
        out.append(rec)
    df = pd.DataFrame(out, columns=CSV_COLUMNS)
    # 코드 순서(tables.json) → 기간 순으로 정렬해 재현 가능한 CSV 를 만든다
    order_c1 = {c["code"]: i for i, c in enumerate(t["objL"][0]["codes"])}
    order_c2 = {c["code"]: i for i, c in enumerate(t["objL"][1]["codes"])} if len(t["objL"]) > 1 else {}
    order_itm = {c: i for i, c in enumerate(t["itmIds"])}
    df["_o1"] = df["c1"].map(order_c1).fillna(999)
    df["_o2"] = df["c2"].map(order_c2).fillna(999)
    df["_o3"] = df["itm_id"].map(order_itm).fillna(999)
    df = df.sort_values(["_o1", "_o2", "_o3", "period"]).drop(columns=["_o1", "_o2", "_o3"]).reset_index(drop=True)
    return df


def fetch_table(table_key, cfg, key=None, fixture_dir=None, raw_dir=None, out_dir=None, quiet=False):
    t = cfg["tables"][table_key]
    raw_dir = Path(raw_dir) if raw_dir else HERE / "raw"
    out_dir = Path(out_dir) if out_dir else HERE
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    fetch_date = dt.date.today().isoformat()

    if fixture_dir:
        fixture_dir = Path(fixture_dir)
        with open(fixture_dir / f"{table_key}.json", encoding="utf-8") as f:
            rows_plain = json.load(f)
        smbl_path = fixture_dir / f"{table_key}_smbl.json"
        rows_smbl = json.load(open(smbl_path, encoding="utf-8")) if smbl_path.exists() else []
    else:
        rows_plain = call_kosis(cfg["api_base"], key, t, smbl=False)
        time.sleep(SLEEP_SEC)
        rows_smbl = call_kosis(cfg["api_base"], key, t, smbl=True)
        time.sleep(SLEEP_SEC)

    with open(raw_dir / f"{table_key}.json", "w", encoding="utf-8") as f:
        json.dump(rows_plain, f, ensure_ascii=False, indent=1)
    with open(raw_dir / f"{table_key}_smbl.json", "w", encoding="utf-8") as f:
        json.dump(rows_smbl, f, ensure_ascii=False, indent=1)

    df = normalize(table_key, t, rows_plain, rows_smbl, fetch_date)
    csv_path = out_dir / f"{table_key}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    if not quiet:
        n_flag = int((df["flag"] != "").sum())
        print(f"[{table_key}] {t['tblId']} rows={len(df)} periods={sorted(df['period'].unique())} "
              f"units={sorted(df['unit_nm'].unique())} flagged={n_flag} lst_chn_de={df['lst_chn_de'].max()} -> {csv_path}")
        if n_flag:
            print(df.loc[df["flag"] != "", ["row_label", "col_label", "period", "dt_raw", "dt_smbl", "flag"]].to_string(index=False))
    return df


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", default="all", choices=["pop", "emp", "cpi", "all"])
    ap.add_argument("--fixture-dir", default=None)
    ap.add_argument("--raw-dir", default=None)
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args(argv)
    cfg = load_tables()
    keys = ["pop", "emp", "cpi"] if a.table == "all" else [a.table]
    api_key = None if a.fixture_dir else load_key()
    for k in keys:
        fetch_table(k, cfg, key=api_key, fixture_dir=a.fixture_dir, raw_dir=a.raw_dir, out_dir=a.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
