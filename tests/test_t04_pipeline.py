"""Group unit tests for T04 pipeline.

Contract requirement from README.md:
    pytest -q : >=2 test của nhóm (success + failure/edge), 0 failed
"""
import pandas as pd
import pytest

from src import main as runner


def test_t04_pipeline_success(tmp_path):
    """Test 1 (Success): Pipeline runs end-to-end and outputs valid pairs.csv with >=95% sync rate."""
    rc = runner.main(["--track", "t04", "--output", str(tmp_path), "--offline", "--seed", "42"])
    assert rc == 0
    pairs_file = tmp_path / "pairs.csv"
    assert pairs_file.exists()
    df = pd.read_csv(pairs_file)
    assert "frame_id" in df.columns and "imu_t_ms" in df.columns
    assert len(df) == 3570
    valid_sync = df["imu_t_ms"].notna().sum()
    sync_rate = valid_sync / len(df)
    assert sync_rate >= 0.95


def test_t04_pipeline_dropout_edge_case(tmp_path):
    """Test 2 (Failure/Edge case): Pipeline correctly detects IMU dropout and puts gap frames on HOLD."""
    rc = runner.main(["--track", "t04", "--output", str(tmp_path), "--offline", "--seed", "42"])
    assert rc == 0
    df = pd.read_csv(tmp_path / "pairs.csv")
    gap_frames = df.iloc[1352:1394]
    assert gap_frames["imu_t_ms"].isna().all(), "Dropout gap frames must be put on HOLD (NaN)"
    assert df["imu_t_ms"].isna().sum() == 46, "Exactly 46 frames must be held"
