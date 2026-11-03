# -*- coding: utf-8 -*-
"""재조회 후 gold_hash 대조: questions.csv 의 각 qid 에 대해 KOSIS 를 다시 조회하고
같은 템플릿·같은 seed 로 문항을 재생성해 gold_hash 가 같은지 확인한다.

실행: py -3.11 data/verify_gold.py            (KOSIS 재조회 → data/raw/verify/ 에 저장)
      py -3.11 data/verify_gold.py --offline  (재조회 없이 data/raw/*.json 로 재생성·대조; 파이프라인 자체 점검용)
      py -3.11 data/verify_gold.py --questions PATH --data-dir DIR --work-dir DIR
출력: qid 별 OK/DIFF, 요약. DIFF 가 하나라도 있으면 종료코드 1.
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_kosis  # noqa: E402
import build_questions  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--questions", default=str(HERE / "questions.csv"))
    ap.add_argument("--data-dir", default=str(HERE), help="tables.json 위치")
    ap.add_argument("--work-dir", default=None, help="재조회 raw/CSV 저장 위치(기본 data/raw/verify)")
    ap.add_argument("--offline", action="store_true", help="재조회 대신 기존 raw JSON(--raw-dir)을 fixture 로 사용")
    ap.add_argument("--raw-dir", default=str(HERE / "raw"))
    a = ap.parse_args(argv)

    data_dir = Path(a.data_dir)
    work = Path(a.work_dir) if a.work_dir else HERE / "raw" / "verify"
    work.mkdir(parents=True, exist_ok=True)
    cfg = fetch_kosis.load_tables()
    api_key = None if a.offline else fetch_kosis.load_key()
    for k in ("pop", "emp", "cpi"):
        fetch_kosis.fetch_table(k, cfg, key=api_key, fixture_dir=(a.raw_dir if a.offline else None),
                                raw_dir=work, out_dir=work, quiet=True)
    # tables.json 을 work 에 복사해 build_all 이 같은 메타를 쓰게 한다
    (work / "tables.json").write_text((data_dir / "tables.json").read_text(encoding="utf-8"), encoding="utf-8")
    new = build_questions.build_all(work, cfg)
    old = pd.read_csv(a.questions, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    new_map = {r["qid"]: r for r in new.to_dict("records")}

    n_ok = n_diff = 0
    for r in old.to_dict("records"):
        qid = r["qid"]
        nr = new_map.get(qid)
        if nr is None:
            print(f"DIFF {qid}: 재생성 결과에 없음")
            n_diff += 1
            continue
        if nr["gold_hash"] == r["gold_hash"]:
            print(f"OK   {qid}")
            n_ok += 1
        else:
            n_diff += 1
            og, ng = json.loads(r["gold_required"]), json.loads(nr["gold_required"])
            oc, nc = json.loads(r["context_cells"]), json.loads(nr["context_cells"])
            why = []
            if oc != nc:
                for c1, c2 in zip(oc, nc):
                    if c1 != c2:
                        why.append(f"cell {c1['row_label']}/{c1['col_label']}/{c1['period']}: {c1['dt_raw']} -> {c2['dt_raw']} flag {c1['flag']!r}->{c2['flag']!r}")
                        break
                if len(oc) != len(nc):
                    why.append(f"cell count {len(oc)} -> {len(nc)}")
            if og != ng:
                for g1, g2 in zip(og, ng):
                    if g1 != g2:
                        why.append(f"gold {g1['name']}: {g1['display']} -> {g2['display']}")
                        break
            print(f"DIFF {qid}: " + ("; ".join(why) or "hash mismatch"))
    extra = set(new_map) - set(old["qid"])
    print(f"\n요약: OK {n_ok} / DIFF {n_diff} / 총 {len(old)}" + (f" (재생성에만 있는 qid {len(extra)})" if extra else ""))
    return 1 if n_diff else 0


if __name__ == "__main__":
    sys.exit(main())
