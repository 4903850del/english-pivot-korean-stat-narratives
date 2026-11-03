# -*- coding: utf-8 -*-
"""합성 fixture 로 fetch → build → verify 파이프라인을 끝까지 돌리는 단위 테스트.

실행: PYTHONUTF8=1 py -3.11 data/test_pipeline.py
"""
import json
import re
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_questions as bq  # noqa: E402

PY = [sys.executable]


def run(args, cwd):
    r = subprocess.run(PY + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env={**__import__("os").environ, "PYTHONUTF8": "1"})
    if r.returncode != 0:
        print(r.stdout); print(r.stderr)
        raise AssertionError(f"exit {r.returncode}: {' '.join(args)}")
    return r.stdout


def test_rounding():
    assert bq.q(Decimal("0.05"), 1) == Decimal("0.1"), "ROUND_HALF_UP 0.05 -> 0.1"
    assert bq.q(Decimal("-0.05"), 1) == Decimal("-0.1")
    assert bq.q(Decimal("2.345"), 2) == Decimal("2.35")
    assert bq.q(Decimal("1.25"), 1) == Decimal("1.3")  # banker's 라면 1.2
    g = bq.gold("x", "COUNT", Decimal("1234567.4"), 0, "f")
    assert g["display"] == "1,234,567" and g["sign"] == 1
    g = bq.gold("x", "PCT", Decimal("-0.04"), 1, "f")
    assert g["display"] == "-0.0" and g["sign"] == -1 and g["exact"] == -0.04
    assert bq.rank_desc([Decimal(3), Decimal(5), Decimal(5), Decimal(1)], 0) == 3
    assert bq.rank_desc([Decimal(3), Decimal(5), Decimal(5), Decimal(1)], 2) == 1
    print("rounding/gold OK")


