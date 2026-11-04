# English-pivot vs. Korean-direct: numeric faithfulness of LLM Korean statistical narratives

Repository: https://github.com/4903850del/english-pivot-korean-stat-narratives

Code, prompts, questions and logs for the KIIS 2026 Fall paper
「영어 피벗 전략이 LLM의 한국어 통계 분석 서술 수치 충실도에 미치는 영향」 (차주훈, 청주대학교 데이터사이언스학과).

## What is compared
- 2 × 2 matched design: language strategy (Korean-direct vs. English-pivot) × model tier (large: `claude-opus-5-5`, small: `claude-haiku-5-5`), 50 questions common to all four cells, 3 repeats per cell (600 generations).
- Both conditions produce an `===ANALYSIS===` section followed by a Korean `===NARRATIVE===`. The two prompts differ in **one line** (the language of the analysis section). Run `diff prompts/direct.txt prompts/pivot.txt` to see it.
- Inputs are raw KOSIS table cells only; derived values (growth %, %p gaps, ratios, ranks) are computed by the model and scored against gold values computed from the same cells.
- Metrics: hallucination rate H, unit/digit error rate U (⊆ H), coverage C. Primary contrast: pooled Δ = H_pivot − H_direct with a question-level cluster bootstrap (ratio-of-sums, B = 10,000, BCa with percentile fallback). See `analysis_plan_v1.md` (plan and prompts hashed before the main run; hashes in `results/preregistration_hashes.txt`). **The scorer was not frozen:** the question file gained `gold_pool` and the scorer was revised after the main run started, after an interim look at 73 responses, and again after two audits. The change log in v1.5 discloses this, and `results/precorrection_results.md` shows the primary contrast at each stage.

## Layout
```
SPEC.md               interface contract between the scripts
analysis_plan_v1.md   pre-registered analysis plan (+ change log)
data/                 KOSIS fetch, question builder, gold verification, questions.csv
prompts/              system / common template / direct / pivot
run.py                calls the Claude Code CLI (subscription path), resumable, interleaved order
gate.py               symmetric integrity gate for the analysis section
normalize.py score.py numeric claim extraction, normalization, typed matching
analyze.py            bootstrap, MDE, table 2, forest plot
human_sample.py       blind stratified validation sheet (50 claims) and scoring against auto verdicts
tests/                unit tests and synthetic fixtures
results/              results.jsonl (all generations), claims, tables, summary
figures/              fig2_forest.png
```

## Reproduce
```
set PYTHONUTF8=1
py -3.11 -m pip install numpy scipy "pandas==2.2.3" matplotlib requests python-dotenv pytest
copy .env.example .env   # put your KOSIS Open API key in .env (never commit it)
py -3.11 data/fetch_kosis.py --table all
py -3.11 data/build_questions.py
py -3.11 data/verify_gold.py
py -3.11 run.py --workers 3 --reject-fallback      # needs a logged-in Claude Code CLI
py -3.11 gate.py
py -3.11 score.py
py -3.11 analyze.py
py -3.11 human_sample.py
```
Model calls go through `claude -p` with `--safe-mode --tools "" --setting-sources "" --effort low --output-format json --system-prompt <prompts/system.txt>`; temperature and seed are not exposed by this path, extended thinking is minimized but not fully disabled, and every call logs `served_model`, `thinking_tokens` and `model_fallback`.

## Data source and licenses
- Statistics: 국가데이터처, 국가통계포털(KOSIS) — 주민등록인구현황 행정구역(시군구)별/성별 인구수 (DT_1B040A3), 경제활동인구조사 성/연령별 경제활동인구 고용률 (DT_1DA7012S), 지출목적별 소비자물가지수 (DT_1J22001). Retrieval dates are recorded per question in `data/questions.csv`. KOSIS data: free reuse with attribution, no alteration (KOSIS 이용지침).
- Questions, gold values and logs: CC BY 4.0. Code: MIT (see LICENSE).

## Notes
- `results/results.jsonl` is append-only; downstream scripts use the last `is_error=false` record per (qid, cond, model, rep).
- `results/human_sheet.csv` labels were produced by two independent LLM graders (Claude; blind to conditions, auto verdicts and the results folder; 50/50 agreement) and reviewed by the author (auto verdicts per claim: `results/human_key.csv`), in place of the pre-registered single-author human rating (analysis plan v1.4). That sample targets the v1.4 scorer and oversamples mismatches. `results/audit_unaudited_matches.csv` holds the census of 340 previously unaudited matches behind the v1.5 rule change, and `results/audit_match_sample.csv` holds the random audit of the remaining match paths. `results/archive_v1.4/` keeps the pre-change outputs. `results/human_eval.json` holds agreement/κ/FP/FN.
- `analysis_plan_v1.1_frozen.md` matches its registered hash only after converting CRLF to LF.
- `results/explore_noscaffold.jsonl` is a post-hoc auxiliary run (small tier, no required-value list or rounding rule, 100 generations, not audited). Rescored with the v1.5 scorer: direct 1.18% (6/508), pivot 0.59% (3/510).
- Pilot runs (`results/pilot_results.jsonl`, `results/pilot2_results.jsonl`) are not part of the analysis.
