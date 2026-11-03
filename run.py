#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run.py — Claude Code CLI 호출기 (SPEC §0, §3, §4).

사용 예:
  py -3.11 run.py --dry-run                 # 프롬프트만 출력
  py -3.11 run.py --pilot                   # 5문항 × 4셀 × rep 1
  py -3.11 run.py --workers 3               # 본 실행 (재개 가능)
  py -3.11 run.py --qids P01 --models claude-haiku-5-5 --conds pivot --reps 1

키 (qid, cond, model, rep). results/results.jsonl 에 is_error=false 로 존재하는 키는 건너뜀.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
PROMPTS_DIR = HERE / "prompts"
RESULTS_DIR = HERE / "results"
CWD_DIR = RESULTS_DIR / "_cwd"
DEFAULT_QUESTIONS = HERE / "data" / "questions.csv"
DEFAULT_OUT = RESULTS_DIR / "results.jsonl"

MODELS = ["claude-opus-5-5", "claude-haiku-5-5"]
CONDS = ["direct", "pivot"]
REPS = [1, 2, 3]
EFFORT = "low"
CALL_TIMEOUT = 300          # s, per call (§4)
SEED = 42                   # cell-order shuffle seed (§4)
BACKOFF_BASE = 60           # s
BACKOFF_MAX = 1800          # s
CONSEC_FAIL_LIMIT = 5       # 연속 5회 실패 → 1800s 대기 후 계속 (§4)
RATE_RE = re.compile(r"rate limit|usage limit|overloaded|429|529", re.I)
REJECT_FALLBACK = False     # --reject-fallback: 요청 모델이 아닌 모델이 본문을 생성하면 실패로 간주해 재시도


# 파일럿 qid 배분(표별 2/2/1): questions.csv 의 table_key 순서로 앞에서 뽑는다.
PILOT_PER_TABLE = {"pop": 2, "emp": 2, "cpi": 1}


# ---------------------------------------------------------------- prompts
def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def load_prompt_parts() -> dict:
    return {
        "system": _read(PROMPTS_DIR / "system.txt").strip(),
        "common": _read(Path(os.environ.get("PROMPT_COMMON", str(PROMPTS_DIR / "common.txt")))),
        "direct": _read(PROMPTS_DIR / "direct.txt").strip(),
        "pivot": _read(PROMPTS_DIR / "pivot.txt").strip(),
    }


_PARTS: dict | None = None


def make_prompt(question_row, cond: str) -> tuple[str, str]:
    """questions.csv 한 행 + 조건 → (system_prompt, user_prompt).

    {TABLE_TITLE} {UNIT} {MARKDOWN_TABLE} {QUESTION_KO} {REQUIRED_LIST} {LANG_LINE} 치환.
    str.replace 를 써서 표 안의 중괄호 등에 영향받지 않게 한다.
    """
    global _PARTS
    if _PARTS is None:
        _PARTS = load_prompt_parts()
    if cond not in ("direct", "pivot"):
        raise ValueError(f"unknown cond: {cond}")
    q = question_row
    get = (lambda k: str(q[k])) if not isinstance(q, dict) else (lambda k: str(q[k]))
    user = _PARTS["common"]
    user = user.replace("{TABLE_TITLE}", get("table_title_ko").strip())
    user = user.replace("{UNIT}", get("unit_nm").strip())
    user = user.replace("{MARKDOWN_TABLE}", get("context_markdown").strip())
    user = user.replace("{QUESTION_KO}", get("question_ko").strip())
    user = user.replace("{REQUIRED_LIST}", get("required_list_text").strip())
    user = user.replace("{LANG_LINE}", _PARTS[cond])
    return _PARTS["system"], user.rstrip("\n")


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- CLI resolve
def resolve_claude() -> list[str]:
    """`claude` 실행 파일 경로. Windows 의 npm .cmd 래퍼는 cmd.exe 를 거치며 인용부호·%·! 가
    깨질 수 있으므로, 래퍼가 가리키는 실제 실행 파일(claude.exe 또는 node cli.js)을 직접 호출한다."""
    exe = shutil.which("claude") or shutil.which("claude.cmd")
    if not exe:
        raise FileNotFoundError("claude CLI not found on PATH (need `claude` or `claude.cmd`)")
    if exe.lower().endswith(".cmd"):
        txt = Path(exe).read_text(encoding="utf-8", errors="replace")
        d = Path(exe).parent
        m = re.search(r'"%dp0%\\([^"]+?\.exe)"', txt)
        if m and (d / m.group(1)).exists():
            return [str(d / m.group(1))]
        m = re.search(r'"%dp0%\\([^"]+?\.js)"', txt)
        node = shutil.which("node")
        if m and node and (d / m.group(1)).exists():
            return [node, str(d / m.group(1))]
    return [exe]


