"""Quantify an incident from a raw request sample.

Runs in the sandbox on the CSV that `get_request_sample` returns. Answers
three questions that should never be answered by eye:

  1. WHEN did behaviour actually change?  Likelihood-ratio change point over
     the binary failure series, rather than picking the first bucket that
     looks bad.

  2. WHAT do the failures have in common?  Every per-request attribute is
     tested for whether failures cluster on it. The script does not know in
     advance which attribute matters -- it finds the strongest discriminator
     and reports the effect size and p-value, or reports that there is none.

  3. HOW BAD is it?  Requests affected, share of traffic, duration, and the
     latency shift.

Deliberately ignorant of this particular incident. Nothing here knows about
carts, inventory, or N+1 queries. Point it at a different outage and it will
find whatever structure is actually present -- or correctly report that the
failures are uniform, which is itself a finding.

    python analyze_incident.py sample.csv [--out-dir .] [--deploy-epoch 1787997840]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

REQUIRED = ["pandas", "numpy", "scipy", "matplotlib"]


def _ensure_deps() -> None:
    missing = []
    for pkg in REQUIRED:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"installing {missing} ...", file=sys.stderr)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", *missing], check=True
        )


_ensure_deps()

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

# Columns that describe the request itself rather than an attribute of it.
STRUCTURAL = {"epoch", "status", "duration_ms", "route"}

# Below this, a "numeric" column is really a category (a version number, a
# replica count), and a rank test on it would be misleading.
CATEGORICAL_MAX_CARDINALITY = 12

SIGNIFICANCE = 0.01


def load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "status" not in df or "epoch" not in df:
        raise SystemExit("CSV must contain at least 'epoch' and 'status' columns")
    df = df.sort_values("epoch").reset_index(drop=True)
    df["failed"] = df["status"] >= 500
    df["dt"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
    return df


# ------------------------------------------------------------- change point


def _bernoulli_loglik(k: int, n: int) -> float:
    """Log-likelihood of k successes in n trials at the MLE rate."""
    if n == 0:
        return 0.0
    p = k / n
    if p in (0.0, 1.0):
        return 0.0
    return k * np.log(p) + (n - k) * np.log1p(-p)


def find_change_point(df: pd.DataFrame, min_side: int = 30) -> dict:
    """Locate the split maximising the likelihood of two different failure rates.

    A single Bernoulli rate over the whole window is the null. For every
    candidate split we score two independent rates instead, and take the best.
    The likelihood ratio against the null gives a significance for the shift,
    so "nothing actually changed" is a reportable answer.
    """
    failed = df["failed"].to_numpy().astype(int)
    n = len(failed)
    if n < 2 * min_side:
        return {"found": False, "reason": f"need at least {2 * min_side} requests, got {n}"}

    total_failures = int(failed.sum())
    null_ll = _bernoulli_loglik(total_failures, n)

    prefix = np.concatenate([[0], np.cumsum(failed)])
    best_idx, best_ll = -1, -np.inf
    for i in range(min_side, n - min_side):
        left_ll = _bernoulli_loglik(int(prefix[i]), i)
        right_ll = _bernoulli_loglik(int(prefix[n] - prefix[i]), n - i)
        if left_ll + right_ll > best_ll:
            best_ll, best_idx = left_ll + right_ll, i

    # 2 * log-likelihood ratio is chi-square with 1 df under the null.
    statistic = 2 * (best_ll - null_ll)
    p_value = float(stats.chi2.sf(statistic, df=1))

    before, after = df.iloc[:best_idx], df.iloc[best_idx:]
    return {
        "found": p_value < SIGNIFICANCE,
        "epoch": float(df["epoch"].iloc[best_idx]),
        "timestamp": df["dt"].iloc[best_idx].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "p_value": p_value,
        "before": {
            "requests": int(len(before)),
            "error_rate": round(float(before["failed"].mean()), 4),
            "p99_ms": round(float(before["duration_ms"].quantile(0.99)), 1),
            "median_ms": round(float(before["duration_ms"].median()), 1),
        },
        "after": {
            "requests": int(len(after)),
            "error_rate": round(float(after["failed"].mean()), 4),
            "p99_ms": round(float(after["duration_ms"].quantile(0.99)), 1),
            "median_ms": round(float(after["duration_ms"].median()), 1),
        },
    }


# ------------------------------------------------------ correlation discovery


def _test_attribute(df: pd.DataFrame, column: str) -> dict | None:
    """Does this attribute separate failures from successes?"""
    series = df[column].dropna()
    if series.nunique() < 2 or len(series) < 20:
        return None

    sub = df[[column, "failed"]].dropna()
    numeric = pd.api.types.is_numeric_dtype(sub[column])
    treat_as_category = (not numeric) or sub[column].nunique() <= CATEGORICAL_MAX_CARDINALITY

    if treat_as_category:
        table = pd.crosstab(sub[column], sub["failed"])
        if table.shape[0] < 2 or table.shape[1] < 2:
            return None
        chi2, p, _, _ = stats.chi2_contingency(table)
        n = int(table.to_numpy().sum())
        # Cramer's V: 0 = no association, 1 = perfect.
        v = float(np.sqrt(chi2 / (n * (min(table.shape) - 1))))
        rates = sub.groupby(column)["failed"].agg(["mean", "size"])
        return {
            "attribute": column,
            "kind": "categorical",
            "test": "chi-square",
            "p_value": float(p),
            "effect_size": round(v, 3),
            "effect_metric": "cramers_v",
            "breakdown": [
                {
                    "value": str(idx),
                    "requests": int(row["size"]),
                    "error_rate": round(float(row["mean"]), 4),
                }
                for idx, row in rates.iterrows()
            ],
        }

    failed_vals = sub.loc[sub["failed"], column]
    ok_vals = sub.loc[~sub["failed"], column]
    if len(failed_vals) < 10 or len(ok_vals) < 10:
        return None

    u, p = stats.mannwhitneyu(failed_vals, ok_vals, alternative="two-sided")
    # Rank-biserial correlation: signed, -1..1, independent of sample size.
    rank_biserial = float(2 * u / (len(failed_vals) * len(ok_vals)) - 1)
    return {
        "attribute": column,
        "kind": "numeric",
        "test": "mann-whitney-u",
        "p_value": float(p),
        "effect_size": round(abs(rank_biserial), 3),
        "effect_metric": "rank_biserial",
        "median_when_failed": round(float(failed_vals.median()), 2),
        "median_when_ok": round(float(ok_vals.median()), 2),
        "direction": "higher" if rank_biserial > 0 else "lower",
    }


def discriminators(df: pd.DataFrame) -> list[dict]:
    """Rank every attribute by how strongly it separates failures."""
    if df["failed"].nunique() < 2:
        return []

    results = []
    for column in df.columns:
        if column in STRUCTURAL or column in {"failed", "dt"}:
            continue
        try:
            found = _test_attribute(df, column)
        except (ValueError, ZeroDivisionError):
            continue
        if found and found["p_value"] < SIGNIFICANCE:
            results.append(found)

    return sorted(results, key=lambda r: (-r["effect_size"], r["p_value"]))


def _failing_segment(df: pd.DataFrame, top: dict) -> pd.DataFrame | None:
    """Narrow to the slice of traffic the top discriminator implicates.

    A near-perfect categorical split usually means the obvious thing (all
    failures are on one version). The interesting question is what separates
    failures *within* that slice, so this hands back the affected subset for
    a second pass.
    """
    if top["kind"] != "categorical":
        return None
    worst = max(top["breakdown"], key=lambda b: b["error_rate"])
    if worst["error_rate"] < 0.05:
        return None
    segment = df[df[top["attribute"]].astype(str) == worst["value"]]
    return segment if len(segment) >= 40 else None


# ------------------------------------------------------------- blast radius


def blast_radius(df: pd.DataFrame, change: dict) -> dict:
    affected = df[df["epoch"] >= change["epoch"]] if change.get("found") else df
    failures = affected[affected["failed"]]
    if affected.empty:
        return {"failed_requests": 0}

    duration_s = float(affected["epoch"].max() - affected["epoch"].min())
    return {
        "failed_requests": int(len(failures)),
        "requests_in_window": int(len(affected)),
        "share_of_traffic_failing": round(float(len(failures) / len(affected)), 4),
        "window_seconds": round(duration_s, 1),
        "failures_per_minute": round(len(failures) / (duration_s / 60), 1)
        if duration_s > 0
        else 0.0,
        "latency_p99_ms": round(float(affected["duration_ms"].quantile(0.99)), 1),
        "routes_affected": sorted(failures["route"].unique().tolist())
        if "route" in failures
        else [],
    }


# -------------------------------------------------------------------- chart


def render_chart(df: pd.DataFrame, change: dict, top: dict | None, out_path: str,
                 deploy_epoch: float | None) -> None:
    panels = 3 if top else 2
    fig, axes = plt.subplots(panels, 1, figsize=(11, 3.1 * panels), constrained_layout=True)
    fig.suptitle("Incident analysis", fontsize=14, fontweight="bold")

    bucket = max(10.0, (df["epoch"].max() - df["epoch"].min()) / 60)
    df = df.assign(bucket=(df["epoch"] // bucket) * bucket)
    grouped = df.groupby("bucket").agg(
        error_rate=("failed", "mean"),
        p99=("duration_ms", lambda s: s.quantile(0.99)),
        n=("failed", "size"),
    )
    times = pd.to_datetime(grouped.index, unit="s", utc=True)

    def mark(ax):
        if change.get("found"):
            ax.axvline(
                pd.to_datetime(change["epoch"], unit="s", utc=True),
                color="crimson", linestyle="--", linewidth=1.6,
                label=f"change point {change['timestamp'][11:19]}",
            )
        if deploy_epoch:
            ax.axvline(
                pd.to_datetime(deploy_epoch, unit="s", utc=True),
                color="darkorange", linestyle=":", linewidth=1.6, label="deploy",
            )
        ax.legend(fontsize=8, loc="upper left")

    axes[0].plot(times, grouped["error_rate"] * 100, color="crimson", linewidth=1.8)
    axes[0].fill_between(times, grouped["error_rate"] * 100, color="crimson", alpha=0.15)
    axes[0].set_ylabel("error rate (%)")
    axes[0].set_title("Error rate", fontsize=10, loc="left")
    mark(axes[0])

    axes[1].plot(times, grouped["p99"], color="steelblue", linewidth=1.8)
    axes[1].set_ylabel("p99 latency (ms)")
    axes[1].set_title("Latency", fontsize=10, loc="left")
    mark(axes[1])

    if top:
        ax = axes[2]
        attr = top["attribute"]
        if top["kind"] == "numeric":
            ok = df.loc[~df["failed"], attr].dropna()
            bad = df.loc[df["failed"], attr].dropna()
            bins = np.histogram_bin_edges(pd.concat([ok, bad]), bins=20)
            ax.hist(ok, bins=bins, alpha=0.6, label="succeeded", color="seagreen")
            ax.hist(bad, bins=bins, alpha=0.6, label="failed", color="crimson")
            ax.set_xlabel(attr)
            ax.set_ylabel("requests")
        else:
            labels = [b["value"] for b in top["breakdown"]]
            values = [b["error_rate"] * 100 for b in top["breakdown"]]
            ax.bar(labels, values, color="crimson", alpha=0.75)
            ax.set_ylabel("error rate (%)")
            ax.set_xlabel(attr)
        ax.set_title(
            f"Failures by {attr}  (effect {top['effect_size']}, p={top['p_value']:.2e})",
            fontsize=10, loc="left",
        )
        ax.legend(fontsize=8)

    fig.savefig(out_path, dpi=130)
    plt.close(fig)


# --------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="CSV from get_request_sample")
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--deploy-epoch", type=float, default=None,
                        help="Deploy time to overlay, if a suspect deploy is known")
    args = parser.parse_args()

    df = load(args.csv)
    change = find_change_point(df)

    scope = df[df["epoch"] >= change["epoch"]] if change.get("found") else df
    ranked = discriminators(scope)
    top = ranked[0] if ranked else None

    nested = []
    if top:
        segment = _failing_segment(scope, top)
        if segment is not None:
            nested = discriminators(segment)
            nested = [r for r in nested if r["attribute"] != top["attribute"]]

    chart_path = f"{args.out_dir.rstrip('/')}/incident_analysis.png"
    render_chart(df, change, (nested[0] if nested else top), chart_path, args.deploy_epoch)

    report = {
        "requests_analysed": int(len(df)),
        "window": {
            "from": df["dt"].iloc[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": df["dt"].iloc[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "change_point": change,
        "blast_radius": blast_radius(df, change),
        "discriminators": ranked[:5],
        "within_affected_segment": nested[:3],
        "chart": chart_path,
    }

    if not ranked:
        report["interpretation"] = (
            "No attribute significantly separates failures from successes. The"
            " failures appear uniformly distributed across the traffic, which"
            " points at something global (the service, a dependency, or the"
            " environment) rather than a property of particular requests."
        )
    else:
        primary = nested[0] if nested else top
        if primary["kind"] == "numeric":
            report["interpretation"] = (
                f"Failures cluster on {primary['attribute']}:"
                f" {primary['direction']} values fail"
                f" (median {primary['median_when_failed']} when failing vs"
                f" {primary['median_when_ok']} when succeeding,"
                f" p={primary['p_value']:.2e})."
            )
        else:
            worst = max(primary["breakdown"], key=lambda b: b["error_rate"])
            report["interpretation"] = (
                f"Failures concentrate in {primary['attribute']}={worst['value']}"
                f" ({worst['error_rate']:.1%} error rate over"
                f" {worst['requests']} requests, p={primary['p_value']:.2e})."
            )

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
