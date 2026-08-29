"""Tests for the incident analysis script.

The script's output drives what the agent claims about an incident, so the
thing that matters most is not that it finds a signal -- it is that it does
NOT find one when there is nothing there. A tool that always reports a
correlation would let the agent confabulate a root cause for any outage, and
that is worse than no analysis at all.

Synthetic data throughout, with the answer planted, so a wrong result is
unambiguous rather than a judgement call.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "analyze_incident.py"

CHANGE_AT = 400
BASE = 1_788_000_000.0


def write_csv(path: Path, rows: list[dict]) -> Path:
    columns = list(rows[0])
    lines = [",".join(columns)]
    lines += [",".join(str(r[c]) for c in columns) for r in rows]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run(csv_path: Path, tmp_path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(csv_path), "--out-dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"script failed:\n{result.stderr}"
    return json.loads(result.stdout)


@pytest.fixture()
def rng():
    return np.random.default_rng(20260829)


def make_rows(rng, *, n=900, failure_after=0.30, discriminating=True):
    """Healthy stretch, then a partial outage.

    When `discriminating`, failures happen only to requests with a high
    `payload_size`. Otherwise failures are spread at random, and the script
    is expected to find nothing.

    `payload_size` spans a wide range on purpose, matching what a real
    per-request attribute looks like, so this exercises the numeric branch.
    Low-cardinality attributes take the categorical branch and are covered
    separately.
    """
    rows = []
    for i in range(n):
        broken = i >= CHANGE_AT
        payload = int(rng.integers(18, 29) if rng.random() < 0.3 else rng.integers(2, 8))
        if not broken:
            failed = False
        elif discriminating:
            failed = payload >= 15 and rng.random() < 0.85
        else:
            failed = rng.random() < failure_after
        rows.append(
            {
                "epoch": f"{BASE + i:.3f}",
                "route": "/checkout",
                "status": 503 if failed else 200,
                "duration_ms": round(float(rng.normal(620, 30) if failed else rng.normal(45, 12)), 1),
                "version": "v1.5.0" if broken else "v1.4.2",
                "payload_size": payload,
            }
        )
    return rows


def test_finds_the_planted_change_point(tmp_path, rng):
    report = run(write_csv(tmp_path / "s.csv", make_rows(rng)), tmp_path)
    change = report["change_point"]

    assert change["found"] is True
    assert change["p_value"] < 1e-10
    # Within 5% of the planted index; exact placement depends on where the
    # first failures happen to land.
    assert abs(change["epoch"] - (BASE + CHANGE_AT)) < 0.05 * 900
    assert change["before"]["error_rate"] == 0.0
    assert change["after"]["error_rate"] > 0.1
    assert change["after"]["p99_ms"] > change["before"]["p99_ms"] * 3


def test_discovers_the_planted_discriminator(tmp_path, rng):
    report = run(write_csv(tmp_path / "s.csv", make_rows(rng)), tmp_path)

    assert report["discriminators"], "should have found payload_size"
    top = report["discriminators"][0]
    assert top["attribute"] == "payload_size"
    assert top["direction"] == "higher"
    assert top["effect_size"] > 0.8
    assert top["p_value"] < 1e-10
    assert top["median_when_failed"] > top["median_when_ok"] * 2
    assert "payload_size" in report["interpretation"]


def test_discovers_a_categorical_discriminator(tmp_path, rng):
    """A low-cardinality attribute takes the chi-square branch, not the rank test."""
    rows = []
    for i in range(900):
        broken = i >= CHANGE_AT
        region = str(rng.choice(["eu-west", "us-east", "ap-south"]))
        failed = broken and region == "eu-west" and rng.random() < 0.8
        rows.append(
            {
                "epoch": f"{BASE + i:.3f}",
                "route": "/checkout",
                "status": 503 if failed else 200,
                "duration_ms": round(float(rng.normal(600, 30) if failed else rng.normal(45, 12)), 1),
                "region": region,
            }
        )
    report = run(write_csv(tmp_path / "s.csv", rows), tmp_path)

    top = report["discriminators"][0]
    assert top["attribute"] == "region"
    assert top["kind"] == "categorical"
    assert top["test"] == "chi-square"
    worst = max(top["breakdown"], key=lambda b: b["error_rate"])
    assert worst["value"] == "eu-west"
    assert worst["error_rate"] > 0.5
    for other in top["breakdown"]:
        if other["value"] != "eu-west":
            assert other["error_rate"] == 0.0


def test_reports_no_discriminator_when_failures_are_uniform(tmp_path, rng):
    """The most important test here: no signal must mean no claim."""
    rows = make_rows(rng, discriminating=False)
    report = run(write_csv(tmp_path / "s.csv", rows), tmp_path)

    assert report["change_point"]["found"] is True, "the outage itself is still real"
    assert report["discriminators"] == [], (
        "failures were random, so no attribute should be reported as"
        f" discriminating; got {report['discriminators']}"
    )
    assert "uniformly distributed" in report["interpretation"]


def test_reports_no_change_point_when_nothing_changes(tmp_path, rng):
    rows = [
        {
            "epoch": f"{BASE + i:.3f}",
            "route": "/checkout",
            "status": 200,
            "duration_ms": round(float(rng.normal(45, 12)), 1),
            "version": "v1.4.2",
            "payload_size": 5,
        }
        for i in range(600)
    ]
    report = run(write_csv(tmp_path / "s.csv", rows), tmp_path)
    assert report["change_point"]["found"] is False


def test_blast_radius_counts_only_the_affected_window(tmp_path, rng):
    report = run(write_csv(tmp_path / "s.csv", make_rows(rng)), tmp_path)
    blast = report["blast_radius"]

    assert blast["failed_requests"] > 0
    assert blast["requests_in_window"] < report["requests_analysed"], (
        "blast radius must measure the post-change-point window, not the"
        " whole sample, or it understates the failure share"
    )
    assert 0 < blast["share_of_traffic_failing"] < 1
    assert blast["routes_affected"] == ["/checkout"]


def test_writes_a_chart(tmp_path, rng):
    report = run(write_csv(tmp_path / "s.csv", make_rows(rng)), tmp_path)
    chart = Path(report["chart"])
    assert chart.exists() and chart.stat().st_size > 10_000


def test_rejects_a_csv_without_the_required_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "--out-dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "epoch" in result.stderr