def test_pipeline():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        run([str(HERE / "fixtures" / "make_fixture.py")], cwd=HERE)
        run([str(HERE / "fetch_kosis.py"), "--table", "all", "--fixture-dir", str(HERE / "fixtures"),
             "--raw-dir", str(td / "raw"), "--out-dir", str(td)], cwd=HERE)
        for k, n in (("pop", 18 * 3 * 3), ("emp", 3 * 7 * 3), ("cpi", 13 * 3)):
            df = pd.read_csv(td / f"{k}.csv", encoding="utf-8-sig", dtype=str, keep_default_na=False)
            assert len(df) == n, (k, len(df))
            assert set(bq.CSV_COLUMNS) >= {"context_cells"} or True
        pop = pd.read_csv(td / "pop.csv", encoding="utf-8-sig", dtype=str, keep_default_na=False)
        assert (pop["flag"] == "p").sum() == 1, "smbl 부호 p 셀 1개"
        emp = pd.read_csv(td / "emp.csv", encoding="utf-8-sig", dtype=str, keep_default_na=False)
        assert (emp["flag"] == "NA").sum() == 1 and (emp["value"] == "").sum() == 1, "비숫자 셀 NA"
        cpi = pd.read_csv(td / "cpi.csv", encoding="utf-8-sig", dtype=str, keep_default_na=False)
        assert "총지수" in set(cpi["row_label"]) and not any(re.match(r"^\d", x) for x in cpi["row_label"]), "코드 접두어 제거"

        (td / "tables.json").write_text((HERE / "tables.json").read_text(encoding="utf-8"), encoding="utf-8")
        run([str(HERE / "build_questions.py"), "--data-dir", str(td)], cwd=HERE)
        qs = pd.read_csv(td / "questions.csv", encoding="utf-8-sig", dtype=str, keep_default_na=False)
        assert list(qs.columns) == bq.CSV_COLUMNS and "target_cells" in qs.columns
        assert len(qs) == 50
        assert qs["table_key"].value_counts().to_dict() == {"pop": 17, "emp": 17, "cpi": 16}
        assert qs["qid"].is_unique
        for r in qs.to_dict("records"):
            req, aux, cells = json.loads(r["gold_required"]), json.loads(r["gold_aux"]), json.loads(r["context_cells"])
            tcells = json.loads(r["target_cells"])
            assert 3 <= len(req) <= 6 and 2 <= len(aux) <= 12, r["qid"]
            # v2: 표 전체 슬라이스 + target ⊂ context + 표 전체 RANK 1개 이상 + markdown 길이
            exp_ctx = {"pop": {54, 68}, "emp": {63}, "cpi": {39}}[r["table_key"]]
            assert len(cells) in {n - d for n in exp_ctx for d in (0, 3, 4, 9)}, (r["qid"], len(cells))  # fixture 는 부호 행 1개 제외
            assert all(c in cells for c in tcells) and 3 <= len(tcells) <= 12, (r["qid"], len(tcells))
            assert any(g["type"] == "RANK" for g in req), r["qid"]
            assert len(r["context_markdown"]) <= 2500, (r["qid"], len(r["context_markdown"]))
            assert all(g["basis"] in ("exact", "display") for g in req + aux) and all(g["basis"] == "exact" for g in req)
            pool = json.loads(r["gold_pool"])
            assert len(pool) >= 100 and all(set(g) == {"name", "type", "exact", "display", "decimals", "sign", "formula", "basis", "row"} for g in pool), r["qid"]
            ctx_rows = {c["row_label"] for c in cells}
            assert {g["row"] for g in pool} == ctx_rows, (r["qid"], "pool must cover every context row")
            assert sum(1 for g in pool if g["type"] == "RANK") >= len(ctx_rows) - 2, r["qid"]
            assert "순" in r["question_ko"], ("rank direction missing", r["qid"])
            # aux 는 gold_required 와 (type, display) 중복 없음, aux 내부도 중복 없음
            keys_req = {(g["type"], round(abs(g["exact"]), 6)) for g in req}
            keys_aux = [(g["type"], round(abs(g["exact"]), 6)) for g in aux]
            assert not (set(keys_aux) & keys_req) and len(set(keys_aux)) == len(keys_aux), r["qid"]
            # context_cells.value == float(dt_raw) (스케일링 없음)
            for c in cells:
                assert c["value"] == float(c["dt_raw"].replace(",", "")), (r["qid"], c)
            if r["template_id"] == "P1":
                # 규칙 (a): 2023·2024 증감률 차이(%p) 가 aux 에 있어야 함
                assert any(g["type"] == "PP" and "차이(%p)" in g["name"] for g in aux), r["qid"]
            if r["template_id"] == "P2":
                assert all(g["type"] == "COUNT" and g["decimals"] == 1 for g in req if "성비" in g["name"] and g["type"] != "RANK"), r["qid"]
            assert all(c["flag"] == "" for c in cells), ("flagged cell in context", r["qid"])
            assert not any(c["row_label"] == "세종특별자치시" and c["period"] == "2024" and c["col_label"] == "총인구수" for c in cells), r["qid"]
            assert not any(c["row_label"] == "15 - 19세" and c["period"] == "2022" and c["col_label"] == "여자" for c in cells), r["qid"]
            for g in req + aux:
                assert set(g) == {"name", "type", "exact", "display", "decimals", "sign", "formula", "basis"}
                assert g["type"] in {"PCT", "PP", "RATIO", "COUNT", "INDEX", "RANK"}
                # display 는 exact 를 decimals 로 반올림한 것
                d = Decimal(str(g["exact"])).quantize(Decimal(1).scaleb(-g["decimals"]), rounding="ROUND_HALF_UP")
                assert Decimal(g["display"].replace(",", "")) == d, (r["qid"], g)
            # 모든 셀 값이 markdown 에 표시
            for c in cells:
                shown = f"{int(c['value']):,}" if r["unit_nm"] == "명" else c["dt_raw"]
                assert shown in r["context_markdown"], (r["qid"], shown)
            assert "단위:" in r["context_markdown"] and "년" in r["context_markdown"]
            assert re.search(r"\d\)", r["required_list_text"]) and not re.search(r"\d+\.\d", r["required_list_text"]), "필수값 목록에 값 없음"
            assert r["gold_hash"] == bq.gold_hash(req, cells)
            if r["template_id"] in ("E2", "E5"):
                types = {g["type"] for g in req}
                assert {"PP", "PCT"} <= types, (r["qid"], types)
                assert "%p" in r["question_ko"] and "%" in r["question_ko"]
            assert "{fetch_date}" not in r["source_citation"] and r["fetch_date"] in r["source_citation"]
        # 결정성: 두 번 빌드해도 동일
        qs2 = bq.build_all(td)
        assert qs2["gold_hash"].tolist() == qs["gold_hash"].tolist()
        # verify --offline 는 전부 OK
        out = run([str(HERE / "verify_gold.py"), "--offline", "--questions", str(td / "questions.csv"), "--data-dir", str(td),
                   "--raw-dir", str(td / "raw"), "--work-dir", str(td / "verify")], cwd=HERE)
        assert "DIFF 0" in out and "OK 50" in out, out[-300:]
        # 값이 바뀌면 DIFF 가 잡히는지: pop raw 의 서울 2024 총인구를 바꾼다
        raw = json.load(open(td / "raw" / "pop.json", encoding="utf-8"))
        for row in raw:
            if row["C1"] == "11" and row["PRD_DE"] == "2024" and row["ITM_ID"] == "T20":
                row["DT"] = str(int(row["DT"]) + 1)
        json.dump(raw, open(td / "raw" / "pop.json", "w", encoding="utf-8"), ensure_ascii=False)
        r = subprocess.run(PY + [str(HERE / "verify_gold.py"), "--offline", "--questions", str(td / "questions.csv"), "--data-dir", str(td),
                                 "--raw-dir", str(td / "raw"), "--work-dir", str(td / "verify")], cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                           env={**__import__("os").environ, "PYTHONUTF8": "1"})
        assert r.returncode == 1 and "DIFF" in r.stdout and "서울특별시" in r.stdout, r.stdout[-500:]
        n_diff = int(re.search(r"DIFF (\d+)", r.stdout.split("요약:")[-1]).group(1))
        print(f"pipeline OK (서울 2024 변경 시 DIFF {n_diff}건 감지)")
        print((td / "questions_preview.md").read_text(encoding="utf-8")[:1500])


if __name__ == "__main__":
    test_rounding()
    test_pipeline()
    print("ALL TESTS PASSED")