def cli_version(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd + ["--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return (r.stdout or r.stderr).strip().splitlines()[0]
    except Exception as e:  # pragma: no cover
        return f"unknown ({e})"


def build_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDECODE") or k.startswith("CLAUDE_CODE_")
                   or k.startswith("CLAUDE_AGENT_SDK") or k in ("CLAUDE_EFFORT", "CLAUDE_PID"))}
    env["MAX_THINKING_TOKENS"] = "0"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def build_cmd(cmd: list[str], model: str, system: str, user: str) -> list[str]:
    # SPEC §0 확정 플래그. --fallback-model 금지.
    return cmd + [
        "-p",
        "--model", model,
        "--safe-mode",
        "--tools", "",
        "--setting-sources", "",
        "--settings", '{"alwaysThinkingEnabled":false}',
        "--no-session-persistence",
        "--effort", EFFORT,
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--output-format", "json",
        "--system-prompt", system,
        user,
    ]


# ---------------------------------------------------------------- jobs
def load_questions(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    need = ["qid", "table_key", "table_title_ko", "unit_nm", "context_markdown",
            "question_ko", "required_list_text"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise SystemExit(f"questions.csv missing columns: {missing}")
    return df


def pilot_qids(df: pd.DataFrame) -> list[str]:
    out = []
    for tk, n in PILOT_PER_TABLE.items():
        out += df.loc[df["table_key"] == tk, "qid"].tolist()[:n]
    return out


def cell_order(qid: str, cells: list[tuple]) -> list[tuple]:
    """문항마다 seed 고정 셔플 (SEED, qid 기반)."""
    seed = int(hashlib.sha256(f"{SEED}:{qid}".encode("utf-8")).hexdigest()[:12], 16)
    rng = random.Random(seed)
    cells = list(cells)
    rng.shuffle(cells)
    return cells


def plan_jobs(df: pd.DataFrame, models, conds, reps, qids_filter) -> list[dict]:
    jobs = []
    for _, row in df.iterrows():
        qid = row["qid"]
        if qids_filter and qid not in qids_filter:
            continue
        # 전체 12셀을 기준으로 셔플한 뒤 필터 → 필터와 무관하게 순서가 고정됨
        full = [(c, m, r) for c in CONDS for m in MODELS for r in REPS]
        for (c, m, r) in cell_order(qid, full):
            if c in conds and m in models and r in reps:
                jobs.append({"qid": qid, "cond": c, "model": m, "rep": r, "row": row})
    return jobs


def load_done(out_path: Path) -> set:
    done = set()
    if not out_path.exists():
        return done
    with out_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("is_error") is False:
                done.add((rec["qid"], rec["cond"], rec["model"], int(rec["rep"])))
    return done


# ---------------------------------------------------------------- throttle
class Throttle:
    """연속 실패 기반 전역 백오프. 모든 워커가 공유한다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.consec = 0
        self.pause_until = 0.0

    def wait_if_paused(self):
        while True:
            with self.lock:
                remain = self.pause_until - time.time()
            if remain <= 0:
                return
            time.sleep(min(remain, 5))

    def on_success(self):
        with self.lock:
            self.consec = 0

    def on_failure(self, reason: str) -> float:
        with self.lock:
            self.consec += 1
            if self.consec >= CONSEC_FAIL_LIMIT:
                delay = BACKOFF_MAX
                self.consec = 0
            else:
                delay = min(BACKOFF_BASE * (2 ** (self.consec - 1)), BACKOFF_MAX)
            self.pause_until = max(self.pause_until, time.time() + delay)
            print(f"  [backoff] {reason} -> all workers pause {delay}s", flush=True)
            return delay


# ---------------------------------------------------------------- one call
def call_once(cmd: list[str], env: dict, model: str, system: str, user: str) -> dict:
    """claude 1회 호출. 반환: {ok, data(dict|None), reason, raw_stdout, returncode, stderr}."""
    full = build_cmd(cmd, model, system, user)
    try:
        r = subprocess.run(full, cwd=str(CWD_DIR), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=CALL_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "data": None, "reason": f"timeout>{CALL_TIMEOUT}s",
                "raw_stdout": "", "returncode": None, "stderr": ""}
    except OSError as e:
        return {"ok": False, "data": None, "reason": f"spawn error: {e}",
                "raw_stdout": "", "returncode": None, "stderr": ""}
    out = r.stdout or ""
    # stdout 만 파싱 (stderr 에는 unrecognized_model 경고가 섞임)
    data = None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # 앞뒤에 잡문이 섞인 경우 첫 '{' 부터 마지막 '}' 까지 재시도
        i, j = out.find("{"), out.rfind("}")
        if i >= 0 and j > i:
            try:
                data = json.loads(out[i:j + 1])
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        return {"ok": False, "data": None,
                "reason": f"stdout parse fail (rc={r.returncode}) stderr={r.stderr.strip()[-300:]!r}",
                "raw_stdout": out, "returncode": r.returncode, "stderr": r.stderr}
    result = data.get("result")
    if data.get("is_error"):
        return {"ok": False, "data": data, "reason": f"is_error status={data.get('api_error_status')} result={str(result)[:200]!r}",
                "raw_stdout": out, "returncode": r.returncode, "stderr": r.stderr}
    if isinstance(result, str) and RATE_RE.search(result) and len(result) < 600:
        # 짧은 본문에 한도/과부하 문구 → 실패로 간주 (긴 정상 본문의 숫자 429 오탐 방지)
        return {"ok": False, "data": data, "reason": f"rate-limit text: {result[:200]!r}",
                "raw_stdout": out, "returncode": r.returncode, "stderr": r.stderr}
    if r.returncode not in (0, None):
        return {"ok": False, "data": data, "reason": f"rc={r.returncode} stderr={r.stderr.strip()[-300:]!r}",
                "raw_stdout": out, "returncode": r.returncode, "stderr": r.stderr}
    sm, _, fb = served_models(data.get("modelUsage") or {}, model)
    if REJECT_FALLBACK and fb:
        return {"ok": False, "data": data, "reason": f"model fallback: requested {model}, served {sm}",
                "raw_stdout": out, "returncode": r.returncode, "stderr": r.stderr}
    if not isinstance(result, str) or not result.strip():
        return {"ok": False, "data": data, "reason": "empty result",
                "raw_stdout": out, "returncode": r.returncode, "stderr": r.stderr}
    return {"ok": True, "data": data, "reason": "", "raw_stdout": out,
            "returncode": r.returncode, "stderr": r.stderr}


def served_models(mu: dict | None, requested: str) -> tuple[str | None, list[str], bool]:
    """modelUsage → (served_model, side_models, model_fallback).
    served_model = outputTokens 최대 키(동률이면 요청 모델 우선). CLI 의 model_refusal_fallback 이 일어나면
    요청 모델 키는 outputTokens=0 으로 남고 대체 모델 키가 본문을 생성하므로, 이 규칙으로 실제 응답 모델이 잡힌다."""
    if not mu:
        return None, [], False
    def key(k):
        return ((mu[k] or {}).get("outputTokens") or 0, 1 if k == requested else 0)
    main = max(mu.keys(), key=key)
    side = [k for k in mu.keys() if k != main]
    return main, side, (main != requested)


def make_record(job, system, user, data: dict | None, attempt: int, version: str,
                is_error: bool, error: str | None) -> dict:
    d = data or {}
    usage = d.get("usage") or {}
    details = usage.get("output_tokens_details") or {}
    mu = d.get("modelUsage") or {}
    sm, side, fb = served_models(mu, job["model"])
    return {
        "qid": job["qid"], "cond": job["cond"], "model": job["model"], "rep": job["rep"],
        "prompt_sha256": sha256(user), "system_sha256": sha256(system),
        "served_model": sm,
        "side_models": side,                # 본문을 만들지 않은 나머지 modelUsage 키
        "model_fallback": fb,               # served_model != 요청 모델 (CLI 안전장치 대체 호출 등)
        "model_usage": mu or None,          # CLI modelUsage 원본 전체
        "usage_raw": usage or None,         # CLI usage 원본 전체
        "response": d.get("result"),
        "input_tokens": usage.get("input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "thinking_tokens": details.get("thinking_tokens"),
        "duration_api_ms": d.get("duration_api_ms"),
        "stop_reason": d.get("stop_reason"),
        "is_error": bool(is_error),
        "api_error_status": d.get("api_error_status"),
        "uuid": d.get("uuid"),
        "cli_version": version, "effort": EFFORT,
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attempt": attempt,
        "error": error,
    }


class Writer:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, rec: dict):
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())


def run_job(job, cmd, env, version, writer: Writer, throttle: Throttle, max_attempts: int,
            stop_event: threading.Event) -> dict:
    system, user = make_prompt(job["row"], job["cond"])
    attempt = 0
    t0 = time.time()
    while True:
        if stop_event.is_set():
            return {"job": job, "status": "aborted"}
        throttle.wait_if_paused()
        attempt += 1
        res = call_once(cmd, env, job["model"], system, user)
        if res["ok"]:
            throttle.on_success()
            rec = make_record(job, system, user, res["data"], attempt, version, False, None)
            writer.append(rec)
            return {"job": job, "status": "done", "rec": rec, "wall": time.time() - t0}
        throttle.on_failure(f"{job['qid']}/{job['cond']}/{job['model']}/r{job['rep']} attempt {attempt}: {res['reason']}")
        if max_attempts and attempt >= max_attempts:
            rec = make_record(job, system, user, res["data"], attempt, version, True, res["reason"])
            writer.append(rec)
            return {"job": job, "status": "failed", "rec": rec, "wall": time.time() - t0}


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--pilot", action="store_true", help="5 qid (pop2/emp2/cpi1) × 4셀 × rep1")
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--conds", default=",".join(CONDS))
    ap.add_argument("--reps", default=",".join(map(str, REPS)))
    ap.add_argument("--qids", default="", help="comma-separated qid filter")
    ap.add_argument("--dry-run", action="store_true", help="프롬프트만 출력, 호출 없음")
    ap.add_argument("--max-attempts", type=int, default=8,
                    help="작업당 최대 시도 횟수 (0=무제한). 초과 시 is_error=true 기록 후 다음 작업")
    ap.add_argument("--limit", type=int, default=0, help="디버그: 처음 N개 작업만")
    ap.add_argument("--reject-fallback", action="store_true",
                    help="요청 모델이 아닌 모델이 본문을 생성한 호출(CLI model_refusal_fallback)을 실패로 간주해 재시도")
    args = ap.parse_args(argv)
    global REJECT_FALLBACK
    REJECT_FALLBACK = args.reject_fallback

    qpath, out_path = Path(args.questions), Path(args.out)
    df = load_questions(qpath)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    conds = [c.strip() for c in args.conds.split(",") if c.strip()]
    reps = [int(r) for r in args.reps.split(",") if r.strip()]
    qids = [q.strip() for q in args.qids.split(",") if q.strip()]
    for m in models:
        if m not in MODELS:
            print(f"[warn] model {m} not in SPEC list {MODELS}", file=sys.stderr)
    if args.pilot:
        reps = [1]
        qids = pilot_qids(df) if not qids else qids
        print(f"[pilot] qids={qids} reps={reps}")

    jobs = plan_jobs(df, models, conds, reps, set(qids))
    if args.limit:
        jobs = jobs[:args.limit]
    done = load_done(out_path)
    todo = [j for j in jobs if (j["qid"], j["cond"], j["model"], j["rep"]) not in done]
    skipped = len(jobs) - len(todo)
    print(f"[plan] questions={len(df)} jobs={len(jobs)} already_done={skipped} todo={len(todo)} "
          f"models={models} conds={conds} reps={reps} out={out_path}")

    if args.dry_run:
        for j in todo:
            system, user = make_prompt(j["row"], j["cond"])
            print("=" * 78)
            print(f"[{j['qid']} | {j['cond']} | {j['model']} | rep {j['rep']}]  "
                  f"prompt_sha256={sha256(user)[:12]} system_sha256={sha256(system)[:12]}")
            print("--- SYSTEM ---")
            print(system)
            print("--- USER ---")
            print(user)
        print("=" * 78)
        print(f"[dry-run] {len(todo)} prompts printed; no calls made")
        return 0

    if not todo:
        print("[done] nothing to do")
        return 0

    CWD_DIR.mkdir(parents=True, exist_ok=True)
    cmd = resolve_claude()
    version = cli_version(cmd)
    env = build_env()
    print(f"[cli] {cmd} version={version!r} cwd={CWD_DIR}")

    writer = Writer(out_path)
    throttle = Throttle()
    stop_event = threading.Event()
    results = []
    t_start = time.time()
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = [ex.submit(run_job, j, cmd, env, version, writer, throttle, args.max_attempts, stop_event)
                    for j in todo]
            for i, fut in enumerate(as_completed(futs), 1):
                r = fut.result()
                results.append(r)
                j = r["job"]
                if r["status"] == "done":
                    rec = r["rec"]
                    print(f"[{i}/{len(todo)}] OK  {j['qid']} {j['cond']:6s} {j['model']:16s} r{j['rep']} "
                          f"served={rec['served_model']} out={rec['output_tokens']} think={rec['thinking_tokens']} "
                          f"api={rec['duration_api_ms']}ms attempt={rec['attempt']}"
                          + ("  ** FALLBACK side=" + ",".join(rec['side_models']) if rec['model_fallback'] else ""), flush=True)
                elif r["status"] == "failed":
                    print(f"[{i}/{len(todo)}] FAIL {j['qid']} {j['cond']} {j['model']} r{j['rep']}: {r['rec']['error']}", flush=True)
    except KeyboardInterrupt:
        stop_event.set()
        print("\n[abort] KeyboardInterrupt — 완료된 건은 저장됨, 재실행 시 재개", flush=True)

    # summary
    done_recs = [r["rec"] for r in results if r["status"] == "done"]
    failed = [r for r in results if r["status"] == "failed"]
    tot_in = sum((r.get("input_tokens") or 0) + (r.get("cache_creation_input_tokens") or 0)
                 + (r.get("cache_read_input_tokens") or 0) for r in done_recs)
    tot_out = sum(r.get("output_tokens") or 0 for r in done_recs)
    tot_think = sum(r.get("thinking_tokens") or 0 for r in done_recs)
    print("=" * 78)
    print(f"[summary] done={len(done_recs)} failed={len(failed)} skipped(resume)={skipped} "
          f"wall={time.time() - t_start:.0f}s")
    print(f"[summary] tokens: input(incl. cache)={tot_in} output={tot_out} thinking={tot_think}")
    for m in models:
        ds = [r.get("duration_api_ms") for r in done_recs if r["model"] == m and r.get("duration_api_ms")]
        served = sorted({r.get("served_model") for r in done_recs if r["model"] == m})
        nfb = sum(1 for r in done_recs if r["model"] == m and r.get("model_fallback"))
        if ds:
            print(f"[summary] {m}: n={len(ds)} mean_api={sum(ds) / len(ds) / 1000:.1f}s served={served} fallback={nfb}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
