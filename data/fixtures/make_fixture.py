# -*- coding: utf-8 -*-
"""합성 KOSIS 응답(fixture) 생성기 — 실제 값이 아님. 파이프라인 단위 테스트 전용.

실행: py -3.11 data/fixtures/make_fixture.py   → data/fixtures/{pop,emp,cpi}.json, *_smbl.json
응답 형식은 statisticsParameterData.do(format=json, jsonVD=Y) 필드명을 따른다.
일부 셀에 통계부호(p, -)를 넣어 '부호 셀 제외' 로직을 검증한다.
"""
import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
cfg = json.load(open(HERE.parent / "tables.json", encoding="utf-8"))
rng = random.Random(7)
YEARS = ["2022", "2023", "2024"]


def row(t, c1, c1nm, itm, itmnm, unit, prd, dt, c2=None, c2nm=None):
    r = {"ORG_ID": t["orgId"], "TBL_ID": t["tblId"], "TBL_NM": t["tbl_name_ko"],
         "C1": c1, "C1_OBJ_NM": t["objL"][0]["classNm"], "C1_NM": c1nm}
    if c2 is not None:
        r.update({"C2": c2, "C2_OBJ_NM": t["objL"][1]["classNm"], "C2_NM": c2nm})
    r.update({"ITM_ID": itm, "ITM_NM": itmnm, "UNIT_ID": "", "UNIT_NM": unit,
              "PRD_SE": "Y", "PRD_DE": prd, "DT": dt, "LST_CHN_DE": "20250930"})
    return r


def make_pop():
    t = cfg["tables"]["pop"]
    plain, smbl = [], []
    for c in t["objL"][0]["codes"]:
        base = 51_000_000 if c["code"] == "00" else rng.randint(300_000, 13_000_000)
        m_share = rng.uniform(0.485, 0.505)
        for y_i, y in enumerate(YEARS):
            tot = int(base * (1 + rng.uniform(-0.01, 0.01)) ** y_i)
            m = int(tot * m_share)
            f = tot - m
            for itm, val in (("T20", tot), ("T21", m), ("T22", f)):
                dt = str(val)
                plain.append(row(t, c["code"], c["name"], itm, t["itm_names"][itm], "명", y, dt))
                # 세종 2024 총인구에 잠정치 부호
                flag = "p" if (c["code"] == "36" and y == "2024" and itm == "T20") else ""
                smbl.append(row(t, c["code"], c["name"], itm, t["itm_names"][itm], "명", y, dt + flag))
    return plain, smbl


def make_emp():
    t = cfg["tables"]["emp"]
    plain, smbl = [], []
    for s in t["objL"][0]["codes"]:
        for g in t["objL"][1]["codes"]:
            base = {"00": 62, "10": 8, "20": 60, "30": 78, "40": 78, "50": 76, "60": 45}[g["code"]]
            base += {"0": 0, "2": 8, "3": -8}[s["code"]]
            for y_i, y in enumerate(YEARS):
                val = round(base + y_i * rng.uniform(-0.6, 0.9), 1)
                # 15-19세 여자 2022 결측('-')
                if s["code"] == "3" and g["code"] == "10" and y == "2022":
                    plain.append(row(t, s["code"], s["name"], "T90", "고용률", "%", y, "-", g["code"], g["name"]))
                    smbl.append(row(t, s["code"], s["name"], "T90", "고용률", "%", y, "-", g["code"], g["name"]))
                    continue
                plain.append(row(t, s["code"], s["name"], "T90", "고용률", "%", y, f"{val:.1f}", g["code"], g["name"]))
                smbl.append(row(t, s["code"], s["name"], "T90", "고용률", "%", y, f"{val:.1f}", g["code"], g["name"]))
    return plain, smbl


def make_cpi():
    t = cfg["tables"]["cpi"]
    plain, smbl = [], []
    reg = t["objL"][0]["codes"][0]
    for it in t["objL"][1]["codes"]:
        idx = 100 * (1 + rng.uniform(0.02, 0.09)) ** 2
        for y_i, y in enumerate(YEARS):
            idx = idx * (1 + rng.uniform(-0.01, 0.06)) if y_i else idx
            dt = f"{idx:.2f}"
            plain.append(row(t, reg["code"], reg["name"], "T", "소비자물가지수", "2020=100", y, dt, it["code"], it["name"]))
            smbl.append(row(t, reg["code"], reg["name"], "T", "소비자물가지수", "2020=100", y, dt, it["code"], it["name"]))
    return plain, smbl


for key, fn in (("pop", make_pop), ("emp", make_emp), ("cpi", make_cpi)):
    plain, smbl = fn()
    json.dump(plain, open(HERE / f"{key}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(smbl, open(HERE / f"{key}_smbl.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(key, len(plain), "rows")
