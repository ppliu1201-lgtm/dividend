"""红利策略回测（A股，watchlist 中 market=A 且有代码的股票）

比较三种做法（每只股票分配等额资金，分红再投入）：
  1. 买入持有   ：一开始等权买入，一直拿着
  2. 股息率网格 ：按“过去12个月实际分红 ÷ 当时股价”定仓位
                  ≤4% 空仓，4%→6% 线性加到满仓，≥6% 满仓；每周检查一次
  3. 网格+周线低位：同上，但只有在周线布林带下 1/3 时才允许加仓（减仓不受限）

数据：akshare 不复权 + 后复权日线。后复权价格含分红再投入（全收益口径），
      两者之比的跳变 = 每次分红，用来算历史股息率（不用未来数据）。
费用：每次调仓按成交额 0.15% 扣（佣金+印花税近似）；空仓资金按年化 1.5% 计息。

运行：python backtest.py           （默认回测最近 10 年）
      YEARS=8 python backtest.py
      MOCK=1 python backtest.py     （模拟数据，仅测试流程）
输出：docs/backtest.html
"""
import json
import math
import os
import random
from datetime import date, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
MOCK = bool(os.getenv("MOCK"))
YEARS = int(os.getenv("YEARS", "10"))
COST = 0.0015
CASH_RATE = 0.015
LO_Y, HI_Y = 4.0, 6.0


# ---------------- 数据 ----------------
def fetch(code):
    """返回 [(date, raw_close, hfq_close)]"""
    if MOCK:
        return mock(code)
    import akshare as ak

    start = (date.today() - timedelta(days=365 * (YEARS + 1) + 40)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    raw = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="")
    hfq = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="hfq")
    h = {str(d)[:10]: float(c) for d, c in zip(hfq["日期"], hfq["收盘"])}
    return [(str(d)[:10], float(c), h[str(d)[:10]]) for d, c in zip(raw["日期"], raw["收盘"]) if str(d)[:10] in h]


def mock(code):
    r = random.Random(code)
    d, p, fct, out = date.today() - timedelta(days=int(365 * (YEARS + 1) * 1.0)), r.uniform(10, 60), 1.0, []
    paid = set()
    while d < date.today():
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        p *= 1 + r.gauss(0.0002, 0.017)
        if d.month == 7 and d.day >= 10 and d.year not in paid:
            paid.add(d.year)
            dv = p * r.uniform(0.03, 0.07)
            fct *= p / (p - dv)
            p -= dv
        out.append((d.isoformat(), round(p, 2), round(p * fct, 4)))
    return out


def dividends(rows):
    """从复权因子跳变还原每股现金分红 [(date, D)]。
    价格只到分，因子有舍入噪声：要求跳变 >0.3% 且第二天仍保持；送转股（跳变>20%）忽略。"""
    f = [h / p for _, p, h in rows]
    out = []
    for i in range(1, len(rows) - 1):
        if f[i] / f[i - 1] > 1.003 and f[i + 1] / f[i - 1] > 1.003:
            p0 = rows[i - 1][1]
            D = p0 * (1 - f[i - 1] / f[i])
            if 0 < D / p0 < 0.2:
                out.append((rows[i][0], D))
    return out


# ---------------- 指标 ----------------
def boll_low_third(closes):
    if len(closes) < 20:
        return False
    w = closes[-20:]
    m = sum(w) / 20
    sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
    lo, up = m - 2 * sd, m + 2 * sd
    return up > lo and (closes[-1] - lo) / (up - lo) <= 1 / 3


def target_weight(y):
    if y is None or y <= LO_Y:
        return 0.0
    return min(1.0, (y - LO_Y) / (HI_Y - LO_Y))


