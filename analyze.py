# -*- coding: utf-8 -*-
"""analyze.py — 문항 클러스터 부트스트랩(ratio-of-sums), 표2, 그림2 (SPEC §7).

입력:  results/per_cell_question.csv, results/per_question.csv (score.py 산출), results/gate.csv (있으면)
출력:  results/table2.csv, results/summary.json, figures/fig2_forest.png

실행:  PYTHONUTF8=1 py -3.11 analyze.py [--indir results] [--figdir figures] [--B 10000] [--seed 42] [--sesoi 2.0]

설계(스펙 모호점 해소 포함):
  * 분석 단위 = 문항. 셀별 문항 값 = rep 합(n_mismatch_eff, n_claims_eff 등; claims=0 응답은 1/1 로 H=1 반영).
  * 통계량 = ratio-of-sums. H_cond = Σ_q Σ_model num / Σ_q Σ_model den; Δ = H_pivot − H_direct (단위: %p = ×100).
  * 1차: pooled(두 모델 합산). 2차: small(haiku), large(opus), interaction(Δ_small − Δ_large), 지표 U/C/H_cell/H_derived.
  * 부트스트랩: 문항 재표집 B회, rng=default_rng(seed). 한 번 뽑은 (B×Q) 인덱스 행렬을 모든 대비·지표에 공유(대응 재표집).
  * BCa 직접 구현(z0: 부트스트랩 분포에서 θ̂ 미만 비율, a: 문항 jackknife). 퇴화(Δ_q≠0 문항 <5, z0/a NaN·무한, 부트 분포 상수)
    → percentile 로 대체하고 ci_method='percentile', bca_degenerate=True 플래그.
  * MDE(사후 민감도) = 2.80 × SD(Δ_q) / √Q (%p). SESOI 분류: 0∉CI → detected; CI⊂(−SESOI, SESOI) → bounded; 그 외 undetected.
    (0∉CI 이면서 CI⊂(−SESOI,SESOI) 인 경우 'detected' 로 두고 bounded_within_sesoi=True 를 따로 기록.)
  * 모델 등급: 모델명에 'haiku' → small, 'opus' → large (그 외 이름은 사전순으로 small/large 배정, 플래그 기록).
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.stats import beta, norm

HERE = os.path.dirname(os.path.abspath(__file__))
METRICS = {
    "H": ("n_mismatch_eff", "n_claims_eff"),
    "U": ("n_unitdigit", "n_claims_eff"),
    "C": ("n_required_matched", "n_required"),
    "H_cell": ("n_cell_mismatch", "n_cell_claims"),
    "H_derived": ("n_derived_mismatch", "n_derived_claims"),
    "H_required": ("n_req_mismatch", "n_req_claims"),
    "H_other": ("n_other_mismatch", "n_other_claims"),
}
CONDS = ("direct", "pivot")
B_LABEL = 10000  # 그림 제목용; run() 에서 실제 B 로 갱신


# ---------------------------------------------------------------------------
def model_tiers(models):
    small = [m for m in models if "haiku" in m.lower()]
    large = [m for m in models if "opus" in m.lower()]
    flag = False
    if not (small and large):
        ms = sorted(models)
        small, large, flag = ms[:1], ms[-1:], True
    return small[0], large[0], flag


def to_arrays(df: pd.DataFrame, qids, models, num_col, den_col):
    """(Q, 2 conds, 2 models) 배열. 없는 셀은 0."""
    Q = len(qids)
    num = np.zeros((Q, 2, 2))
    den = np.zeros((Q, 2, 2))
    qi = {q: i for i, q in enumerate(qids)}
    mi = {m: j for j, m in enumerate(models)}
    for _, r in df.iterrows():
        if r["qid"] in qi and r["model"] in mi and r["cond"] in CONDS:
            i, c, j = qi[r["qid"]], CONDS.index(r["cond"]), mi[r["model"]]
            num[i, c, j] = 0 if pd.isna(r[num_col]) else float(r[num_col])
            den[i, c, j] = 0 if pd.isna(r[den_col]) else float(r[den_col])
    return num, den


def ratio(num, den):
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)


def contrast_stat(num_s, den_s, kind: str):
    """num_s, den_s: (..., 2 conds, 2 models) 합계. 반환: Δ (pivot − direct) in %p."""
    if kind == "pooled":
        h = ratio(num_s[..., :, :].sum(-1), den_s[..., :, :].sum(-1))  # (..., 2)
        return 100 * (h[..., 1] - h[..., 0])
    if kind in ("small", "large"):
        j = 0 if kind == "small" else 1
        h = ratio(num_s[..., :, j], den_s[..., :, j])
        return 100 * (h[..., 1] - h[..., 0])
    if kind == "interaction":
        h = ratio(num_s, den_s)  # (..., 2, 2)
        d_small = h[..., 1, 0] - h[..., 0, 0]
        d_large = h[..., 1, 1] - h[..., 0, 1]
        return 100 * (d_small - d_large)
    raise ValueError(kind)


def bca_interval(theta_hat, boots, jack, alpha=0.05):
    """BCa. 반환 (lo, hi, info). 퇴화 시 info['degenerate']=True 와 percentile 값."""
    boots = boots[np.isfinite(boots)]
    info = {"degenerate": False, "reason": ""}
    if boots.size < 100 or not np.isfinite(theta_hat):
        return np.nan, np.nan, {"degenerate": True, "reason": "too_few_finite_bootstraps"}
    p_lo, p_hi = np.percentile(boots, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    if np.allclose(boots, boots[0]):
        return p_lo, p_hi, {"degenerate": True, "reason": "constant_bootstrap_distribution"}
    prop = np.mean(boots < theta_hat)
    if prop <= 0 or prop >= 1:
        return p_lo, p_hi, {"degenerate": True, "reason": "z0_infinite"}
    z0 = norm.ppf(prop)
    jack = jack[np.isfinite(jack)]
    jm = jack.mean()
    denom = 6.0 * (np.sum((jm - jack) ** 2) ** 1.5)
    if denom == 0 or not np.isfinite(denom):
        return p_lo, p_hi, {"degenerate": True, "reason": "acceleration_undefined"}
    a = np.sum((jm - jack) ** 3) / denom
    za = norm.ppf([alpha / 2, 1 - alpha / 2])
    adj = z0 + (z0 + za) / (1 - a * (z0 + za))
    probs = norm.cdf(adj)
    if not np.all(np.isfinite(probs)):
        return p_lo, p_hi, {"degenerate": True, "reason": "adjusted_probs_nan"}
    lo, hi = np.percentile(boots, 100 * probs)
    info.update(z0=float(z0), a=float(a))
    return float(lo), float(hi), info


def clopper_pearson(k: int, n: int, alpha: float = 0.05):
    """주장 단위 정확 이항 CI (양측 1-alpha). k=0 → upper = 1-(alpha/2)^(1/n)."""
    if n <= 0:
        return float("nan"), float("nan")
    lo = 0.0 if k == 0 else float(beta.ppf(alpha / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1 - alpha / 2, k + 1, n - k))
    return lo, hi


def exact_bounds_block(k: int, n: int, boots=None, alpha: float = 0.05):
    """셀/조건별 H(또는 U) 정확 상한 + 문항 클러스터 부트스트랩 일측 95% 상한(비퇴화 시)."""
    lo, hi = clopper_pearson(k, n, alpha)
    out = {"N_claims": int(n), "N_mismatch": int(k), "estimate_pct": (100 * k / n) if n else None,
           "exact_ci95_pct": [100 * lo, 100 * hi], "upper95_exact_pct": 100 * hi, "upper95_cluster_pct": None,
           "cluster_upper95_pct": None, "cluster_adjust_unavailable": True, "upper95_used_pct": 100 * hi,
           "upper95_basis": "exact (cluster bootstrap degenerate)"}
    if boots is not None:
        b = np.asarray(boots, dtype=float)
        b = b[np.isfinite(b)]
        if b.size >= 100 and not np.allclose(b, b[0]):
            cu = 100 * float(np.percentile(b, 95))
            # 사용 상한 = max(클러스터 부트스트랩 일측 95% 상한, 정확 이항 상한): 조건별 부트스트랩이 퇴화에 가까우면 클러스터 상한이 더 작아질 수 있음
            out.update(upper95_cluster_pct=cu, cluster_upper95_pct=cu, cluster_adjust_unavailable=False,
                       upper95_used_pct=max(cu, 100 * hi),
                       upper95_basis="max(cluster_bootstrap_one_sided, exact_binomial)")
    return out


def analyze_contrast(num, den, kind, idx, sesoi, min_nonzero=5):
    """num, den: (Q,2,2). idx: (B,Q) 재표집 인덱스."""
    Q = num.shape[0]
    theta_hat = float(contrast_stat(num.sum(0), den.sum(0), kind))
    boots = contrast_stat(num[idx].sum(1), den[idx].sum(1), kind)
    jack = np.array([contrast_stat(np.delete(num, i, 0).sum(0), np.delete(den, i, 0).sum(0), kind) for i in range(Q)])
    # 문항별 Δ_q (MDE 용) 및 비영 문항 수
    dq = np.array([contrast_stat(num[i], den[i], kind) for i in range(Q)])
    dq_f = dq[np.isfinite(dq)]
    n_nonzero = int(np.sum(np.abs(dq_f) > 1e-12))
    bf_all = boots[np.isfinite(boots)]
    pct_lo, pct_hi = (np.percentile(bf_all, [2.5, 97.5]) if bf_all.size else (np.nan, np.nan))
    lo, hi, info = bca_interval(theta_hat, boots, jack)
    method = "BCa"
    if info["degenerate"] or n_nonzero < min_nonzero:
        if not info["degenerate"]:
            info = {"degenerate": True, "reason": f"nonzero_questions<{min_nonzero}"}
        method = "percentile"
        bf = boots[np.isfinite(boots)]
        lo, hi = (np.percentile(bf, [2.5, 97.5]) if bf.size else (np.nan, np.nan))
        lo, hi = float(lo), float(hi)
    sd = float(np.std(dq_f, ddof=1)) if dq_f.size > 1 else float("nan")
    mde = 2.80 * sd / math.sqrt(dq_f.size) if dq_f.size > 0 else float("nan")
    detected = np.isfinite(lo) and np.isfinite(hi) and (lo > 0 or hi < 0)
    bounded = np.isfinite(lo) and np.isfinite(hi) and (lo > -sesoi and hi < sesoi)
    if detected:
        cls = "detected"
    elif bounded:
        cls = "bounded"
    else:
        cls = "undetected"
    return {
        "estimate_pp": theta_hat, "ci_low_pp": lo, "ci_high_pp": hi, "ci_method": method,
        "bca_degenerate": bool(info.get("degenerate", False)), "degenerate_reason": info.get("reason", ""),
        "bca_z0": info.get("z0"), "bca_a": info.get("a"),
        "pct_ci_low_pp": float(pct_lo), "pct_ci_high_pp": float(pct_hi),  # 사전등록 폴백 percentile CI (항상 보고)
        "n_questions": int(dq_f.size), "n_nonzero_questions": n_nonzero,
        "delta_q_sd_pp": sd, "mde_pp": mde, "sesoi_pp": sesoi, "sesoi_class": cls,
        "bounded_within_sesoi": bool(bounded), "boot_sd_pp": float(np.nanstd(boots)),
    }


# ---------------------------------------------------------------------------
BOUND_LABEL = "two one-sided 95% upper bounds (max of cluster bootstrap and exact binomial), joint \u2265 90%"


def _delta_text(d):
    if d.get("bca_degenerate") and "delta_bound" in d:
        b = d["delta_bound"]
        return f"{d['estimate_pp']:+.2f} [{b['lower_pp']:+.2f}, {b['upper_pp']:+.2f}]*"
    return f"{d['estimate_pp']:+.2f} [{d['ci_low_pp']:+.2f}, {d['ci_high_pp']:+.2f}]"


def _r2(x):
    return None if x is None else round(x, 2)


def _delta_basis(d):
    if d.get("bca_degenerate") and "delta_bound" in d:
        return "* " + BOUND_LABEL + ": [-U95(H_direct), +U95(H_pivot)]"
    return f"{d['ci_method']} 95% CI (question-cluster bootstrap)"


def forest_plot(rows, path, sesoi):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from matplotlib.lines import Line2D

    # 단일 단(83 mm ≈ 3.3 in) 폭, 글꼴 ≥ 7 pt
    FS = 7
    plt.rcParams.update({"font.size": FS, "axes.labelsize": FS, "xtick.labelsize": FS, "ytick.labelsize": FS})
    labels = [r["label"].replace(" (", "\n(") for r in rows]
    est = np.array([r["estimate_pp"] for r in rows])
    degen = np.array([bool(r.get("bca_degenerate")) and "delta_bound" in r for r in rows])
    # 1차 구간: 비퇴화 = 사전등록 부트스트랩 CI(실선), 퇴화 = 보수적 경계(점선)
    lo = np.array([r["delta_bound"]["lower_pp"] if dg else r["ci_low_pp"] for r, dg in zip(rows, degen)])
    hi = np.array([r["delta_bound"]["upper_pp"] if dg else r["ci_high_pp"] for r, dg in zip(rows, degen)])
    # 보수적 경계(모든 행, 얇은 회색 배경 수염)
    blo = np.array([r["delta_bound"]["lower_pp"] if "delta_bound" in r else np.nan for r in rows])
    bhi = np.array([r["delta_bound"]["upper_pp"] if "delta_bound" in r else np.nan for r in rows])
    y = np.arange(len(rows))[::-1] * 1.0
    fig, ax = plt.subplots(figsize=(3.3, 2.7))
    ax.axvspan(-sesoi, sesoi, color="0.93", zorder=0)
    ax.axvline(0, color="0.3", lw=0.8, zorder=1)
    for yi, l, h in zip(y, blo, bhi):
        if np.isfinite(l) and np.isfinite(h):
            ax.plot([l, h], [yi, yi], color="0.78", lw=3.0, solid_capstyle="butt", zorder=2)
    for yi, xi, l, h, dg in zip(y, est, lo, hi, degen):
        ls = "--" if dg else "-"
        ax.plot([l, h], [yi, yi], ls=ls, color="black", lw=1.0, zorder=3)
        ax.plot([l, l], [yi - 0.09, yi + 0.09], color="black", lw=1.0, zorder=3)
        ax.plot([h, h], [yi - 0.09, yi + 0.09], color="black", lw=1.0, zorder=3)
        ax.plot(xi, yi, "o", color="black", ms=4, zorder=4)
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel("ΔH = H(English-pivot) − H(Korean-direct), pp")
    span = max(abs(np.nanmin(np.r_[lo, blo])), abs(np.nanmax(np.r_[hi, bhi])), sesoi) * 1.15
    ax.set_xlim(-span, span)
    ax.set_ylim(-0.75, len(rows) - 0.35)
    for yi, r, l, h, dg in zip(y, rows, lo, hi, degen):
        tag = "bounds" if dg else r["ci_method"]
        ax.annotate(f"{r['estimate_pp']:+.2f} [{l:+.2f}, {h:+.2f}] ({tag})", xy=(0, yi - 0.36), ha="center", va="center",
                    fontsize=FS, color="0.25", zorder=5,
                    bbox=dict(boxstyle="square,pad=0.08", fc="0.93", ec="none"))  # 0 기준선이 글자를 가로지르지 않도록
    ax.spines[["top", "right"]].set_visible(False)
    fig.text(0.02, 0.965, f"B={B_LABEL:,} question-cluster bootstrap; shaded = SESOI ±{sesoi:g} pp", fontsize=FS, ha="left", va="top")
    handles = [
        Line2D([0], [0], color="black", marker="o", ms=4, lw=1.0, label="Pre-registered bootstrap 95% CI (BCa/percentile)"),
        Line2D([0], [0], color="black", ls="--", lw=1.0, label="Bounds used as interval (bootstrap degenerate)"),
        Line2D([0], [0], color="0.78", lw=3.0, label="Conservative bounds: two one-sided 95%\n(max cluster-bootstrap / exact binomial), joint ≥ 90%"),
    ]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.005), ncol=1, fontsize=FS, frameon=False,
               handlelength=1.8, borderaxespad=0.0)
    # 83 mm 단 폭에 맞춰 그림 폭 3.3 in 을 그대로 유지(여백 안에 y 라벨·범례를 배치, tight 자르기 없음)
    fig.set_size_inches(3.3, 3.0)
    fig.subplots_adjust(left=0.30, right=0.97, top=0.91, bottom=0.40)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=300)
    plt.close(fig)


# ---------------------------------------------------------------------------
def run(indir, figdir, B=10000, seed=42, sesoi=2.0, min_nonzero=5):
    cell = pd.read_csv(os.path.join(indir, "per_cell_question.csv"), encoding="utf-8-sig", dtype={"qid": str})
    pq = pd.read_csv(os.path.join(indir, "per_question.csv"), encoding="utf-8-sig", dtype={"qid": str})
    gate_path = os.path.join(indir, "gate.csv")
    gate = pd.read_csv(gate_path, encoding="utf-8-sig", dtype={"qid": str}) if os.path.exists(gate_path) else None

    models = sorted(cell["model"].unique())
    small, large, tier_flag = model_tiers(models)
    models = [small, large]
    # 완전 문항(4셀 모두 존재)만 사용, 불완전 문항은 보고
    counts = cell[cell["model"].isin(models)].groupby("qid").size()
    qids = sorted(counts[counts == 4].index)
    incomplete = sorted(set(cell["qid"]) - set(qids))
    Q = len(qids)
    global B_LABEL
    B_LABEL = B
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, Q, size=(B, Q))

    contrasts = {}
    for metric, (ncol, dcol) in METRICS.items():
        num, den = to_arrays(cell, qids, models, ncol, dcol)
        contrasts[metric] = {}
        for kind in ("pooled", "small", "large", "interaction"):
            contrasts[metric][kind] = analyze_contrast(num, den, kind, idx, sesoi, min_nonzero)
        # 조건×모델 점추정
        s_num, s_den = num.sum(0), den.sum(0)
        contrasts[metric]["point"] = {
            f"{c}|{m}": (100 * float(s_num[ci, mi] / s_den[ci, mi]) if s_den[ci, mi] > 0 else None)
            for ci, c in enumerate(CONDS) for mi, m in enumerate(models)
        }
        contrasts[metric]["point_pooled"] = {
            c: (100 * float(s_num[ci].sum() / s_den[ci].sum()) if s_den[ci].sum() > 0 else None) for ci, c in enumerate(CONDS)
        }

    # ---- 주장 단위 정확 상한(Clopper–Pearson) + 클러스터 부트스트랩 일측 상한, Δ 함의 경계 ----
    exact = {}
    for metric in ("H", "U", "H_required", "H_other"):
        ncol, dcol = METRICS[metric]
        num, den = to_arrays(cell, qids, models, ncol, dcol)
        bs_num, bs_den = num[idx].sum(1), den[idx].sum(1)  # (B,2,2)
        exact[metric] = {"cells": {}, "conditions": {}}
        for ci, c in enumerate(CONDS):
            for mi, m in enumerate(models):
                k, n = int(num[:, ci, mi].sum()), int(den[:, ci, mi].sum())
                boots = ratio(bs_num[:, ci, mi], bs_den[:, ci, mi])
                exact[metric]["cells"][f"{c}|{m}"] = exact_bounds_block(k, n, boots)
            k, n = int(num[:, ci, :].sum()), int(den[:, ci, :].sum())
            boots = ratio(bs_num[:, ci, :].sum(-1), bs_den[:, ci, :].sum(-1))
            exact[metric]["conditions"][c] = exact_bounds_block(k, n, boots)
    delta_bound_all = {}
    for metric in ("H", "H_required", "H_other"):
      delta_bound = {}
      for kind in ("pooled", "small", "large"):
        if kind == "pooled":
            hd, hp = exact[metric]["conditions"]["direct"], exact[metric]["conditions"]["pivot"]
        else:
            m = small if kind == "small" else large
            hd, hp = exact[metric]["cells"][f"direct|{m}"], exact[metric]["cells"][f"pivot|{m}"]
        lo, hi = -hd["upper95_used_pct"], hp["upper95_used_pct"]
        lo_x, hi_x = -hd["upper95_exact_pct"], hp["upper95_exact_pct"]
        lo_c = -hd["upper95_cluster_pct"] if hd["upper95_cluster_pct"] is not None else None
        hi_c = hp["upper95_cluster_pct"] if hp["upper95_cluster_pct"] is not None else None
        cls_b = "bounded" if (abs(lo) < sesoi and abs(hi) < sesoi) else "undetected"
        d = contrasts[metric][kind]
        delta_bound[kind] = {
            "lower_pp": lo, "upper_pp": hi, "label": BOUND_LABEL,
            "basis": f"-upper95(H_direct:{hd['upper95_basis']}), +upper95(H_pivot:{hp['upper95_basis']})",
            "lower_exact_pp": lo_x, "upper_exact_pp": hi_x, "lower_cluster_pp": lo_c, "upper_cluster_pp": hi_c,
            "components": {"direct": {"upper95_cluster_pct": hd["upper95_cluster_pct"], "upper95_exact_pct": hd["upper95_exact_pct"], "upper95_used_pct": hd["upper95_used_pct"]},
                           "pivot": {"upper95_cluster_pct": hp["upper95_cluster_pct"], "upper95_exact_pct": hp["upper95_exact_pct"], "upper95_used_pct": hp["upper95_used_pct"]}},
            "percentile_ci_pp": [d["pct_ci_low_pp"], d["pct_ci_high_pp"]],
            "sesoi_class_bound": cls_b, "bootstrap_degenerate": d["bca_degenerate"],
            "sesoi_class_final": cls_b if d["bca_degenerate"] else d["sesoi_class"],
            "final_basis": "upper_bounds" if d["bca_degenerate"] else "bootstrap_ci",
        }
        d["delta_bound"] = delta_bound[kind]
        d["sesoi_class_final"] = delta_bound[kind]["sesoi_class_final"]
      delta_bound_all[metric] = delta_bound
    delta_bound = delta_bound_all["H"]

    # ---- 민감도: gate 통과 부분집합(ITT 대비) ----
    sensitivity = {"available": False, "note": "results/gate.csv not found"}
    if gate is not None and "gate_pass" in gate:
        gp = gate.copy()
        gp["gate_pass"] = gp["gate_pass"].astype(str).str.lower().isin(["true", "1", "1.0", "yes"])
        gp["rep"] = gp["rep"].astype(int)
        key = ["qid", "cond", "model", "rep"]
        pqs = pq.copy(); pqs["rep"] = pqs["rep"].astype(int)
        sub = pqs.merge(gp[key + ["gate_pass"]], on=key, how="inner")
        sub = sub[sub["gate_pass"]]
        agg = sub.groupby(["qid", "cond", "model"])[["n_mismatch_eff", "n_claims_eff"]].sum().reset_index()
        cnt = agg[agg["model"].isin(models)].groupby("qid").size()
        qids_s = sorted(cnt[cnt == 4].index)
        if len(qids_s) >= 2:
            num_s, den_s = to_arrays(agg, qids_s, models, "n_mismatch_eff", "n_claims_eff")
            idx_s = np.random.default_rng(seed).integers(0, len(qids_s), size=(B, len(qids_s)))
            sens_contrasts = {k: analyze_contrast(num_s, den_s, k, idx_s, sesoi, min_nonzero) for k in ("pooled", "small", "large")}
            sn, sd_ = num_s.sum(0), den_s.sum(0)
            sensitivity = {
                "available": True, "subset": "gate_pass == True (both conditions)",
                "n_responses": int(len(sub)), "n_responses_itt": int(len(pq)),
                "n_questions_complete": len(qids_s), "excluded_qids": sorted(set(pq["qid"]) - set(qids_s)),
                "point": {f"{c}|{m}": (100 * float(sn[ci, mi] / sd_[ci, mi]) if sd_[ci, mi] > 0 else None)
                          for ci, c in enumerate(CONDS) for mi, m in enumerate(models)},
                "point_pooled": {c: (100 * float(sn[ci].sum() / sd_[ci].sum()) if sd_[ci].sum() > 0 else None) for ci, c in enumerate(CONDS)},
                "contrasts": sens_contrasts,
            }
            for k, d in sens_contrasts.items():
                if k == "pooled":
                    kd, kp = (int(sn[0].sum()), int(sd_[0].sum())), (int(sn[1].sum()), int(sd_[1].sum()))
                else:
                    mi = 0 if k == "small" else 1
                    kd, kp = (int(sn[0, mi]), int(sd_[0, mi])), (int(sn[1, mi]), int(sd_[1, mi]))
                ud = exact_bounds_block(kd[0], kd[1], ratio(num_s[idx_s].sum(1)[:, 0, :].sum(-1) if k == "pooled" else num_s[idx_s].sum(1)[:, 0, mi],
                                                            den_s[idx_s].sum(1)[:, 0, :].sum(-1) if k == "pooled" else den_s[idx_s].sum(1)[:, 0, mi]))
                up = exact_bounds_block(kp[0], kp[1], ratio(num_s[idx_s].sum(1)[:, 1, :].sum(-1) if k == "pooled" else num_s[idx_s].sum(1)[:, 1, mi],
                                                            den_s[idx_s].sum(1)[:, 1, :].sum(-1) if k == "pooled" else den_s[idx_s].sum(1)[:, 1, mi]))
                lo_b, hi_b = -ud["upper95_used_pct"], up["upper95_used_pct"]
                cls_b = "bounded" if (abs(lo_b) < sesoi and abs(hi_b) < sesoi) else "undetected"
                d["delta_bound"] = {"lower_pp": lo_b, "upper_pp": hi_b, "label": BOUND_LABEL, "sesoi_class_bound": cls_b,
                                    "bootstrap_degenerate": d["bca_degenerate"],
                                    "sesoi_class_final": cls_b if d["bca_degenerate"] else d["sesoi_class"],
                                    "final_basis": "upper_bounds" if d["bca_degenerate"] else "bootstrap_ci"}
                d["sesoi_class_final"] = d["delta_bound"]["sesoi_class_final"]
        else:
            sensitivity = {"available": False, "note": f"too few complete questions in gate-pass subset ({len(qids_s)})"}

    # ---- 셀별 요약 (C, format_fail, gate_pass, en_leak, 토큰, 글자수, 반복 간 SD) ----
    cells_summary = {}
    for c in CONDS:
        for m in models:
            sub = pq[(pq["cond"] == c) & (pq["model"] == m)]
            gs = gate[(gate["cond"] == c) & (gate["model"] == m)] if gate is not None else None
            cells_summary[f"{c}|{m}"] = {
                "n_responses": int(len(sub)),
                "H_pct": contrasts["H"]["point"][f"{c}|{m}"], "U_pct": contrasts["U"]["point"][f"{c}|{m}"],
                "C_pct": contrasts["C"]["point"][f"{c}|{m}"],
                "H_cell_pct": contrasts["H_cell"]["point"][f"{c}|{m}"], "H_derived_pct": contrasts["H_derived"]["point"][f"{c}|{m}"],
                "H_required_pct": contrasts["H_required"]["point"][f"{c}|{m}"], "H_other_pct": contrasts["H_other"]["point"][f"{c}|{m}"],
                "claims_per_answer": float(sub["n_claims"].mean()) if len(sub) else None,
                "format_fail_rate": float(sub["format_fail"].mean()) if len(sub) else None,
                "zero_claims": int(sub["zero_claims"].sum()),
                "gate_pass_rate": (float(gs["gate_pass"].astype(float).mean()) if gs is not None and len(gs) else None),
                "en_leak_ratio_mean": (float(pd.to_numeric(gs["en_leak_ratio"], errors="coerce").mean())
                                       if gs is not None and len(gs) and "en_leak_ratio" in gs else None),
                "output_tokens_mean": float(pd.to_numeric(sub["output_tokens"], errors="coerce").mean()) if len(sub) else None,
                "thinking_tokens_mean": (float(pd.to_numeric(sub["thinking_tokens"], errors="coerce").mean())
                                         if len(sub) and "thinking_tokens" in sub else None),
                "narrative_chars_mean": float(sub["narrative_chars"].mean()) if len(sub) else None,
                "n_pool": int(sub["n_pool"].sum()) if "n_pool" in sub else None,
                "n_excluded_table_list": int(sub["n_excluded_table_list"].sum()) if "n_excluded_table_list" in sub else None,
            }

    # 반복 간 SD (응답 단위 H, 문항 내 rep 간 SD → 셀별 평균/중앙값)
    rep_sd = {}
    g = pq.groupby(["qid", "cond", "model"])["H"].std(ddof=1).reset_index()
    for (c, m), sub in g.groupby(["cond", "model"]):
        rep_sd[f"{c}|{m}"] = {"mean_sd": float(sub["H"].mean()), "median_sd": float(sub["H"].median()),
                              "n_questions": int(sub["H"].notna().sum())}
        if f"{c}|{m}" in cells_summary:
            cells_summary[f"{c}|{m}"]["H_rep_sd_mean"] = float(sub["H"].mean())

    # 표2
    rows = []
    for c in CONDS:
        for m in models:
            sub = pq[(pq["cond"] == c) & (pq["model"] == m)]
            gp = None
            if gate is not None:
                gs = gate[(gate["cond"] == c) & (gate["model"] == m)]
                gp = 100 * float(gs["gate_pass"].astype(float).mean()) if len(gs) else None
            kind = "small" if m == small else "large"
            d = contrasts["H"][kind]
            ex = exact["H"]["cells"][f"{c}|{m}"]
            rows.append({
                "Strategy": "Korean-direct" if c == "direct" else "English-pivot",
                "Model": m + (" (small)" if m == small else " (large)"),
                "H%": round(contrasts["H"]["point"][f"{c}|{m}"], 2),
                "U%": round(contrasts["U"]["point"][f"{c}|{m}"], 2),
                "C%": round(contrasts["C"]["point"][f"{c}|{m}"], 2) if contrasts["C"]["point"][f"{c}|{m}"] is not None else None,
                "H_required%": _r2(contrasts["H_required"]["point"][f"{c}|{m}"]),
                "H_other%": _r2(contrasts["H_other"]["point"][f"{c}|{m}"]),
                "claims/answer": round(float(sub["n_claims"].mean()), 2),
                "N_claims": ex["N_claims"], "N_mismatch": ex["N_mismatch"], "H_upper95": round(ex["upper95_used_pct"], 2),
                "ΔH[CI]": "ref." if c == "direct" else _delta_text(d),
                "ΔH_basis": "" if c == "direct" else _delta_basis(d),
                "ΔH_percentile_CI": "" if c == "direct" else f"[{d['pct_ci_low_pp']:+.2f}, {d['pct_ci_high_pp']:+.2f}]",
                "tokens/answer": round(float(pd.to_numeric(sub["output_tokens"], errors="coerce").mean()), 1),
                "narrative_chars": round(float(sub["narrative_chars"].mean()), 1),
                "gate_pass%": round(gp, 1) if gp is not None else None,
                "n_responses": int(len(sub)),
                "format_fail": int(sub["format_fail"].sum()),
                "zero_claims": int(sub["zero_claims"].sum()),
            })
    for c in CONDS:
        sub = pq[pq["cond"] == c]
        d = contrasts["H"]["pooled"]
        ex = exact["H"]["conditions"][c]
        rows.append({
            "Strategy": "Korean-direct" if c == "direct" else "English-pivot", "Model": "pooled",
            "H%": round(contrasts["H"]["point_pooled"][c], 2), "U%": round(contrasts["U"]["point_pooled"][c], 2),
            "C%": round(contrasts["C"]["point_pooled"][c], 2) if contrasts["C"]["point_pooled"][c] is not None else None,
            "H_required%": _r2(contrasts["H_required"]["point_pooled"][c]),
            "H_other%": _r2(contrasts["H_other"]["point_pooled"][c]),
            "claims/answer": round(float(sub["n_claims"].mean()), 2),
            "N_claims": ex["N_claims"], "N_mismatch": ex["N_mismatch"], "H_upper95": round(ex["upper95_used_pct"], 2),
            "ΔH[CI]": "ref." if c == "direct" else _delta_text(d),
            "ΔH_basis": "" if c == "direct" else _delta_basis(d),
            "ΔH_percentile_CI": "" if c == "direct" else f"[{d['pct_ci_low_pp']:+.2f}, {d['pct_ci_high_pp']:+.2f}]",
            "tokens/answer": round(float(pd.to_numeric(sub["output_tokens"], errors="coerce").mean()), 1),
            "narrative_chars": round(float(sub["narrative_chars"].mean()), 1),
            "gate_pass%": (round(100 * float(gate[gate["cond"] == c]["gate_pass"].astype(float).mean()), 1)
                           if gate is not None and len(gate[gate["cond"] == c]) else None),
            "n_responses": int(len(sub)), "format_fail": int(sub["format_fail"].sum()),
            "zero_claims": int(sub["zero_claims"].sum()),
        })
    table2 = pd.DataFrame(rows)
    os.makedirs(indir, exist_ok=True)
    table2.to_csv(os.path.join(indir, "table2.csv"), index=False, encoding="utf-8-sig")

    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bootstrap": {"B": B, "seed": seed, "unit": "question", "statistic": "ratio-of-sums", "ci": "BCa (percentile fallback)",
                      "min_nonzero_questions_for_bca": min_nonzero},
        "sesoi_pp": sesoi, "mde_formula": "2.80 * SD(delta_q) / sqrt(Q)",
        "models": {"small": small, "large": large, "tier_assignment_fallback": tier_flag},
        "n_questions_complete": Q, "incomplete_qids_excluded": incomplete,
        "n_responses": int(len(pq)), "n_format_fail": int(pq["format_fail"].sum()), "n_zero_claims": int(pq["zero_claims"].sum()),
        "primary": {"metric": "H", "contrast": "pooled", **contrasts["H"]["pooled"]},
        "contrasts": contrasts,
        "h_upper95_exact": exact["H"], "u_upper95_exact": exact["U"],
        "exact_bound_note": "Clopper-Pearson two-sided 95% (claims as unit; zero-claim responses count as 1 claim/1 mismatch). "
                            "cluster_upper95 = 95th percentile of question-cluster bootstrap of ratio-of-sums when non-degenerate.",
        "delta_bound": delta_bound,
        "delta_bound_H_required": delta_bound_all["H_required"],
        "delta_bound_H_other": delta_bound_all["H_other"],
        "h_required_upper95_exact": exact["H_required"], "h_other_upper95_exact": exact["H_other"],
        "sensitivity_gate_pass": sensitivity,
        "cells": cells_summary,
        "rep_sd_H": rep_sd,
        "code_counts": {k: int(pq[k].sum()) for k in ("n_K1", "n_K2", "n_K3", "n_K4", "n_K6") if k in pq},
    }

    def _clean(o):
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_clean(v) for v in o]
        if isinstance(o, (np.floating, float)):
            return None if (o is None or not np.isfinite(o)) else float(o)
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, np.bool_):
            return bool(o)
        return o

    with open(os.path.join(indir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(_clean(summary), f, ensure_ascii=False, indent=2)

    fig_rows = [
        {"label": "Pooled (primary)", **contrasts["H"]["pooled"]},
        {"label": f"Small ({small})", **contrasts["H"]["small"]},
        {"label": f"Large ({large})", **contrasts["H"]["large"]},
    ]
    forest_plot(fig_rows, os.path.join(figdir, "fig2_forest.png"), sesoi)

    print(f"[analyze] Q={Q} (excluded incomplete: {incomplete}) B={B} seed={seed}")
    for kind in ("pooled", "small", "large", "interaction"):
        d = contrasts["H"][kind]
        extra = ""
        if kind in delta_bound:
            b = delta_bound[kind]
            extra = (f" | bounds [{b['lower_pp']:+.2f}, {b['upper_pp']:+.2f}] (max of cluster/exact) | percentile CI "
                     f"[{d['pct_ci_low_pp']:+.2f}, {d['pct_ci_high_pp']:+.2f}] → final {b['sesoi_class_final']} ({b['final_basis']})")
        print(f"  ΔH {kind:12s} {d['estimate_pp']:+.3f} pp  CI [{d['ci_low_pp']:+.3f}, {d['ci_high_pp']:+.3f}] "
              f"({d['ci_method']}, nonzero={d['n_nonzero_questions']}, MDE={d['mde_pp']:.2f}) → {d['sesoi_class']}{extra}")
    for kind in ("pooled", "small", "large"):
        d = contrasts["H_required"][kind]; b = d["delta_bound"]
        print(f"  ΔH_required {kind:8s} {d['estimate_pp']:+.3f} pp  CI [{d['ci_low_pp']:+.3f}, {d['ci_high_pp']:+.3f}] ({d['ci_method']}, nonzero={d['n_nonzero_questions']})"
              f" | bounds [{b['lower_pp']:+.2f}, {b['upper_pp']:+.2f}] | percentile CI [{d['pct_ci_low_pp']:+.2f}, {d['pct_ci_high_pp']:+.2f}] → final {d['sesoi_class_final']}")
    for metric in ("H_required", "H_other"):
        for key, ex in exact[metric]["conditions"].items():
            print(f"  {metric} {key:7s} {ex['N_mismatch']}/{ex['N_claims']} = {ex['estimate_pct']:.2f}%  used upper95 {ex['upper95_used_pct']:.2f}%")
    if sensitivity.get("available"):
        print(f"[sensitivity gate-pass] responses={sensitivity['n_responses']}/{sensitivity['n_responses_itt']} "
              f"Q={sensitivity['n_questions_complete']} excluded={sensitivity['excluded_qids']}")
        for k, d in sensitivity["contrasts"].items():
            b = d["delta_bound"]
            print(f"  ΔH {k:8s} {d['estimate_pp']:+.3f} pp  CI [{d['ci_low_pp']:+.3f}, {d['ci_high_pp']:+.3f}] ({d['ci_method']}, nonzero={d['n_nonzero_questions']})"
                  f" | bounds [{b['lower_pp']:+.2f}, {b['upper_pp']:+.2f}] | percentile CI [{d['pct_ci_low_pp']:+.2f}, {d['pct_ci_high_pp']:+.2f}]"
                  f" → final {d['sesoi_class_final']} ({b['final_basis']})")
    else:
        print(f"[sensitivity gate-pass] unavailable: {sensitivity.get('note')}")
    for key, ex in exact["H"]["conditions"].items():
        print(f"  H {key:7s} {ex['N_mismatch']}/{ex['N_claims']} = {ex['estimate_pct']:.2f}%  exact upper95 {ex['upper95_exact_pct']:.2f}%  "
              f"cluster upper95 {ex['upper95_cluster_pct'] if ex['upper95_cluster_pct'] is None else round(ex['upper95_cluster_pct'], 2)}  "
              f"used {ex['upper95_used_pct']:.2f}%")
    print(table2.to_string(index=False))
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--indir", default=os.path.join(HERE, "results"))
    ap.add_argument("--figdir", default=os.path.join(HERE, "figures"))
    ap.add_argument("--B", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--sesoi", type=float, default=2.0)
    a = ap.parse_args(argv)
    run(a.indir, a.figdir, a.B, a.seed, a.sesoi)


if __name__ == "__main__":
    main()
