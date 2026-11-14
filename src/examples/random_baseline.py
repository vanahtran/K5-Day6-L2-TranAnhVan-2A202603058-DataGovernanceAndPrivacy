"""Wiring example = the *random* baseline required by T01 / T02 / T11 / T13.

Shows how load -> process -> rank -> review -> decide -> outputs connect.
It is intentionally dumb (seeded random) — it is the bar your PoC must beat,
evaluated on the same pool, same budget, same seed.

    python -m src.main --track t11 --pipeline src.examples.random_baseline --output out/baseline --run-name baseline --offline
    python -m src.evaluate --track t11 --pred out/baseline/ranking.csv --run-name baseline --out reports/metrics_baseline.json
"""
from __future__ import annotations

import pandas as pd

from ..common.contact_sheet import render_contact_sheet
from ..common.context import RunContext
from ..common.decision import Decision, write_decision
from ..common.review import make_review_queue, write_review_queue
from ..metrics.tracks import get_track


def _items(ctx: RunContext) -> tuple[pd.DataFrame, str]:
    d = ctx.input_dir
    if ctx.track == "t01":
        return pd.read_csv(d / "candidates.csv"), "clip_id"
    if ctx.track == "t02":
        return pd.read_csv(d / "images.csv"), "image_id"
    if ctx.track == "t11":
        ann = pd.read_csv(d / "annotations.csv")
        imgs = pd.read_csv(d / "images.csv")
        items = pd.concat([ann[["ann_id", "image_id"]].rename(columns={"ann_id": "item_id"}),
                           imgs[["image_id"]].assign(item_id=lambda x: "img:" + x.image_id)], ignore_index=True)
        return items.merge(imgs[["image_id", "path"]], on="image_id"), "item_id"
    if ctx.track == "t13":
        return pd.read_csv(d / "images.csv"), "image_id"
    if ctx.track == "t04":
        import numpy as np
        cam = pd.read_csv(d / "camera.csv")
        imu = pd.read_csv(d / "imu.csv")
        cam_t_ms = cam["t_cam_s"].values * 1000.0
        imu_ts = imu["t_imu_ms"].values.astype(float)
        idx = np.searchsorted(imu_ts, cam_t_ms)
        idx = np.clip(idx, 0, len(imu_ts) - 1)
        prev_idx = np.clip(idx - 1, 0, len(imu_ts) - 1)
        diff1 = np.abs(imu_ts[idx] - cam_t_ms)
        diff2 = np.abs(imu_ts[prev_idx] - cam_t_ms)
        chosen = np.where(diff1 < diff2, idx, prev_idx)
        min_diff = np.minimum(diff1, diff2)
        tol_ms = float(ctx.param("tol_ms", 50.0))
        paired_imu = np.where(min_diff > tol_ms, np.nan, imu_ts[chosen])
        items = pd.DataFrame({"frame_id": cam["frame_id"], "imu_t_ms": paired_imu, "score": min_diff, "reason": "nearest_raw"})
        return items, "frame_id"
    raise NotImplementedError(f"random_baseline example supports t01, t02, t04, t11, t13 (not {ctx.track})")


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    items, id_col = _items(ctx)                                        # 01 load
    items = items.assign(score=ctx.rng.random(len(items)), reason="random")  # 02 process (random!)
    ctx.stats["n_items"] = len(items)
    budget = int(ctx.param("budget", ctx.param("k", 20)))
    evidence = "path" if "path" in items.columns else None
    q = make_review_queue(items, id_col, "score", budget, reason_col="reason", evidence_col=evidence)  # 03 rank
    write_review_queue(q, ctx.out("review_queue.csv"))
    if evidence:                                                        # 04 review (HTML contact sheet)
        render_contact_sheet(q, ctx.out("contact_sheet.html"), image_root=ctx.input_dir, title=f"{spec.track} random baseline queue")
    ranked = items.sort_values("score", ascending=False)
    if ctx.track in ("t01", "t13"):
        k = int(ctx.param("k", 30 if ctx.track == "t01" else 20))
        out = ranked.head(k)[[id_col]].assign(rank=range(1, k + 1))
    elif ctx.track == "t02":
        out = ranked[["image_id", "score"]]
    elif ctx.track == "t04":
        out = ranked[["frame_id", "imu_t_ms"]]
    else:
        out = ranked[["item_id", "score"]]
    out.to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(Decision(status="HOLD", reason_code="BASELINE_ONLY", reason="Random baseline: never release on this",
                            owner=ctx.config.get("owner", "baseline"), scope=ctx.track, evidence=[str(ctx.out(spec.pred_file))]),
                   ctx.out("decision.json"), ctx.out("decision_log.csv"))
