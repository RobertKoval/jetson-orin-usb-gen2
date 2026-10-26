#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Publication figures from summary.json; needs matplotlib, not used by probes."""

import argparse
import json
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

BLUE = "#587bb6"
TEAL = "#008b7a"
INK = "#183044"
GREY = "#788a98"
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "axes.labelcolor": INK,
        "text.color": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "svg.fonttype": "none",
    }
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    data = json.loads((Path(a.folder) / "summary.json").read_text())
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    count = len(data["coverage"]["complete_pairs"])
    loaded_count = data["loaded"][0]["p50_us"]["runs"]

    def save(fig, name):
        fig.savefig(
            out / (name + ".png"), dpi=170, bbox_inches="tight", facecolor="white"
        )
        fig.savefig(out / (name + ".svg"), bbox_inches="tight", facecolor="white")
        svg = out / (name + ".svg")
        svg.write_text(
            "\n".join(line.rstrip() for line in svg.read_text().splitlines()) + "\n"
        )
        plt.close(fig)

    def axes_style(ax):
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(alpha=0.18)
        ax.set_axisbelow(True)

    labels = {"host-orin": "ПК → Orin", "orin-host": "Orin → ПК"}
    for metric in ("p50_us", "p99_us"):
        fig, axs = plt.subplots(2, 2, figsize=(12.5, 8.5))
        fig.subplots_adjust(hspace=0.4, wspace=0.24, top=0.86, bottom=0.15)
        for row, t in enumerate(("ncm", "rndis")):
            for col, d in enumerate(("host-orin", "orin-host")):
                ax = axs[row, col]
                for g, color in ((1, BLUE), (2, TEAL)):
                    r = sorted(
                        (
                            x
                            for x in data["latency"]
                            if x["generation"] == g
                            and x["transport"] == t
                            and x["direction"] == d
                        ),
                        key=lambda x: x["bytes"],
                    )
                    vals = [x[metric]["median"] / 1000 for x in r]
                    low = [(x[metric]["median"] - x[metric]["min"]) / 1000 for x in r]
                    high = [(x[metric]["max"] - x[metric]["median"]) / 1000 for x in r]
                    ax.errorbar(
                        range(4),
                        vals,
                        yerr=[low, high],
                        color=color,
                        marker="o",
                        capsize=4,
                        label=f"Gen{g} · {g * 5}G",
                    )
                ax.set_xticks(range(4), ["1 B", "20 KiB", "64 KiB", "1 MiB"])
                ax.set_yscale("log")
                axes_style(ax)
                ax.set_title(t.upper() + " · " + labels[d])
                ax.set_ylabel("RTT, мс · логарифмічна шкала")
                ax.legend(frameon=False)
        title = "p50" if metric == "p50_us" else "p99"
        fig.suptitle(
            f"{title} RTT: медіана та діапазон {count} прогонів",
            fontsize=19,
            weight="bold",
            y=0.98,
        )
        fig.text(
            0.02,
            0.025,
            "Back-to-back TCP echo, повний payload туди й назад; 100 warmup.\n1 B / 20 KiB: 2000 зразків на прогін; 64 KiB: 1000; 1 MiB: 100.\nError bars — min–max між прогонами, не довірчі інтервали. Після кожного вибору режиму — 20 с стабілізації.",
            fontsize=10,
            color=GREY,
        )
        save(fig, "campaign-latency-" + title)
    fig, axs = plt.subplots(1, 2, figsize=(12.5, 5.5))
    fig.subplots_adjust(top=0.78, bottom=0.24, wspace=0.35)
    for ax, t in zip(axs, ("ncm", "rndis")):
        for j, (g, color) in enumerate(((1, BLUE), (2, TEAL))):
            rows = [
                next(
                    x
                    for x in data["bulk"]
                    if x["generation"] == g
                    and x["transport"] == t
                    and x["direction"] == d
                    and x["label"] == "unloaded"
                )
                for d in ("host-orin", "orin-host")
            ]
            vals = [r["MiB_s"]["median"] for r in rows]
            lo = [r["MiB_s"]["median"] - r["MiB_s"]["min"] for r in rows]
            hi = [r["MiB_s"]["max"] - r["MiB_s"]["median"] for r in rows]
            pos = [i + (j - 0.5) * 0.3 for i in range(2)]
            ax.bar(
                pos,
                vals,
                width=0.28,
                color=color,
                label=f"Gen{g}",
                yerr=[lo, hi] if count > 1 else None,
                capsize=4,
            )
            for x, y, r in zip(pos, vals, rows):
                ax.text(
                    x,
                    r["MiB_s"]["max"] + max(vals) * 0.04,
                    f"{y:.0f}",
                    ha="center",
                    fontsize=10,
                )
        ax.set_xticks(range(2), ["ПК → Orin", "Orin → ПК"])
        ax.set_ylabel("MiB/s")
        ax.set_title(t.upper())
        axes_style(ax)
        ax.legend(frameon=False)
        ax.set_ylim(
            0,
            max(
                x["MiB_s"]["max"]
                for x in data["bulk"]
                if x["transport"] == t and x["label"] == "unloaded"
            )
            * 1.25,
        )
    fig.suptitle(
        f"1,25 GiB: пропускна здатність · {count} парні прогони",
        fontsize=18,
        weight="bold",
        y=0.98,
    )
    fig.text(
        0.02,
        0.035,
        f"Стовпчик — медіана; вуса — повний min–max {count} прогонів.\nНапрямки запускалися окремо. CPU-буфери; SHA-256 перевіряється окремим тестом.",
        fontsize=10,
        color=GREY,
    )
    save(fig, "campaign-throughput")
    fig, axs = plt.subplots(1, 2, figsize=(12, 5.5))
    fig.subplots_adjust(top=0.8, bottom=0.25, wspace=0.35)
    for ax, metric, title in zip(axs, ("p50_us", "p99_us"), ("p50", "p99")):
        for j, (g, color) in enumerate(((1, BLUE), (2, TEAL))):
            loaded = next(x for x in data["loaded"] if x["generation"] == g)
            idle = loaded["paired_idle"]
            rows = [idle, loaded]
            vals = [r[metric]["median"] / 1000 for r in rows]
            lo = [(r[metric]["median"] - r[metric]["min"]) / 1000 for r in rows]
            hi = [(r[metric]["max"] - r[metric]["median"]) / 1000 for r in rows]
            pos = [i + (j - 0.5) * 0.3 for i in range(2)]
            ax.bar(
                pos,
                vals,
                width=0.28,
                color=color,
                label=f"Gen{g}",
                yerr=[lo, hi] if loaded_count > 1 else None,
                capsize=4,
            )
        ax.set_xticks(range(2), ["Без NCM bulk", "Під NCM bulk"])
        ax.set_ylabel("RTT, мс")
        ax.set_title(title)
        axes_style(ax)
        ax.legend(frameon=False)
        ax.set_ylim(bottom=0)
    fig.suptitle(
        "RNDIS 20 KiB під час великої NCM-передачі ПК → Orin",
        fontsize=17,
        weight="bold",
        y=0.98,
    )
    detail = (
        "Одна завершена пара Gen1 / Gen2; idle і loaded з тієї самої пари. Це ілюстрація, не оцінка повторюваності."
        if loaded_count == 1
        else f"Завершених пар: {loaded_count}; idle і loaded з тих самих пар. Вуса — min–max між прогонами, не CI."
    )
    failure = (
        " Кампанія перервана: збій збережено окремо, не замінено числом."
        if not data["manifest"]["complete"]
        else ""
    )
    fig.text(
        0.02,
        0.03,
        detail
        + "\nДля loaded використані лише echo-зразки, повністю всередині bulk-інтервалу.\nОбидва клієнти вимірюють час на ПК."
        + failure,
        fontsize=10,
        color=GREY,
    )
    save(fig, "campaign-loaded-latency")
    print("Created four PNG and four SVG campaign figures")


if __name__ == "__main__":
    main()