# ---------------- 回测 ----------------
def run(stocks_rows, start):
    """stocks_rows: {name: rows}. 返回 {策略: [(date, nav)]}, 交易次数"""
    names = list(stocks_rows)
    # 统一交易日
    all_dates = sorted({d for rows in stocks_rows.values() for d, _, _ in rows if d >= start})
    idx = {n: {d: (p, h) for d, p, h in rows} for n, rows in stocks_rows.items()}
    divs = {n: dividends(rows) for n, rows in stocks_rows.items()}
    weekly_hfq = {n: [] for n in names}

    strategies = ["买入持有", "股息率网格", "网格+周线低位"]
    sleeve = 1.0 / len(names)
    # 每个策略每只股票：持有份额(按后复权价计)、现金
    st = {s: {n: {"units": 0.0, "cash": sleeve} for n in names} for s in strategies}
    trades = {s: 0 for s in strategies}
    navs = {s: [] for s in strategies}
    last = {n: None for n in names}
    last_week = None
    daily_cash = (1 + CASH_RATE) ** (1 / 245) - 1

    for d in all_dates:
        wk = date.fromisoformat(d).isocalendar()[:2]
        new_week = wk != last_week
        for n in names:
            if d in idx[n]:
                last[n] = idx[n][d]
        for s in strategies:
            for n in names:
                st[s][n]["cash"] *= 1 + daily_cash

        if new_week:
            last_week = wk
            for n in names:
                if last[n] is None:
                    continue
                p, h = last[n]
                weekly_hfq[n].append(h)
                ttm = sum(D for dd, D in divs[n] if dd <= d and dd > (date.fromisoformat(d) - timedelta(days=365)).isoformat())
                y = ttm / p * 100 if ttm > 0 else None
                low = boll_low_third(weekly_hfq[n])
                for s in strategies:
                    acc = st[s][n]
                    val = acc["units"] * h + acc["cash"]
                    if s == "买入持有":
                        tw = 1.0
                    else:
                        tw = target_weight(y)
                        cur_w = acc["units"] * h / val if val else 0
                        if s == "网格+周线低位" and tw > cur_w and not low:
                            tw = cur_w  # 不在低位不加仓
                    cur_w = acc["units"] * h / val if val else 0
                    if abs(tw - cur_w) >= 0.2 or (tw == 0 and cur_w > 0) or (tw == 1 and cur_w < 0.999 and s == "买入持有"):
                        trade_val = abs(tw - cur_w) * val
                        val -= trade_val * COST
                        acc["units"] = tw * val / h
                        acc["cash"] = val - acc["units"] * h
                        trades[s] += 1
        for s in strategies:
            tot = sum(st[s][n]["units"] * last[n][1] + st[s][n]["cash"] if last[n] else st[s][n]["cash"] for n in names)
            navs[s].append((d, tot))
    return navs, trades


