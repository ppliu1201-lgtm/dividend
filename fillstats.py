"""建仓统计：用真实历史检验“三档能不能买到、资金能投出去多少”

对股票池里每只股票（watchlist.yaml）：
  1. 取近 11 年日线 + 分红，算每周的“近 12 个月股息率”
  2. 在最近 5 年里逐周检查：用当时之前的历史（每只股票自己的 hist_years，至少 3 年）算分位，
     记录股息率达到 中位数 / 80 分位 / 90 分位 / 95 分位 的周数占比 —— 不用未来数据
  3. 资金投入模拟（每月检查一次，按账户分别算）：
       A：现规则    买 80/90/95 分位，每只计划 = 部分目标 ÷ 候选数
       G：新规则    买 中位数/80/90 分位，每档 50%/25%/25%，候选计划合计 = 部分目标 × 3，按分位从高到低先买，到目标为止
     卖出规则相同：20 分位卖 1/3，10 分位再卖 1/3，留 1/3。只统计“投入了计划的百分之几”，不计股价涨跌。
输出 docs/fillstats.html

运行：python fillstats.py      （MOCK=1 用模拟数据测试）
"""
import json
import os
from datetime import date
from pathlib import Path

import numpy as np
import yaml

import build

HERE = Path(__file__).resolve().parent
OUT = HERE / "docs" / "fillstats.html"
FWD = 260          # 最近 5 年
STEP = 4           # 每 4 周检查一次（约每月）
OVER = 3.0
W_G = [0.5, 0.25, 0.25]


def weekly_yields(s):
    h = build.history(s["code"], s.get("market", "A"))
    yh = build.yield_history(h)
    return [d for d, _ in yh], np.array([y for _, y in yh], dtype=float)


def levels_at(y, t, win):
    w = y[max(0, t - win):t]
    if len(w) < 156 or np.nanmax(w) <= 0:
        return None
    return np.quantile(w, [0.5, 0.8, 0.9, 0.95, 0.2, 0.1])


def per_stock(y, win):
    n = len(y)
    if n < FWD + 156:
        return None
    hits = np.zeros(4); cnt = 0; first = None; gap = cur_gap = 0
    for i, t in enumerate(range(n - FWD, n)):
        q = levels_at(y, t, win)
        if q is None:
            continue
        cnt += 1
        r = y[t] >= q[:4]
        hits += r
        if r[0]:
            if first is None:
                first = i
            cur_gap = 0
        else:
            cur_gap += 1
            gap = max(gap, cur_gap)
    if not cnt:
        return None
    return {"p": (hits / cnt * 100).round(0).tolist(), "first": first, "gap": gap}


def simulate(stocks, ys, wins, budget_by_bucket, rule):
    """返回每次检查时 已投入/计划总额 的比例序列（按部分汇总）。"""
    if rule == "A":
        qb_idx, over, W = [1, 2, 3], 1.0, [1 / 3] * 3
    else:
        qb_idx, over, W = [0, 1, 2], OVER, W_G
    cum = lambda n: sum(W[:n])                         # 建到第 n 档时占计划的比例
    buckets = {}
    for i, s in enumerate(stocks):
        buckets.setdefault(s["bucket"], []).append(i)
    held = {i: 0 for i in range(len(stocks))}
    peak = dict(held)
    series = []
    n = min(len(y) for y in ys if len(y)) if ys else 0
    for k in range(0, FWD, STEP):
        invested_total = 0.0
        budget_total = 0.0
        for b, idx in buckets.items():
            budget = budget_by_bucket[b]
            plan = budget * over / len(idx)              # 每只计划金额
            q = {}
            for i in idx:
                y = ys[i]
                t = len(y) - FWD + k
                if t <= 0 or t >= len(y):
                    continue
                lv = levels_at(y, t, wins[i])
                if lv is None:
                    continue
                q[i] = lv
                nb = int((y[t] >= lv[qb_idx]).sum())
                ns = int((y[t] <= lv[4:]).sum())
                # 减仓：按最高持仓 2/3、1/3 保留（held 用“占计划比例”表示）
                keep = peak[i] * (1 / 3 if ns >= 2 else 2 / 3) if ns else None
                if keep is not None and held[i] > keep:
                    held[i] = keep
                q[i] = (lv, nb, (y[t] > y[max(0, t - wins[i]):t]).mean())
            # 买：按当前分位从高到低，直到部分额度用完
            for i in sorted(q, key=lambda i: -q[i][2]):
                want = cum(q[i][1])
                if held[i] < want:
                    room = budget - sum(held[j] for j in idx) * plan
                    held[i] += max(0.0, min(want - held[i], room / plan))
                peak[i] = max(peak[i], held[i])
            invested_total += sum(held[j] for j in idx) * plan
            budget_total += budget
        series.append(invested_total / budget_total if budget_total else 0)
    return series


