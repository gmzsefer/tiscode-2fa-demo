"""Deney analizi: results.csv → grafikler + özet tablo (analysis/ klasörüne).

Kullanım:
    python3 analyze.py                              # tüm veri
    python3 analyze.py --since 2026-10-07T04:00     # kurulum denemelerini hariç tut

Ölçütler:
  - Başarı oranı + %95 Wilson güven aralığı (telefon / süre / ses bazında)
  - Doğrulama süresi: ortalama, medyan, std, min, max (sadece başarılı denemeler)
  - TISCODE vs OTP süre farkı: Mann-Whitney U testi
Not: ortak test (all#) satırları sadece tanıma oranında kullanılır; süreleri adil değil.
"""
import argparse, csv, os, math
from collections import defaultdict
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import mannwhitneyu

OUT = "analysis"
PHONES = {"phoneA": "Lab phone A", "phoneB": "Lab phone B", "gamze": "My phone"}
OM_OFFSET = 4.0           # sesin başındaki sessizlik + OM + boşluk (sn)


def wilson(k, n, z=1.96):
    """Başarı oranı için %95 güven aralığı (küçük örneklemde normal yaklaşımdan iyi)."""
    if n == 0:
        return 0, 0, 0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0, c - h), min(1, c + h)


def stats(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    mean = sum(xs) / n
    med = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0
    return dict(n=n, mean=mean, median=med, sd=sd, min=xs[0], max=xs[-1])


def load(since):
    rows = list(csv.DictReader(open("results.csv")))
    return [r for r in rows if not since or r["time"] >= since]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="bu zamandan önceki satırları at (ISO, örn. 2026-10-07T04:00)")
    args = ap.parse_args()
    rows = load(args.since)
    os.makedirs(OUT, exist_ok=True)
    md = [f"# Experiment summary\n", f"Rows used: {len(rows)}" + (f" (since {args.since})" if args.since else "") + "\n"]

    # ---- 1) Tekli TISCODE: telefon × süre başarı oranı ----
    def attempt(r):
        return int(r.get("attempt") or 1)
    tis_rows = [r for r in rows if r["method"] == "TISCODE" and r["phone"] in PHONES
                and r["result"] in ("success", "fail")]
    single = [r for r in tis_rows if attempt(r) == 1]      # ana ölçüt: İLK denemede tanındı mı
    cell = defaultdict(lambda: [0, 0])                 # (phone, dur) -> [başarı, toplam]
    for r in single:
        c = cell[(r["phone"], int(r["duration_s"]))]
        c[1] += 1
        c[0] += r["result"] == "success"

    # ortak test (all#): her cihaz, her oturum için
    sessions = defaultdict(set)                        # dur -> oturum id'leri
    dev_hits = defaultdict(set)                        # (cihaz, dur) -> tanıdığı oturumlar
    for r in rows:
        if r["phone"].startswith("all#") and r["method"] == "TISCODE":
            d = int(r["duration_s"])
            sessions[d].add(r["phone"])
            if r["result"] == "success":
                dev_hits[(r["ack_device"], d)].add(r["phone"])

    md.append("## 1. TISCODE recognition rate (single trials, first attempt)\n")
    md.append("| Phone | Infocore (s) | Success | Rate | 95% CI |\n|---|---|---|---|---|")
    for (ph, d), (k, n) in sorted(cell.items()):
        p, lo, hi = wilson(k, n)
        md.append(f"| {PHONES[ph]} | {d} | {k}/{n} | {p:.0%} | {lo:.0%}–{hi:.0%} |")
    retried = [r for r in tis_rows if attempt(r) >= 2]
    if retried:
        ok2 = sum(r["result"] == "success" for r in retried)
        md.append(f"\n**Retries (same sound played again):** {len(retried)} retry attempts, "
                  f"{ok2} succeeded. Trials that succeeded only after a retry are counted as "
                  f"FAILED in the first-attempt rate above.")
    if sessions:
        md.append("\n## 1b. Simultaneous test (same sound, all phones)\n")
        md.append("| Device | Infocore (s) | Recognized | Rate | 95% CI |\n|---|---|---|---|---|")
        for (dev, d), hits in sorted(dev_hits.items()):
            n = len(sessions[d])
            p, lo, hi = wilson(len(hits), n)
            md.append(f"| {dev} | {d} | {len(hits)}/{n} | {p:.0%} | {lo:.0%}–{hi:.0%} |")

    # Grafik: başarı oranı vs infocore süresi
    fig, ax = plt.subplots(figsize=(6, 4))
    for ph in PHONES:
        ds = sorted(d for (p, d) in cell if p == ph)
        if not ds:
            continue
        ps, los, his = zip(*[wilson(*cell[(ph, d)]) for d in ds])
        ax.errorbar(ds, [x * 100 for x in ps],
                    yerr=[[(p - l) * 100 for p, l in zip(ps, los)], [(h - p) * 100 for p, h in zip(ps, his)]],
                    marker="o", capsize=4, label=PHONES[ph])
    ax.set_xlabel("Infocore length (s)"); ax.set_ylabel("Recognition rate (%)")
    ax.set_ylim(0, 105); ax.set_title("TISCODE recognition vs. length (95% CI)")
    ax.grid(alpha=.3); ax.legend()
    fig.tight_layout(); fig.savefig(f"{OUT}/recognition_vs_length.png", dpi=200); plt.close(fig)

    # ---- 2) Doğrulama süresi: yöntem karşılaştırması ----
    times = defaultdict(list)
    for r in rows:
        if r["result"] != "success" or r["phone"].startswith("all#"):
            continue
        if r["method"] == "TISCODE":
            tag = "" if int(r.get("attempt") or 1) == 1 else " (after retry)"
            times[f"TISCODE {r['duration_s']}s{tag}"].append(float(r["elapsed_s"]))
        else:
            times[r["method"]].append(float(r["elapsed_s"]))

    md.append("\n## 2. Authentication time (successful logins only)\n")
    md.append("| Method | n | Mean (s) | Median (s) | SD | Min | Max |\n|---|---|---|---|---|---|---|")
    for m, xs in sorted(times.items()):
        s = stats(xs)
        md.append(f"| {m} | {s['n']} | {s['mean']:.1f} | {s['median']:.1f} | {s['sd']:.1f} | {s['min']:.1f} | {s['max']:.1f} |")

    # TISCODE süresi telefon bazında (ilk denemede başarılı olanlar)
    by_phone = defaultdict(list)
    for r in single:
        if r["result"] == "success":
            by_phone[(r["phone"], r["duration_s"])].append(float(r["elapsed_s"]))
    if by_phone:
        md.append("\n**TISCODE time per phone (first-attempt successes):**\n")
        md.append("| Phone | Infocore (s) | n | Mean (s) | Median (s) | SD |\n|---|---|---|---|---|---|")
        for (ph, d), xs in sorted(by_phone.items()):
            st = stats(xs)
            md.append(f"| {PHONES[ph]} | {d} | {st['n']} | {st['mean']:.1f} | {st['median']:.1f} | {st['sd']:.1f} |")
        five = {ph: xs for (ph, d), xs in by_phone.items() if d == "5" and len(xs) >= 3}
        names = sorted(five)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                u, p = mannwhitneyu(five[names[i]], five[names[j]], alternative="two-sided")
                md.append(f"\n{PHONES[names[i]]} vs {PHONES[names[j]]} (5s time): Mann-Whitney U={u:.0f}, "
                          f"p={p:.4f} → {'significant' if p < .05 else 'not significant'} (α=0.05)")

    # Yöntem × telefon: aynı telefonda TISCODE (5s) vs OTP yöntemleri
    mp = defaultdict(list)
    for r in rows:
        if r["result"] != "success" or r["phone"] not in PHONES:
            continue
        if r["method"] == "TISCODE":
            if r["duration_s"] != "5" or int(r.get("attempt") or 1) != 1:
                continue
            m = "TISCODE 5s"
        else:
            m = r["method"]
        mp[(r["phone"], m)].append(float(r["elapsed_s"]))
    if mp:
        md.append("\n## 2b. Method comparison on the same phone (successful logins)\n")
        md.append("| Phone | Method | n | Mean (s) | Median (s) | SD | Min | Max |\n|---|---|---|---|---|---|---|---|")
        for (ph, m), xs in sorted(mp.items()):
            st = stats(xs)
            md.append(f"| {PHONES[ph]} | {m} | {st['n']} | {st['mean']:.1f} | {st['median']:.1f} | "
                      f"{st['sd']:.1f} | {st['min']:.1f} | {st['max']:.1f} |")
        for ph in PHONES:
            t = mp.get((ph, "TISCODE 5s"), [])
            for m in sorted({m for (p, m) in mp if p == ph and m != "TISCODE 5s"}):
                o = mp[(ph, m)]
                if len(t) >= 3 and len(o) >= 3:
                    u, p = mannwhitneyu(t, o, alternative="two-sided")
                    md.append(f"\n{PHONES[ph]}: TISCODE 5s vs {m}: U={u:.0f}, p={p:.4f} → "
                              f"{'significant' if p < .05 else 'not significant'}")
        # yanlış kod oranı (Authenticator)
        for ph in PHONES:
            a = [r for r in rows if r["method"] == "Authenticator OTP" and r["phone"] == ph]
            if a:
                w = sum(r["result"] == "wrong_code" for r in a)
                md.append(f"\n{PHONES[ph]} Authenticator: {w} wrong-code entries in {len(a)} submissions")
        # grafik: telefon başına yöntem kutu grafiği
        phs = [ph for ph in PHONES if any(p == ph for (p, _) in mp)]
        meths = sorted({m for (_, m) in mp}, key=lambda m: (not m.startswith("TISCODE"), m))
        fig, axes = plt.subplots(1, len(phs), figsize=(4.5 * len(phs), 4), sharey=True, squeeze=False)
        for ax, ph in zip(axes[0], phs):
            labs = [m for m in meths if (ph, m) in mp]
            ax.boxplot([mp[(ph, m)] for m in labs], showmeans=True)
            ax.set_xticks(range(1, len(labs) + 1), [f"{m}\n(n={len(mp[(ph, m)])})" for m in labs], fontsize=8)
            ax.set_title(PHONES[ph]); ax.grid(axis="y", alpha=.3)
        axes[0][0].set_ylabel("Time to authenticate (s)")
        fig.suptitle("TISCODE vs OTP methods on the same phone")
        fig.tight_layout(); fig.savefig(f"{OUT}/method_by_phone.png", dpi=200); plt.close(fig)

    tis = times.get("TISCODE 5s", [])
    for other in [m for m in times if not m.startswith("TISCODE")]:
        if len(tis) >= 3 and len(times[other]) >= 3:
            u, p = mannwhitneyu(tis, times[other], alternative="two-sided")
            md.append(f"\nTISCODE 5s vs {other}: Mann-Whitney U={u:.0f}, p={p:.4f} "
                      f"→ {'significant' if p < .05 else 'not significant'} (α=0.05)")
        else:
            md.append(f"\nTISCODE 5s vs {other}: not enough data for a test (need ≥3 each)")

    if times:
        labels = sorted(times)
        fig, ax = plt.subplots(figsize=(max(6, 1.3 * len(labels)), 4))
        ax.boxplot([times[l] for l in labels], showmeans=True)
        ax.set_xticks(range(1, len(labels) + 1), labels, rotation=20, ha="right")
        for i, l in enumerate(labels, 1):          # teorik alt sınır (ses bitmeden onay olamaz)
            if l.startswith("TISCODE"):
                lb = OM_OFFSET + int(l.split()[1].rstrip("s"))
                ax.hlines(lb, i - .3, i + .3, colors="red", linestyles="dashed")
        ax.set_ylabel("Time to authenticate (s)")
        ax.set_title("Authentication time by method (red dashed = TISCODE lower bound)")
        ax.grid(axis="y", alpha=.3)
        fig.tight_layout(); fig.savefig(f"{OUT}/time_by_method.png", dpi=200); plt.close(fig)

    # ---- 3) Ses bazında başarı (sound_labels.csv varsa türe göre de) ----
    # ses × süre tablosu: hangi ses hangi uzunlukta tanınıyor (tüm telefonlar birlikte)
    ps = defaultdict(lambda: [0, 0])                   # (ses, süre) -> [başarı, toplam]
    for r in single:
        c = ps[(r["sound"], r["duration_s"])]
        c[1] += 1; c[0] += r["result"] == "success"
    durs = sorted({d for (_, d) in ps}, key=int, reverse=True)
    md.append("\n## 3. Per-sound recognition by length (first attempt, all phones)\n")
    md.append("| Sound | " + " | ".join(f"{d}s" for d in durs) + " |\n|---|" + "---|" * len(durs))
    for snd in sorted({s for (s, _) in ps}, key=lambda x: (int(x.split("-")[0].split("_")[0]), x)):
        cells = [f"{ps[(snd, d)][0]}/{ps[(snd, d)][1]}" if ps[(snd, d)][1] else "–" for d in durs]
        md.append(f"| {snd} | " + " | ".join(cells) + " |")
    per_sound = defaultdict(lambda: [0, 0])            # 5 sn (tam uzunluk) üzerinden
    for (snd, d), (k, n) in ps.items():
        if d == "5":
            per_sound[snd][0] += k; per_sound[snd][1] += n
    bad = [f"{s} ({k}/{n})" for s, (k, n) in sorted(per_sound.items()) if n and k / n < .5]
    md.append(f"\nSounds below 50% at full length (5s): {', '.join(bad) if bad else 'none'}")
    if os.path.exists("sound_labels.csv"):
        label = {r["sound"]: r["type"].strip() for r in csv.DictReader(open("sound_labels.csv")) if r["type"].strip()}
        by_type = defaultdict(lambda: [0, 0])
        for s, (k, n) in per_sound.items():
            t = label.get(s, "unlabeled")
            by_type[t][0] += k; by_type[t][1] += n
        md.append("\n| Sound type | Success | Rate | 95% CI |\n|---|---|---|---|")
        for t, (k, n) in sorted(by_type.items()):
            p, lo, hi = wilson(k, n)
            md.append(f"| {t} | {k}/{n} | {p:.0%} | {lo:.0%}–{hi:.0%} |")

    open(f"{OUT}/summary.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\n→ charts + summary saved in {OUT}/")


if __name__ == "__main__":
    main()