def stats(nav):
    d0, v0 = nav[0]
    d1, v1 = nav[-1]
    yrs = (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25
    cagr = (v1 / v0) ** (1 / yrs) - 1
    peak, mdd = 0, 0
    for _, v in nav:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    yearly, prev = {}, v0
    for i, (d, v) in enumerate(nav):
        if i == len(nav) - 1 or nav[i + 1][0][:4] != d[:4]:
            yearly[d[:4]] = v / prev - 1
            prev = v
    return {"cagr": cagr, "mdd": mdd, "total": v1 / v0 - 1, "yearly": yearly, "years": yrs}


# ---------------- 报告 ----------------
def svg_chart(navs):
    W, H, P = 860, 300, 40
    series = list(navs.items())
    allv = [v for _, nav in series for _, v in nav]
    lo, hi = min(allv), max(allv)
    n = len(series[0][1])
    colors = ["#8a94a6", "#2f6fde", "#1f9d55"]
    lines = []
    for (name, nav), c in zip(series, colors):
        step = max(1, len(nav) // 400)
        pts = " ".join(f"{P + i / (n - 1) * (W - 2 * P):.1f},{H - P - (v - lo) / (hi - lo) * (H - 2 * P):.1f}"
                       for i, (_, v) in enumerate(nav) if i % step == 0 or i == len(nav) - 1)
        lines.append(f'<polyline fill="none" stroke="{c}" stroke-width="2" points="{pts}"/>')
    yrs = sorted({d[:4] for d, _ in series[0][1]})
    ticks, lastx = [], -99
    for y in yrs:
        i = next(i for i, (d, _) in enumerate(series[0][1]) if d[:4] == y)
        x = P + i / (n - 1) * (W - 2 * P)
        if x - lastx < 40:
            continue
        lastx = x
        ticks.append(f'<text x="{x:.0f}" y="{H - 12}" font-size="11" fill="#8a94a6" text-anchor="middle">{y}</text>')
    legend = "".join(f'<span style="margin-right:14px"><i style="display:inline-block;width:14px;height:3px;background:{c};vertical-align:middle;margin-right:4px"></i>{name}</span>'
                     for (name, _), c in zip(series, colors))
    return f'<div style="font-size:12px;margin-bottom:6px">{legend}</div><svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">{"".join(lines)}{"".join(ticks)}</svg>'


def report(navs, trades, names, failed, start):
    S = {k: stats(v) for k, v in navs.items()}
    pct = lambda x: f"{x * 100:+.1f}%"
    years = sorted(next(iter(S.values()))["yearly"])
    head = "".join(f"<th>{k}</th>" for k in S)
    summary = "".join(
        f"<tr><td>{lab}</td>" + "".join(f"<td>{fn(S[k])}</td>" for k in S) + "</tr>"
        for lab, fn in [("年化收益", lambda s: pct(s["cagr"])), ("累计收益", lambda s: pct(s["total"])),
                        ("最大回撤", lambda s: pct(s["mdd"])), ("调仓次数", None)] if fn) + \
        "<tr><td>调仓次数</td>" + "".join(f"<td>{trades[k]}</td>" for k in S) + "</tr>"
    yearly = "".join(f"<tr><td>{y}</td>" + "".join(
        f"<td style='color:{'#d63a3a' if S[k]['yearly'][y] > 0 else '#1f9d55'}'>{pct(S[k]['yearly'][y])}</td>" for k in S) + "</tr>"
        for y in years)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>红利回测</title><style>
body{{margin:0;background:#f4f6fa;color:#1d2433;font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:920px;margin:0 auto;padding:16px}} .card{{background:#fff;border-radius:12px;padding:16px;margin-bottom:14px;border:1px solid #e6e9f0}}
h1{{font-size:20px;margin:6px 0 12px}} h2{{font-size:15px;margin:0 0 10px}} table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{padding:7px 8px;border-bottom:1px solid #e6e9f0;text-align:right}} th:first-child,td:first-child{{text-align:left}} th{{color:#6b7486;font-weight:500;font-size:12px}}
.hint{{color:#6b7486;font-size:12px}} @media(prefers-color-scheme:dark){{body{{background:#0f1218;color:#e7eaf0}}.card{{background:#171b23;border-color:#262c38}}th,td{{border-color:#262c38}}}}
</style></head><body><main>
<h1>红利策略回测</h1>
<div class="hint">区间 {start} 至 {navs['买入持有'][-1][0]}（约 {S['买入持有']['years']:.1f} 年）· 全收益口径（分红再投入）· 每只股票等额资金 · 共 {len(names)} 只：{'、'.join(names)}
{('<br>未纳入：' + '、'.join(failed)) if failed else ''}</div>
<div class="card"><h2>汇总</h2><table><tr><th></th>{head}</tr>{summary}</table></div>
<div class="card"><h2>净值走势</h2>{svg_chart(navs)}</div>
<div class="card"><h2>逐年收益</h2><table><tr><th>年份</th>{head}</tr>{yearly}</table></div>
<div class="card hint">
<b>规则</b>：股息率 = 过去 12 个月实际现金分红 ÷ 当时股价。网格策略 ≤{LO_Y}% 空仓、{LO_Y}%–{HI_Y}% 线性加仓、≥{HI_Y}% 满仓，每周检查，仓位变化 ≥20% 才调仓；“网格+周线低位”只在周线布林带下 1/3 时加仓。
调仓费用 {COST * 100:.2f}%，空仓资金按年化 {CASH_RATE * 100:.1f}% 计息。<br>
<b>重要偏差</b>：股票是按今天的眼光挑的（幸存者偏差），历史上被剔除、分红中断或暴跌的公司不在里面，所以回测结果会偏乐观。不考虑股息税。不构成投资建议。
</div></main></body></html>"""


def main():
    cfg = yaml.safe_load((HERE / "watchlist.yaml").read_text(encoding="utf-8"))
    start = (date.today() - timedelta(days=int(365.25 * YEARS))).isoformat()
    rows, failed = {}, []
    for s in cfg["stocks"]:
        if s.get("market", "A") != "A":
            continue
        try:
            r = fetch(s["code"])
            if not r or r[0][0] > start:
                failed.append(f"{s['name']}(上市晚于回测起点)")
                continue
            rows[s["name"]] = r
            print("ok", s["name"], len(r), "天，分红", len(dividends(r)), "次")
        except Exception as e:
            failed.append(f"{s['name']}(获取失败)")
            print("失败", s["name"], e)
    if not rows:
        raise SystemExit("没有可用数据")
    navs, trades = run(rows, start)
    for k, v in navs.items():
        s = stats(v)
        print(f"{k}: 年化 {s['cagr'] * 100:.2f}%  最大回撤 {s['mdd'] * 100:.1f}%  调仓 {trades[k]}")
    out = HERE / "docs" / "backtest.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(report(navs, trades, list(rows), failed, start), encoding="utf-8")
    print("报告已写入", out)


if __name__ == "__main__":
    main()
