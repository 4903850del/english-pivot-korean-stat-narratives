# 실험 구현 스펙 v1 (2026-11-02)

논문: 영어 피벗(단일 프롬프트 교차언어 사고) 전략이 LLM 한국어 통계 서술의 수치 충실도에 미치는 영향.
설계 근거: `../검증자_검토_보고_2026-11-02.md` (반드시 먼저 읽을 것). 이 문서는 코드 간 인터페이스를 고정한다.

## 0. 환경
- Python: `py -3.11`, 항상 `PYTHONUTF8=1`. 설치됨: numpy, scipy 1.17, pandas 2.2.3(3.x는 DLL 차단), matplotlib, requests, python-dotenv.
- 모든 파일 입출력 `encoding="utf-8"`; CSV 저장은 `utf-8-sig`.
- LLM 호출: Claude Code 구독, CLI `claude`(Git Bash) / `claude.cmd`(PowerShell). 검증된 호출:
  ```
  claude -p --model <ID> --safe-mode --tools "" --setting-sources "" \
    --settings '{"alwaysThinkingEnabled":false}' --no-session-persistence --effort low \
    --disable-slash-commands --strict-mcp-config --output-format json \
    --system-prompt "<SYSTEM>" "<USER PROMPT>"
  ```
  - 모델 ID: `claude-opus-5-5`(large), `claude-haiku-5-5`(small). `modelUsage` 키로 실제 ID 확인됨. **`--fallback-model` 금지.**
  - 환경변수: `MAX_THINKING_TOKENS=0`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. 서브프로세스 env에서 `CLAUDECODE`, `CLAUDE_CODE_*` 변수는 제거.
  - 실행 cwd는 CLAUDE.md가 없는 빈 폴더(`experiment/results/_cwd/`).
  - stdout JSON 필드: `result`(본문), `usage.output_tokens`, `usage.output_tokens_details.thinking_tokens`, `usage.input_tokens`, `usage.cache_creation_input_tokens`, `modelUsage{<id>}`, `duration_api_ms`, `is_error`, `api_error_status`, `stop_reason`, `uuid`. stderr에 `unrecognized_model` 경고가 섞이므로 stdout만 파싱.
  - 확장 사고는 완전히 꺼지지 않음(스모크: Haiku 180, Opus 71 토큰). 호출마다 `thinking_tokens` 기록.
- KOSIS 키: `experiment/.env`의 `KOSIS_API_KEY`. 코드에 하드코딩 금지, `.env`는 gitignore.

## 1. 폴더
```
experiment/
  SPEC.md                     이 문서
  .env / .env.example
  data/raw/*.json             KOSIS 원본 응답(그대로 저장)
  data/tables.json            표 3종 메타(orgId, tblId, objL*, itmId, prdSe, 기간, 단위, 출처 문자열)
  data/questions.csv          50문항 (스키마 §2)
  data/fetch_kosis.py         KOSIS 조회 → raw 저장 → 정규 테이블 CSV
  data/build_questions.py     템플릿 → questions.csv (gold 계산 포함)
  data/verify_gold.py         재조회 후 gold_hash 대조
  prompts/system.txt          공통 시스템 프롬프트(1~2문장)
  prompts/common.txt          공통 사용자 프롬프트 템플릿({LANG_LINE} 자리)
  prompts/direct.txt          LANG_LINE (한국어 분석)
  prompts/pivot.txt           LANG_LINE (영어 분석)
  run.py                      호출기 (§4)
  gate.py                     무결성 게이트 (§6)
  normalize.py                수치 추출·정규화 (§5)
  score.py                    채점 (§5)
  analyze.py                  부트스트랩·표2·그림2 (§7)
  human_sample.py             사람 채점 시트 (§8)
  tests/test_normalize.py     라운드트립 테스트 30문장
  results/results.jsonl       호출 로그 (§4)
  results/claims.jsonl, per_question.csv, table2.csv, summary.json
  figures/fig2_forest.png
  analysis_plan_v1.md         사전 고정 분석 계획 (파일럿 후 해시 기록)
```

