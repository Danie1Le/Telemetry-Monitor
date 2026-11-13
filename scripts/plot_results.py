# /// script
# requires-python = ">=3.12"
# dependencies = ["matplotlib==3.11.2"]
# ///
"""Draws the README charts from the committed raw results in results/<date>/.

  kind-scaling-{light,dark}.svg   queue depth and Ready processor replicas over time,
                                  results/2026-11-10/sustained-064954.json (RESULTS.md#throughput)
  aws-scaling-{light,dark}.svg    the same for the AWS capped-rate run,
                                  results/2026-11-13/sustained-024818.json (RESULTS.md#aws-throughput),
                                  with the KEDA target (queueLength in k8s/base/processor.yaml)
  detection-{light,dark}.svg      GET /alerts ("appeared") median and max per fault, kind
                                  results/2026-11-10/detection-095403.json against AWS
                                  results/2026-11-13/detection-032120.json

Every number drawn is read from those files. Each chart has a light and a dark variant for
GitHub's two themes (README embeds them with <picture>). Backgrounds are transparent; ink and
series colors are chosen per theme. Output is deterministic: fixed font (DejaVu Sans, bundled
with matplotlib, drawn as paths), fixed svg.hashsalt and no date in the SVG metadata, so
re-running gives no diff.

Run from the repo root (uv installs the pinned matplotlib in a throwaway environment):

  uv run scripts/plot_results.py

or, equivalently: uv run --no-project --with matplotlib==3.11.2 scripts/plot_results.py
"""

import itertools
import json
import re
from pathlib import Path

import matplotlib