def main():
    cfg = yaml.safe_load((HERE / "watchlist.yaml").read_text(encoding="utf-8"))
    d = cfg["defaults"]
    targets = cfg.get("targets", {})
    stocks, ys, wins, rows = [], [], [], []
    for s in cfg["stocks"]:
        mk = s.get("market", "A")
        s = dict(s, bucket=s.get("bucket") or build.BUCKET[mk])
        try:
            dates, y = weekly_yields(s)
        except Exception as e:
            print("失败", s["code"], e)
            continue
        win = int(float(s.get("hist_years") or d.get("hist_years", 10)) * 52)
        st = per_stock(y, win)
        if st is None:
            print("历史不足", s["code"])
            continue
        stocks.append(s); ys.append(y); wins.append(win)
        rows.append((s, st))
        print("ok", s["code"], st)

    # 预算：新币账户按目标（现金除外）；A股账户单独算 100%
    def budgets(acct):
        if acct == "A":
            return {"A股": 1.0}
        t = {b: float(v) for b, v in targets.items() if b not in ("现金", "A股") and float(v) > 0}
        tot = sum(t.values()) or 1
        return {b: v / tot for b, v in t.items()}

    sims = {}
    for acct in ("SGD", "A"):
        bud = budgets(acct)
        idx = [i for i, s in enumerate(stocks) if s["bucket"] in bud]
        if not idx:
            continue
        ss = [stocks[i] for i in idx]; yy = [ys[i] for i in idx]; ww = [wins[i] for i in idx]
        sims[acct] = {r: simulate(ss, yy, ww, bud, r) for r in ("A", "G")}

    # 汇总
    p = np.array([st["p"] for _, st in rows]) if rows else np.zeros((0, 4))
    avg = p.mean(0).round(0) if len(p) else [0, 0, 0, 0]

    def yr(series, j):
        i = min(len(series) - 1, int(52 * (j + 1) / STEP) - 1)
        return series[i] * 100

    def sim_rows(acct, name):
        if acct not in sims:
            return ""
        out = ""
        for r, label in (("A", "旧规则 80/90/95"), ("G", "新规则 中位数/80/90 · 50/25/25 · 候选超额 3 倍")):
            sr = sims[acct][r]
            out += f"<tr><td>{name}</td><td>{label}</td>" + "".join(f"<td>{yr(sr, j):.0f}%</td>" for j in range(5)) + f"<td><b>{np.mean(sr) * 100:.0f}%</b></td></tr>"
        return out

    body = "".join(
        f"<tr><td>{s['name']} <span class='c'>{s['code']}</span></td><td>{s['bucket']}</td>"
        + "".join(f"<td>{v:.0f}%</td>" for v in st["p"])
        + f"<td>{'第 %d 周' % st['first'] if st['first'] is not None else '5 年内没碰到'}</td><td>{st['gap']} 周</td></tr>"
        for s, st in sorted(rows, key=lambda r: -r[1]["p"][0]))
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>建仓统计</title><style>
:root{{--bg:#f4f6fa;--card:#fff;--ink:#1d2433;--sub:#6b7486;--line:#e6e9f0}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f1218;--card:#171b23;--ink:#e7eaf0;--sub:#8d96a8;--line:#262c38;color-scheme:dark}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:980px;margin:0 auto;padding:16px}} h1{{font-size:20px}} h2{{font-size:15px;margin:0 0 8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}}
.tw{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}} th:first-child,td:first-child{{text-align:left}}
th{{color:var(--sub);font-weight:500;font-size:12px}} .c,.hint{{color:var(--sub);font-size:12px}}
</style></head><body><main>
<h1>建仓统计（真实历史）</h1>
<div class="hint">数据截至 {date.today().isoformat()} · 检查最近 5 年，每只股票只用当时之前的历史算分位（不用未来数据）· 共 {len(rows)} 只</div>
<div class="card"><h2>一只股票在任意一周处在各档的比例（平均）</h2>
<div class="tw"><table><tr><th></th><th>≥ 中位数</th><th>≥ 80 分位</th><th>≥ 90 分位</th><th>≥ 95 分位</th></tr>
<tr><td>全部股票平均</td>{''.join(f'<td><b>{v:.0f}%</b></td>' for v in avg)}</tr></table></div></div>
<div class="card"><h2>资金投入比例（占股票部分计划总额）</h2>
<div class="tw"><table><tr><th>账户</th><th>规则</th><th>第1年末</th><th>第2年末</th><th>第3年末</th><th>第4年末</th><th>第5年末</th><th>5年平均</th></tr>
{sim_rows('SGD', '新币账户（新加坡+港股）')}{sim_rows('A', 'A股账户')}</table></div>
<div class="hint" style="margin-top:6px">从全部现金开始，每月检查一次。只看“投入了计划的几成”，不计股价涨跌。</div></div>
<div class="card"><h2>每只股票</h2><div class="tw"><table>
<tr><th>股票</th><th>部分</th><th>≥中位数</th><th>≥80分位</th><th>≥90分位</th><th>≥95分位</th><th>第一次到第1档</th><th>最长没到第1档</th></tr>{body}</table></div></div>
<div class="card hint">股票池是按今天的眼光挑的，有幸存者偏差。分红削减的公司股息率会“看起来很高”，统计里会显得更容易买到，实际操作要先看分红是否安全。</div>
</main></body></html>"""
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print("已写入", OUT)
    for acct in sims:
        for r in ("A", "G"):
            print(acct, r, "5年平均投入", round(np.mean(sims[acct][r]) * 100), "%")
    print("各档比例平均", avg)


if __name__ == "__main__":
    main()