## 2. questions.csv (UTF-8-sig, JSON 열은 문자열)
`qid, table_key(pop|emp|cpi), template_id, org_id, tbl_id, itm_id, obj_codes(json), prd_se, periods(json), unit_nm, fetch_date, lst_chn_de, source_citation, table_title_ko, context_cells(json), context_markdown, question_ko, required_list_text, gold_required(json), gold_aux(json), rounding_rule_text, gold_hash`
- `context_cells`: `[{row_label, col_label, period, dt_raw, value(float), flag}]` — 프롬프트에 보이는 셀 전부.
- `context_markdown`: 한국어 헤더의 마크다운 표(단위 포함). 프롬프트에 그대로 삽입.
- `gold_required`: `[{name, type(PCT|PP|RATIO|COUNT|INDEX|RANK), exact(float), display(str), decimals(int), sign(-1|0|1), formula(str)}]` 3~5개. 질문이 명시적으로 요구하는 값.
- `gold_aux`: 같은 스키마. 정당한 중간값(절대 증감, 역비율, 합계 등) 2~4개.
- `required_list_text`: "1) 2024년 서울 인구의 전년 대비 증감률(%)  2) …" (이름만, 값 없음).
- gold 계산 규칙: **context_cells.value만으로** 계산(KOSIS 등락률 열 미사용). 증감률 `(v_t/v_{t-1}-1)*100` 1자리; %p 격차 `a-b` 1자리; 성비 `m/f*100` 1자리; 배율 2자리; 비중 1자리; CPI 누적 `idx-100` 1자리; 순위 내림차순 `min`. 반올림은 `decimal.ROUND_HALF_UP`. `exact`는 미반올림.
- 표별 배분(50): pop 17 / emp 17 / cpi 16. 템플릿은 검증 보고서 1-9 참조(P1~P5, E1~E5, C1~C5). E2·E5는 %와 %p를 한 문장에 같이 요구.
- 잠정치: 2022~2024 확정치만. `smblChk=Y` 조회에서 부호 붙은 셀 제외.
- `gold_hash` = sha256(gold_required + context_cells 직렬화).

## 3. 프롬프트
- `system.txt`: "당신은 국가통계 해설을 작성하는 분석가입니다. 주어진 표의 값만 사용하고, 지시된 출력 형식을 정확히 지키십시오."
- `common.txt` (두 조건 공통, `{LANG_LINE}`만 교체):
  ```
  [표] {TABLE_TITLE} (출처: KOSIS, 단위: {UNIT})
  {MARKDOWN_TABLE}

  [질문] {QUESTION_KO}

  [필수 포함 값] {REQUIRED_LIST}

  [작성 규칙]
  1. 출력은 아래 세 마커를 정확히 이 순서로 포함합니다: ===ANALYSIS=== / ===NARRATIVE=== / ===END===
  2. {LANG_LINE}
  3. ===NARRATIVE=== 절은 한국어로만 4~6문장으로 작성하고, 필수 포함 값을 모두 숫자로 명시합니다.
  4. 계산은 표에 제시된 값만 사용하며, 표에 없는 외부 수치는 쓰지 않습니다.
  5. 비율·증감률·격차는 소수 첫째 자리, 배율은 소수 둘째 자리까지 반올림합니다. 비율의 차이는 %p로 표기합니다.
  ```
- `direct.txt`: `===ANALYSIS=== 절에서 표의 수치를 한국어로 다시 정리하고, 한국어로 단계적으로 계산·해석하십시오.`
- `pivot.txt`: `===ANALYSIS=== 절에서 표의 수치를 영어로 다시 정리하고, 영어로 단계적으로 계산·해석하십시오.`
  - (v1.1) 영어 괄호 문구 "(Restate the table and reason step by step in English.)"는 삭제. 이 문구가 Opus 5.5의 안전장치(reasoning_extraction)를 건드려 CLI가 자동으로 claude-opus-5로 대체 생성(파일럿 2/5, 프로브 3/3 재현)했고, 삭제 후 6/6 정상. 본 실행은 `--reject-fallback`으로 대체 건을 재시도하고 `model_fallback`·`side_models`를 기록.
- `diff prompts/direct.txt prompts/pivot.txt` 결과를 README에 기록.

