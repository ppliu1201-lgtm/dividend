"""回测：周线跌破布林下轨时，等止跌 / 先买一半 / 照买，哪个更好？

对股票池里每只股票（watchlist.yaml，三个市场都算），用真实历史逐周模拟我们的底仓规则：
  - 每只股票分配同样的计划资金 1.0，没投出去的现金按年化 1.5% 计息
  - 档位：用“当时之前”的近 5 年股息率（不用未来数据）
      第 1 档 = 中位数且税后 ≥ 4.5%，第 2 档 = 80 分位，第 3 档 = 90 分位（每档至少高 6%）
      建到计划的 50% / 75% / 100%
  - 价格在周线布林中轨上方不买（三种方案都一样）
  - 减仓：20 分位卖到最高持仓的 2/3，10 分位卖到 1/3
  - 买卖按成交额扣 0.15% 费用；分红再投入（用复权价计算市值）
三种方案只在“周线跌破布林下轨”时不同：
  A 等止跌：不买，等重新站上下轨
  B 先买一半：只买本次应买金额的一半，剩下等止跌
  C 照买：不管下轨
输出 docs/backtest.html。没有考虑公司评级和 250 日线（历史评级无法回溯）。

运行：python backtest.py          （YEARS=6 回测最近 6 年，最多 6 年）
      MOCK=1 python backtest.py    （模拟数据，只测试流程）
"""
import os
from datetime import date
from pathlib import Path

import yaml

import build

HERE = Path(__file__).resolve().parent
OUT = HERE / "docs" / "backtest.html"
YEARS = min(int(os.getenv("YEARS") or 6), 6)
WIN = 260          # 档位用近 5 年
MINWIN = 156       # 至少 3 年历史
COST = 0.0015
CASH_W = 0.015 / 52
VARIANTS = [("A", "等止跌"), ("B", "先买一半"), ("C", "照买")]


def weekly(h):
    """每周最后一个交易日的下标。"""
    ends, last = [], None
    for i, d in enumerate(h["dates"]):
        k = date.fromisoformat(d).isocalendar()[:2]
        if k == last:
            ends[-1] = i
        else:
            ends.append(i)
            last = k
    return ends


def q(sv, p):
    i = (len(sv) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(sv) - 1)
    return sv[lo] + (sv[hi] - sv[lo]) * (i - lo)


def prepare(h, tax, floor_net):
    """逐周算好：股息率、档位、周线布林，供三种方案共用。"""
    ends = weekly(h)
    dd, dv = [x for x, _ in h["divs"]], [v for _, v in h["divs"]]
    ys, wk_adj, rows = [], [], []
    for t in ends:
        d, raw, adj = h["dates"][t], h["raw"][t], h["adj"][t]
        y = build.ttm_div(dd, dv, d) / raw * 100 if raw > 0 else 0
        wk_adj.append(adj)
        row = {"d": d, "adj": adj, "y": y, "lv": None}
        if len(ys) >= MINWIN and len(wk_adj) >= 20:
            sv = sorted(ys[-WIN:])
            if sv[-1] > 0:
                floor_g = floor_net / (1 - tax / 100)
                b1 = max(q(sv, 0.5), floor_g)
                b2 = max(q(sv, 0.8), b1 * 1.06)
                b3 = max(q(sv, 0.9), b2 * 1.06)
                s1 = q(sv, 0.2)
                s2 = min(q(sv, 0.1), s1 * 0.94)
                w = wk_adj[-20:]
                m = sum(w) / 20
                sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
                row.update(lv=(b1, b2, b3), sl=(s1, s2), wlow=m - 2 * sd, wmid=m)
        ys.append(y)
        rows.append(row)
    return rows


def simulate(rows, variant):
    cum = [0, 0.5, 0.75, 1.0]
    cash, sh, units, peak = 1.0, 0.0, 0.0, 0.0
    navs, invested = [], []
    for r in rows:
        cash *= 1 + CASH_W
        p = r["adj"]
        if r["lv"]:
            y = r["y"]
            nb = sum(1 for b in r["lv"] if y >= b)
            ns = sum(1 for s in r["sl"] if y <= s)
            if nb and p <= r["wmid"]:
                target = cum[nb]
                if p < r["wlow"]:
                    if variant == "A":
                        target = units
                    elif variant == "B":
                        target = units + 0.5 * max(0.0, target - units)
                amt = min(max(0.0, target - units), cash / (1 + COST))
                if amt > 1e-9:
                    sh += amt / p
                    cash -= amt * (1 + COST)
                    units += amt
                    peak = max(peak, units)
            elif ns and units > 0:
                keep = peak * (1 / 3 if ns >= 2 else 2 / 3)
                if units > keep + 1e-9:
                    frac = (units - keep) / units
                    sell = sh * frac
                    cash += sell * p * (1 - COST)
                    sh -= sell
                    units = keep
        nav = cash + sh * p
        navs.append(nav)
        invested.append(sh * p / nav if nav > 0 else 0)
    return navs, invested


def stats(navs, invested):
    n = len(navs)
    yrs = n / 52
    cagr = (navs[-1] / navs[0]) ** (1 / yrs) - 1 if yrs > 0 and navs[0] > 0 else 0
    mdd, top = 0.0, navs[0]
    for v in navs:
        top = max(top, v)
        mdd = min(mdd, v / top - 1)
    return {"cagr": cagr * 100, "tot": (navs[-1] / navs[0] - 1) * 100, "mdd": mdd * 100,
            "inv": sum(invested) / len(invested) * 100}


