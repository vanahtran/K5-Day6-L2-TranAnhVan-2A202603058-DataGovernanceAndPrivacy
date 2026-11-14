from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import signal

from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.manifest import verify_manifest
from .common.review import apply_overrides, load_review_decisions, make_review_queue, write_review_queue
from .metrics.tracks import get_track


def load(ctx: RunContext) -> dict:
    if ctx.track != "t04":
        raise NotImplementedError(f"Track {ctx.track} is not implemented in this pipeline.")

    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"Provenance check failed: {problems[:3]}")

    data = {"manifest": pd.read_csv(manifest) if manifest.exists() else None}
    data["camera"] = pd.read_csv(ctx.input_dir / "camera.csv")
    data["imu"] = pd.read_csv(ctx.input_dir / "imu.csv")
    seg_file = ctx.input_dir / "segments.csv"
    data["segments"] = pd.read_csv(seg_file) if seg_file.exists() else None
    return data


def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    cam = data["camera"].copy()
    imu = data["imu"].copy()

    tol_ms = float(ctx.param("tol_ms", 50.0))
    cam_t_ms = cam["t_cam_s"].values * 1000.0
    imu_ts = imu["t_imu_ms"].values.astype(float)
    imu_az = np.abs(imu["az"].values - 9.81)

    t0 = imu_ts.min()
    cam_rel = cam_t_ms - t0
    imu_rel = imu_ts - t0

    cam_adj = np.zeros(len(cam), dtype=float)
    segment_duration_ms = 20000.0
    n_segments = int(np.ceil((imu_rel.max() - imu_rel.min()) / segment_duration_ms)) or 1

    offsets = {}
    for s in range(n_segments):
        t_start = s * segment_duration_ms
        t_end = (s + 1) * segment_duration_ms

        c_mask = (cam_rel >= t_start) & (cam_rel < t_end)
        i_mask = (imu_rel >= t_start) & (imu_rel < t_end)

        if not np.any(c_mask) or not np.any(i_mask):
            continue

        grid = np.arange(t_start, t_end, 10.0)
        c_interp = np.interp(grid, cam_rel[c_mask], cam["motion_score"].values[c_mask], left=0, right=0)
        i_signal = np.interp(grid, imu_rel[i_mask], imu_az[i_mask], left=0, right=0)

        c_std = c_interp.std()
        i_std = i_signal.std()
        c_norm = (c_interp - c_interp.mean()) / (c_std if c_std > 1e-6 else 1.0)
        i_norm = (i_signal - i_signal.mean()) / (i_std if i_std > 1e-6 else 1.0)

        corr = signal.correlate(i_norm, c_norm, mode="full")
        lags = signal.correlation_lags(len(i_norm), len(c_norm), mode="full") * 10.0

        mask = (lags >= -500) & (lags <= 500)
        if np.any(mask):
            best_lag = float(lags[mask][np.argmax(corr[mask])])
        else:
            best_lag = 0.0

        offsets[s] = best_lag
        cam_adj[c_mask] = cam_t_ms[c_mask] + best_lag

    ctx.stats["estimated_offsets_per_segment"] = offsets

    idx = np.searchsorted(imu_ts, cam_adj)
    idx = np.clip(idx, 0, len(imu_ts) - 1)
    prev_idx = np.clip(idx - 1, 0, len(imu_ts) - 1)

    diff1 = np.abs(imu_ts[idx] - cam_adj)
    diff2 = np.abs(imu_ts[prev_idx] - cam_adj)
    chosen = np.where(diff1 < diff2, idx, prev_idx)
    min_diff = np.minimum(diff1, diff2)

    is_held = min_diff > tol_ms
    paired_imu = np.where(is_held, np.nan, imu_ts[chosen])
    reasons = np.where(is_held, "imu_dropout_or_gap", "synced")
    scores = np.where(is_held, 1000.0 + min_diff, min_diff)

    out = pd.DataFrame({
        "frame_id": cam["frame_id"],
        "imu_t_ms": paired_imu,
        "score": scores,
        "reason": reasons,
        "residual_ms": min_diff,
    })
    return out


def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    budget = int(ctx.param("budget", 20))
    q = make_review_queue(
        scored,
        id_col="frame_id",
        score_col="score",
        budget=budget,
        reason_col="reason",
        action_col=None,
    )
    write_review_queue(q, ctx.out("review_queue.csv"))
    return q


def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    reviewed_path = ctx.output_dir / "review_queue_reviewed.csv"
    if reviewed_path.exists():
        reviewed = load_review_decisions(reviewed_path)
        ctx.stats["n_reviewed"] = len(reviewed)
        scored = apply_overrides(scored, reviewed, id_col="frame_id", value_col="imu_t_ms", audit=ctx.audit)
    return scored


def decide(ctx: RunContext, final: pd.DataFrame) -> Decision:
    n_total = len(final)
    n_held = int(final["imu_t_ms"].isna().sum())
    n_synced = n_total - n_held

    sync_rate = n_synced / n_total if n_total else 0.0
    ctx.stats["sync_rate"] = round(sync_rate, 4)
    ctx.stats["n_synced"] = n_synced
    ctx.stats["n_held"] = n_held

    owner = ctx.config.get("owner", "Tran Anh Van - Sync Lead")

    if sync_rate >= 0.95:
        return Decision(
            status="GO",
            reason_code="SYNC_PASS",
            reason=f"Successfully synchronized {n_synced}/{n_total} frames ({sync_rate:.1%}). Held {n_held} dropout frames.",
            owner=owner,
            scope=ctx.track,
            evidence=[str(ctx.out("pairs.csv")), str(ctx.out("review_queue.csv"))],
        )
    else:
        return Decision(
            status="HOLD",
            reason_code="HIGH_SENSOR_DROPOUT",
            reason=f"Sync rate {sync_rate:.1%} below 95% threshold ({n_held} frames held).",
            owner=owner,
            scope=ctx.track,
            evidence=[str(ctx.out("pairs.csv"))],
        )


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    data = load(ctx)
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue)
    final[spec.pred_columns + [c for c in final.columns if c not in spec.pred_columns]].to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(decide(ctx, final), ctx.out("decision.json"), ctx.out("decision_log.csv"))