## 4. run.py / results.jsonl
- 셀: cond ∈ {direct, pivot} × model ∈ {claude-opus-5-5, claude-haiku-5-5} × rep ∈ {1,2,3}. 키 `(qid, cond, model, rep)`.
- 순서: 문항별로 4셀×3회를 **교차(interleave)**, 셀 순서는 문항마다 seed 고정 셔플. 완료 건은 건너뜀(재개 가능). 동시 실행 최대 3(`--workers 3`).
- 재시도: `is_error` 또는 stdout 파싱 실패 또는 `result`에 `rate limit|usage limit|overloaded|429|529` → 60s·120s·240s·…최대 1800s 백오프, 연속 5회 실패 시 1800s 대기 후 계속. 호출당 timeout 300s.
- `--pilot`: qid 5개(표별 2/2/1) × 4셀 × rep 1만.
- `--models`, `--conds`, `--reps`, `--qids` 필터 옵션.
- 레코드: `{qid, cond, model, rep, prompt_sha256, system_sha256, served_model, response(result 전문), input_tokens, cache_creation_input_tokens, output_tokens, thinking_tokens, duration_api_ms, stop_reason, is_error, api_error_status, uuid, cli_version, effort, ts_utc, attempt}`.

## 5. normalize.py / score.py / claims.jsonl
- 파싱: `===ANALYSIS===(.*?)===NARRATIVE===(.*?)===END===` DOTALL. 실패 → `format_fail=True`, 전체를 NARRATIVE로 간주(ITT).
- 추출 대상: NARRATIVE 절만. NFKC 정규화 후 정규식.
- 주장 타입: PCT(%·퍼센트·프로), PP(%p·%P·%포인트·퍼센트포인트·포인트·p.p.·pp — **%보다 먼저**), RATIO(배), COUNT(명·천명·만명·원·만원·억원 + 만/억/조 합성), INDEX(단위 없음, UNK 폴백), RANK(위·번째로).
- 제외: `\d{4}년`, `\d{1,2}월`, `\d분기`, `\d+세`·`\d+~\d+세`, 개·곳·종·가지·단계·번·회·차례 앞 숫자, `=100`·`2020=100`, 마커 내 숫자, `[n]`.
- 정규화: 천단위 쉼표 제거; 만/억/조 합성(5천1백만=51,000,000; 5,163만=51,630,000; 939.4만=9,394,000); 단위 천명 ×10³, 만명 ×10⁴, 억원 ×10⁸; 소수 자릿수 d 기록(합성 표기는 유효 해상도 기록); 부호 `- − － 마이너스`; 근사어 약·대략·거의·가량·남짓·정도·쯤·안팎·여 → approx; 증감 어휘(숫자 뒤 15자: 증가/늘/상승/올라/높아 +, 감소/줄/하락/떨어/낮아 −) → direction. 범위 `a~b%`는 2주장. 동일 (type, value, approx) 중복 제거.
- 허용오차: PCT/PP/RATIO/INDEX `0.5×10^-d`; COUNT `0.5×해상도`; approx면 `max(tol, 0.01×|gold_exact|)`; RANK 정확 일치. 비교는 `|claim|` vs `|gold_exact|`.
- 매칭(의사코드):
  ```
  gold = context_cells(COUNT/INDEX) ∪ gold_required ∪ gold_aux
  for c in claims:
    cands = same-type gold (UNK → INDEX, PCT, PP, RATIO 순)
    hit = first within tol
    if hit: verdict=MATCH (+K6 direction_error→MISMATCH if direction known and opposite); flags precision_loss/unit_missing
    else: cross: PCT↔PP, COUNT×{1e±3,1e±4,1e±8} within tol → K4 unit_error (MISMATCH, H+U)
          elif same-type within 10×tol → K2 rounding/digit (MISMATCH, H+U)
          elif type∈{PCT,PP,RATIO} and gold_required same type exists → K3 calc (MISMATCH, H)
          else K1 fabricated (MISMATCH, H)
  ```