matplotlib.use("svg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "img"
KIND_SUSTAINED = ROOT / "results/2026-11-10/sustained-064954.json"
AWS_SUSTAINED = ROOT / "results/2026-11-13/sustained-024818.json"
KIND_DETECTION = ROOT / "results/2026-11-10/detection-095403.json"
AWS_DETECTION = ROOT / "results/2026-11-13/detection-032120.json"
PROCESSOR_MANIFEST = ROOT / "k8s/base/processor.yaml"
FAULTS = ["spike", "dropout", "stuck", "drift"]

# Reference palette from the dataviz skill: categorical slots 1 (blue) and 2 (orange),
# each stepped for its theme, validated against GitHub's light (#ffffff) and dark (#0d1117)
# page surfaces (CVD and normal-vision separation, contrast >= 3:1).
THEMES = {
    "light": {
        "primary": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "band": "#898781",
        "band_alpha": 0.10,
        "series": ["#2a78d6", "#eb6834"],
        "surface": "#ffffff",
    },
    "dark": {
        "primary": "#f0efec",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "band": "#c3c2b7",
        "band_alpha": 0.07,
        "series": ["#3987e5", "#d95926"],
        "surface": "#0d1117",
    },
}

plt.rcParams.update(
    {
        "svg.hashsalt": "telemetry-monitor",
        "svg.fonttype": "path",
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.linewidth": 1,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "xtick.major.pad": 6,
        "ytick.major.pad": 6,
        "lines.solid_capstyle": "round",
        "lines.solid_joinstyle": "round",
    }
)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def keda_target() -> int:
    match = re.search(
        r'queueLength:\s*"(\d+)"', PROCESSOR_MANIFEST.read_text(encoding="utf-8")
    )
    if not match:
        raise SystemExit(f"queueLength not found in {PROCESSOR_MANIFEST}")
    return int(match.group(1))


def style_axes(ax, theme: dict) -> None:
    ax.set_facecolor("none")
    for side in ("left", "bottom"):
        ax.spines[side].set_color(theme["axis"])
    ax.tick_params(colors=theme["muted"], labelcolor=theme["secondary"])
    ax.grid(axis="y", color=theme["grid"], linewidth=1)
    ax.set_axisbelow(True)


def header(fig, title: str, subtitle: str, theme: dict) -> None:
    fig.text(
        0.012,
        0.975,
        title,
        ha="left",
        va="top",
        fontsize=12.5,
        weight="bold",
        color=theme["primary"],
    )
    fig.text(
        0.012,
        0.915,
        subtitle,
        ha="left",
        va="top",
        fontsize=9.5,
        color=theme["secondary"],
    )


def save(fig, name: str) -> None:
    fig.savefig(OUT / name, format="svg", transparent=True, metadata={"Date": None})
    plt.close(fig)


def scaling_chart(
    run: dict, theme: dict, title: str, subtitle: str, target: int | None
):
    start = run["start"]
    load_end = run["loadEnd"] - start
    t = [s["t"] - start for s in run["timeline"]]
    queue = [s["queue"] for s in run["timeline"]]
    ready = [s["ready"] for s in run["timeline"]]
    blue = theme["series"][0]

    fig, (top, bottom) = plt.subplots(
        2,
        1,
        sharex=True,
        figsize=(8.3, 4.9),
        gridspec_kw={"height_ratios": [3, 1.4], "hspace": 0.18},
    )
    fig.subplots_adjust(left=0.085, right=0.975, top=0.80, bottom=0.11)
    header(fig, title, subtitle, theme)

    for ax in (top, bottom):
        style_axes(ax, theme)
        ax.axvspan(
            0, load_end, color=theme["band"], alpha=theme["band_alpha"], linewidth=0
        )

    top.fill_between(t, queue, color=blue, alpha=0.10, linewidth=0)
    top.plot(t, queue, color=blue, linewidth=2)
    top.set_ylim(0, max(queue) * 1.12)
    top.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
    top.set_ylabel("Queue depth (messages)", color=theme["secondary"])
    top.text(
        0,
        1.03,
        f"Shaded: k6 load, {run['env']['RATE']} batches/s",
        transform=top.transAxes,
        ha="left",
        va="bottom",
        fontsize=9,
        color=theme["secondary"],
    )
    if target is not None:
        top.axhline(target, color=theme["secondary"], linewidth=1)
        top.text(
            1,
            1.03,
            f"Gray line: KEDA target, {target} messages per replica",
            transform=top.transAxes,
            ha="right",
            va="bottom",
            fontsize=9,
            color=theme["secondary"],
        )

    bottom.step(t, ready, where="post", color=blue, linewidth=2)
    bottom.set_ylim(0, max(ready) + 0.6)
    bottom.set_yticks(range(max(ready) + 1))
    bottom.set_ylabel("Ready replicas", color=theme["secondary"])
    bottom.set_xlim(0, t[-1])
    bottom.set_xlabel("Time since run start (s)", color=theme["secondary"])
    return fig


def kind_scaling(theme: dict) -> object:
    run = load(KIND_SUSTAINED)
    timeline = run["timeline"]
    ready = [s["ready"] for s in timeline]
    q = run["queueDepth"]
    title = (
        f"kind: KEDA scaled the processor {min(ready)} to {max(ready)} replicas; "
        "the queue did not grow"
    )
    subtitle = (
        f"Sustained run, {run['env']['RATE']} batches/s for {seconds(run)} s. "
        f"Queue peaked at {q['max']}; last-minute mean {q['lastMinuteMean']} vs "
        f"first-minute {q['firstMinuteMean']}."
    )
    return scaling_chart(run, theme, title, subtitle, None)


def aws_scaling(theme: dict) -> object:
    run = load(AWS_SUSTAINED)
    timeline = run["timeline"]
    desired = [s["desired"] for s in timeline]
    rises = sum(1 for a, b in itertools.pairwise(desired) if b > a)
    q = run["queueDepth"]
    title = (
        f"AWS: one replica fell behind at {run['env']['RATE']} batches/s; "
        f"KEDA added a second {rises} times"
    )
    subtitle = (
        f"{seconds(run)} s on real SQS and DynamoDB. "
        f"Queue peaked at {q['max']}; last-minute mean {q['lastMinuteMean']} vs "
        f"first-minute {q['firstMinuteMean']}."
    )
    return scaling_chart(run, theme, title, subtitle, keda_target())


def seconds(run: dict) -> int:
    return int(run["env"]["DURATION"].removesuffix("s"))


def appeared(detection: dict) -> dict:
    """Per fault: (median, max, runs with an alert) of GET /alerts time, from the summary."""
    by_fault: dict[str, list[dict]] = {}
    for row in detection["summary"]:
        by_fault.setdefault(row["scenario"], []).append(row)
    out = {}
    for fault in FAULTS:
        rows = by_fault[fault]
        values = {(r["appearedMedian"], r["appearedMax"], r["runs"]) for r in rows}
        # spike raises two alerts (threshold and rate_of_change); both rows must agree.
        if len(values) != 1:
            raise SystemExit(f"{fault}: rules disagree {rows}")
        out[fault] = values.pop()
    return out


def detection_chart(theme: dict) -> object:
    kind_raw, aws_raw = load(KIND_DETECTION), load(AWS_DETECTION)
    envs = [
        ("kind", appeared(kind_raw), kind_raw["runs"], theme["series"][0]),
        ("AWS", appeared(aws_raw), aws_raw["runs"], theme["series"][1]),
    ]

    fig, ax = plt.subplots(figsize=(8.3, 4.4))
    fig.subplots_adjust(left=0.11, right=0.72, top=0.72, bottom=0.20)
    header(
        fig,
        "Time to alert in GET /alerts, per fault: kind against AWS",
        "Median (dot) and max (ring) seconds from the fault's start to the first poll "
        "that showed the alert",
        theme,
    )
    style_axes(ax, theme)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=theme["grid"], linewidth=1)
    ax.spines["left"].set_visible(False)
    ax.axvline(0, color=theme["axis"], linewidth=1)

    offsets = (-0.17, 0.17)
    yticks, ylabels = [], []
    for i, fault in enumerate(FAULTS):
        yticks.append(i)
        ylabels.append(fault)
        for (name, values, total, color), dy in zip(envs, offsets, strict=True):
            median, worst, runs = values[fault]
            y = i + dy
            ax.plot([median, worst], [y, y], color=color, linewidth=2, zorder=2)
            ax.scatter(
                [worst],
                [y],
                s=64,
                facecolor=theme["surface"],
                edgecolor=color,
                linewidth=2,
                zorder=3,
            )
            ax.scatter(
                [median],
                [y],
                s=64,
                color=color,
                edgecolor=theme["surface"],
                linewidth=2,
                zorder=4,
            )
            runs_note = f" ({runs} of {total} runs)" if runs != total else ""
            ax.annotate(
                f"{name}: {median:.1f} / {worst:.1f} s{runs_note}",
                xy=(1, y),
                xycoords=("axes fraction", "data"),
                xytext=(8, 0),
                textcoords="offset points",
                va="center",
                fontsize=9,
                color=theme["secondary"],
                annotation_clip=False,
            )

    ax.set_yticks(yticks, ylabels)
    ax.tick_params(axis="y", labelsize=10.5, labelcolor=theme["primary"])
    ax.set_ylim(len(FAULTS) - 0.5, -0.5)
    ax.set_xlabel(
        "Seconds after the fault starts (median / max)", color=theme["secondary"]
    )

    handles = [
        Line2D(
            [],
            [],
            color=color,
            linewidth=2,
            marker="o",
            markersize=7,
            markeredgecolor=theme["surface"],
            label=f"{name} ({total} runs per fault)",
        )
        for name, _, total, color in envs
    ]
    legend = ax.legend(
        handles=handles,
        loc="lower left",
        bbox_to_anchor=(0, 1.0),
        ncols=2,
        frameon=False,
        fontsize=9,
        borderaxespad=0.3,
        handlelength=2.2,
    )
    for text in legend.get_texts():
        text.set_color(theme["secondary"])
    if envs[0][1]["spike"][0] < 0:
        fig.text(
            0.012,
            0.025,
            "kind spike reads below 0 s: the host and flight clocks differ "
            "(RESULTS.md, detection note).",
            fontsize=8.5,
            color=theme["secondary"],
        )
    return fig


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for mode, theme in THEMES.items():
        save(kind_scaling(theme), f"kind-scaling-{mode}.svg")
        save(aws_scaling(theme), f"aws-scaling-{mode}.svg")
        save(detection_chart(theme), f"detection-{mode}.svg")
    print(f"wrote 6 SVGs to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