def main():
    cfg = yaml.safe_load((HERE / "watchlist.yaml").read_text(encoding="utf-8"))
    d = cfg["defaults"]
    floor_net = float(d.get("floor_net", 4.5))
    results, failed = [], []
    for s in cfg["stocks"]:
        mk = s.get("market", "A")
        tax = float(s["tax"]) if s.get("tax") is not None else float(d.get({"A": "tax_a", "HK": "tax_hk", "SG": "tax_sg"}[mk], 0))
        try:
            h = build.history(s["code"], mk)
            rows = prepare(h, tax, floor_net)
        except Exception as e:
            failed.append(f"{s['name']}：{str(e)[:80]}")
            print("失败", s["code"], e)
            continue
        rows = [r for r in rows if r["lv"]][-YEARS * 52:]
        if len(rows) < 104:
            failed.append(f"{s['name']}：历史不足")
            continue
        res = {"s": s}
        for v, _ in VARIANTS:
            res[v] = stats(*simulate(rows, v))
        # 下轨下方出现过几次“想买”的情况
        res["events"] = sum(1 for r in rows if r["y"] >= r["lv"][0] and r["adj"] < r["wlow"])
        res["from"] = rows[0]["d"]
        hold = [r["adj"] for r in rows]
        res["hold"] = ((hold[-1] / hold[0]) ** (52 / len(hold)) - 1) * 100
        results.append(res)
        print("ok", s["code"], {v: round(res[v]["cagr"], 2) for v, _ in VARIANTS})

    def avg(k, f="cagr", rs=None):
        rs = results if rs is None else rs
        return sum(r[k][f] for r in rs) / len(rs) if rs else 0

    def med(k, f="cagr", rs=None):
        rs = results if rs is None else rs
        xs = sorted(r[k][f] for r in rs)
        return xs[len(xs) // 2] if xs else 0

    touched = [r for r in results if r["events"] > 0]
    winAC = sum(1 for r in touched if r["A"]["cagr"] > r["C"]["cagr"] + 0.05)
    winCA = sum(1 for r in touched if r["C"]["cagr"] > r["A"]["cagr"] + 0.05)
    winBC = sum(1 for r in touched if r["B"]["cagr"] > r["C"]["cagr"] + 0.05)

    def summary_rows(rs, label):
        out = ""
        for v, name in VARIANTS:
            out += (f"<tr><td>{label}</td><td><b>{v}</b> {name}</td><td>{avg(v, rs=rs):.2f}%</td><td>{med(v, rs=rs):.2f}%</td>"
                    f"<td>{avg(v, 'mdd', rs):.1f}%</td><td>{avg(v, 'inv', rs):.0f}%</td></tr>")
        return out

    body = "".join(
        f"<tr><td>{r['s']['name']} <span class='c'>{r['s']['code']}</span></td><td>{r['events']}</td>"
        + "".join(f"<td>{r[v]['cagr']:.2f}%</td>" for v, _ in VARIANTS)
        + f"<td>{r['hold']:.2f}%</td></tr>"
        for r in sorted(results, key=lambda r: -(r['C']['cagr'] - r['A']['cagr'])))
    start = min((r["from"] for r in results), default="-")
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>规则回测</title><style>
:root{{--bg:#f4f6fa;--card:#fff;--ink:#1d2433;--sub:#6b7486;--line:#e6e9f0}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f1218;--card:#171b23;--ink:#e7eaf0;--sub:#8d96a8;--line:#262c38;color-scheme:dark}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:980px;margin:0 auto;padding:16px}} h1{{font-size:20px}} h2{{font-size:15px;margin:0 0 8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}}
.tw{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}} th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}}
th{{color:var(--sub);font-weight:500;font-size:12px}} .c,.hint{{color:var(--sub);font-size:12px}}
</style></head><body><main>
<h1>规则回测：跌破周线下轨时怎么办</h1>
<div class="hint">数据截至 {date.today().isoformat()} · 最近约 {YEARS} 年（最早从 {start}）· 每只股票计划资金相同 · 逐周模拟、不用未来数据 · 成功 {len(results)} 只</div>
<div class="card"><h2>汇总（每只股票的计划资金年化收益，含分红和现金利息）</h2>
<div class="tw"><table><tr><th>范围</th><th>方案</th><th>平均年化</th><th>中位数年化</th><th>平均最大回撤</th><th>平均投入比例</th></tr>
{summary_rows(results, '全部股票')}{summary_rows(touched, '出现过下轨下方买点的')}</table></div>
<div class="hint" style="margin-top:6px">出现过“股息率到第 1 档、但价格在周线下轨下方”的股票有 {len(touched)} 只：
A 比 C 好的 {winAC} 只，C 比 A 好的 {winCA} 只；B 比 C 好的 {winBC} 只（差距 0.05 个百分点以内算打平）。</div></div>
<div class="card"><h2>每只股票（按“照买 − 等止跌”从大到小）</h2><div class="tw"><table>
<tr><th>股票</th><th>下轨下方买点（周）</th><th>A 等止跌</th><th>B 先买一半</th><th>C 照买</th><th>一直持有</th></tr>{body}</table></div></div>
<div class="card hint">说明：没有模拟公司评级、250 日线和波段仓；股票池是按今天挑的，有幸存者偏差（当年分红出问题被剔除的股票不在里面），
这会让“照买”看起来比实际更好。失败或历史不足：{'；'.join(failed) if failed else '无'}</div>
</main></body></html>"""
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print("已写入", OUT)
    for v, name in VARIANTS:
        print(v, name, "平均年化", round(avg(v), 2), "%  触发股票平均", round(avg(v, rs=touched), 2))


if __name__ == "__main__":
    main()