- 문항·응답 단위 지표: `H = #MISMATCH/#claims` (claims=0 → H=1.0, flag zero_claims); `U = #(K2∪K4)/#claims`; `C = #(gold_required matched ≥1)/#gold_required`; `H_cell`(COUNT/INDEX 원 셀 주장만), `H_derived`(PCT/PP/RATIO/RANK 주장만); `n_claims`; `n_mismatch`; `n_unitdigit`.
- claims.jsonl: `{qid, cond, model, rep, claim_id, span, type, value, decimals, approx, direction, verdict, code(K1..K6|None), matched_gold_name, sentence}`.
- per_question.csv: 응답 단위 행 + 문항×셀 평균(rep 평균) 행을 별도 파일 `per_cell_question.csv`.
- 자동 판정은 값·단위·방향만. 귀속(K5)·시점(K7)은 사람 채점 라벨에만.

## 6. gate.py
- 두 조건 모두 검사: (1) 마커 3개 존재, (2) ANALYSIS 절 문자 중 지정 언어 비율 ≥ 0.90 (pivot: latin/(latin+hangul); direct: hangul/(latin+hangul)), 숫자·기호 제외, (3) ANALYSIS 절의 COUNT/INDEX 숫자(영문 `9,386,034`, `9.39 million`×10⁶, `thousand`×10³ 처리) 전부가 context_cells ∪ gold_aux 허용오차 내 + context_cells ≥1 재진술. 조건별 통과율·항목별 통과율 로그.
- 부수: NARRATIVE 절 라틴 문자 비율(`en_leak_ratio`, 단위·약어 화이트리스트 제외), `analysis_len_chars`.
- 결과: `results/gate.csv` `{qid,cond,model,rep,format_ok,lang_ok,num_ok,gate_pass,en_leak_ratio}`. 분석은 ITT, 통과 부분집합은 민감도.

## 7. analyze.py
- 분석 단위: 문항(50). 셀별 문항 값 = 3회 반복의 n_mismatch·n_claims 합.
- 통계량: ratio-of-sums. 재표집한 문항 집합에서 `H_cond = ΣN_mismatch/ΣN_claims`, Δ = H_pivot − H_direct.
- 1차: 모델 풀링 주효과 Δ_pooled(두 모델 합산). 2차: Δ_small, Δ_large, 교호작용 Δ_small−Δ_large, 같은 지표로 U, C, H_cell, H_derived. 보정 없음.
- 부트스트랩: 문항 재표집 B=10,000, `numpy.random.default_rng(42)`, BCa 직접 구현 또는 `scipy.stats.bootstrap(paired=True, method='BCa')`; 퇴화(차분 비영 문항 <5 또는 NaN) 시 percentile로 대체하고 플래그. 비영 문항 수 보고.
- MDE(사후 민감도): 문항별 Δ_q SD × 2.80/√50 (paired t, α=.05, power .80). SESOI = 2%p; 구간이 (−2, 2) 안이면 bounded, 0 포함·SESOI 미배제면 undetected, 0 미포함이면 detected.
- 출력: `results/table2.csv` (Strategy, Model, H%, U%, C%, claims/answer, ΔH[CI], tokens/answer, narrative_chars, gate_pass%), `results/summary.json`(모든 Δ·CI·MDE·플래그·반복 간 SD), `figures/fig2_forest.png`(영문 라벨, 0 기준선, pooled/small/large 3줄, 300dpi).

## 8. human_sample.py
- claims.jsonl에서 4셀 × 자동판정{MATCH, MISMATCH} 8층 균등 50건(층당 6~7, 부족 층은 전부 + MATCH로 보충), seed 42.
- 시트 `results/human_sheet.csv`: `claim_id, question_ko, context_markdown, gold_required_display, sentence(스팬 **굵게**), human_label(supported|unsupported|unit_error|not_a_claim), missed_claims(int), note`. 조건·모델·자동판정 열은 **별도 키 파일** `results/human_key.csv`에만.
- `--score`: 라벨 입력 후 일치율, Cohen κ(자체 계산), FP(자동 MISMATCH∧사람 supported), FN(자동 MATCH∧사람 unsupported), not_a_claim, missed 합계 → `results/human_eval.json`.

## 9. 합격 기준
- tests/test_normalize.py 30문장 전부 통과.
- 파일럿 20건: format_fail 0건 목표(≤2건 허용), gate 통과율 보고, Opus-direct H 확인(<2%면 문항 난이도 상향 분기).
- 본 실행 600행 완결, is_error 0.
- analyze.py가 파일럿 데이터로 끝까지 돌고 그림이 생성됨.
