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
        row = {"d": d, "wk": date.fromisoformat(d).isocalendar()[:2], "adj": adj, "y": y, "lv": None}
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
                pct = sum(1 for x in sv if x <= y) / len(sv)
                row.update(lv=(b1, b2, b3), sl=(s1, s2), wlow=m - 2 * sd, wmid=m, pct=pct, med=q(sv, 0.5),
                           s05=min(q(sv, 0.05), s2 * 0.94))
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


WEIGHTS = {"equal": [0, 1 / 3, 2 / 3, 1.0], "front": [0, 0.5, 0.75, 1.0]}
COMBOS = [("equal", 2), ("equal", 3), ("front", 2), ("front", 3)]
COMBO_NAME = {"equal": "每档 1/3", "front": "50/25/25"}


def portfolio(stocks, targets, weights, over, weeks, swing=None, sizing="plan"):
    """账户级模拟：各部分按目标比例封顶，同一部分里股息率分位高的先买，候选计划合计 = 目标 × over。
    stocks: [(bucket, {wk: row})]。返回每周净值和股票占比。"""
    cum = WEIGHTS[weights]
    nb_bucket = {}
    for b, _ in stocks:
        nb_bucket[b] = nb_bucket.get(b, 0) + 1
    cash = 1.0
    st = [{"sh": 0.0, "units": 0.0, "peak": 0.0, "p": None, "ssh": 0.0} for _ in stocks]
    navs, inv = [], []
    sw_inv, blocked, short, cash_out = [], 0, 0.0, 0
    for wk in weeks:
        cash *= 1 + CASH_W
        for i, (b, rows) in enumerate(stocks):
            r = rows.get(wk)
            if r:
                st[i]["p"] = r["adj"]
        mv = [x["sh"] * x["p"] if x["p"] else 0.0 for x in st]
        nav = cash + sum(mv)
        # 先卖
        for i, (b, rows) in enumerate(stocks):
            r = rows.get(wk); x = st[i]
            if not r or not r["lv"] or x["units"] <= 0:
                continue
            ns = sum(1 for sl in r["sl"] if r["y"] <= sl)
            if ns:
                keep = x["peak"] * (1 / 3 if ns >= 2 else 2 / 3)
                if x["units"] > keep + 1e-12:
                    frac = (x["units"] - keep) / x["units"]
                    sell = x["sh"] * frac
                    cash += sell * x["p"] * (1 - COST)
                    x["sh"] -= sell; x["units"] = keep
        mv = [x["sh"] * x["p"] if x["p"] else 0.0 for x in st]
        # 波段（底仓之外）：先处理，看会不会挤掉底仓。波段市值和看板一样计入所在部分
        if swing is not None:
            for i, (b, rows) in enumerate(stocks):
                x = st[i]
                if not x["p"] or not swing[i]:
                    continue
                on = swing[i].get(wk)
                if on is None:
                    continue
                if not on and x["ssh"] > 0:
                    cash += x["ssh"] * x["p"] * (1 - COST); x["ssh"] = 0.0
                elif on and x["ssh"] == 0:
                    nav0 = cash + sum(mv) + sum(y["ssh"] * y["p"] for y in st if y["p"])
                    size = 0.3 * (targets.get(b, 0) * over / max(1, nb_bucket.get(b, 1)) * nav0 if sizing == "plan" else mv[i])
                    size = min(size, cash / (1 + COST))
                    if size > 1e-9:
                        x["ssh"] = size / x["p"]; cash -= size * (1 + COST)
        smv = [x["ssh"] * x["p"] if x["p"] else 0.0 for x in st]
        nav = cash + sum(mv) + sum(smv)
        # 再买：每个部分按分位从高到低
        wk_blocked = False
        sw_total = sum(smv)
        for bucket, tgt in targets.items():
            sw_b = sum(m for (b, _), m in zip(stocks, smv) if b == bucket)
            room = tgt * nav - sum(m for (b, _), m in zip(stocks, mv) if b == bucket) - sw_b
            plan = tgt * over / max(1, nb_bucket.get(bucket, 1)) * nav
            cands = []
            for i, (b, rows) in enumerate(stocks):
                r = rows.get(wk)
                if b != bucket or not r or not r["lv"]:
                    continue
                nb = sum(1 for lv in r["lv"] if r["y"] >= lv)
                if not nb or r["adj"] > r["wmid"] or r["adj"] < r["wlow"]:
                    continue
                cands.append((r["pct"], i, nb))
            for _, i, nb in sorted(cands, reverse=True):
                x = st[i]
                want = cum[nb] * plan - x["units"]
                amt = min(want, room, cash / (1 + COST))
                if swing is not None and want > 1e-9:
                    alt = min(want, room + sw_b, (cash + sw_total) / (1 + COST))
                    if alt - max(amt, 0) > 0.001 * nav:      # 没有波段仓的话本来能多买
                        wk_blocked = True; short += alt - max(amt, 0)
                if amt <= 1e-9:
                    continue
                x["sh"] += amt / x["p"]; x["units"] += amt; x["peak"] = max(x["peak"], x["units"])
                cash -= amt * (1 + COST); room -= amt
        mv = [x["sh"] * x["p"] if x["p"] else 0.0 for x in st]
        smv = [x["ssh"] * x["p"] if x["p"] else 0.0 for x in st]
        nav = cash + sum(mv) + sum(smv)
        navs.append(nav)
        inv.append(sum(mv) / nav if nav else 0)
        sw_inv.append(sum(smv) / nav if nav else 0)
        blocked += wk_blocked
        cash_out += cash < 0.01 * nav
    if swing is None:
        return navs, inv
    return navs, inv, {"sw": sum(sw_inv) / len(sw_inv) * 100, "blocked": blocked, "weeks": len(weeks),
                       "short": short, "cash_out": cash_out}


# ---------------- 底仓策略对比：我们的规则 vs 吴富贵网格 ----------------
STRATS = [("ours", "我们的规则：分位三档（中位数且税后≥4.5% / 80 / 90 分位）各 1/3；20 分位卖到 2/3、10 分位卖到 1/3，留 1/3"),
          ("ours_hold", "我们的买法，但永远不卖"),
          ("hyb", "合并版：我们的分位三档 + 税前≥6% 也算第 1 档；卖出要同时满足 ≤20 分位且 ≤4.5%、≤10 分位且 ≤4%，留 1/3，≤3.5% 清仓"),
          ("fg", "吴富贵网格：税前股息率 5% / 5.5% / 6% 各买 1/3（中轨下才买）；每份涨回 0.5 个点卖（6→5.5、5.5→5、5→4.5），4% 清仓"),
          ("fg2", "网格买法，卖得更晚：股息率 4.5% / 4% / 3.5% 各卖 1/3，3% 清仓")]
FG_BUY = (5.0, 5.5, 6.0)
FG_SELL = {"fg": (4.5, 5.0, 5.5), "fg2": (3.5, 4.0, 4.5),
           "fg3": (3.5, 5.0, 5.5)}   # fg3：6% 和 5.5% 买的两份涨回 0.5 个点就卖（吃来回波动），5% 买的第一份拿到 3.5%   # 股息率 ≤ 第 k 个值时，最多只能留 k 份


def strat_targets(strat, r, frac, peak):
    """返回 (想买到的计划比例, 最多允许持有的计划比例)；None 表示不买 / 不限。"""
    y = r["y"]
    if strat in ("ours_all", "ours_half"):
        if not r["lv"]:
            return None, None
        nb = sum(1 for lv in r["lv"] if y >= lv)
        buy = [0, 1 / 3, 2 / 3, 1.0][nb] if nb and r["wlow"] <= r["adj"] <= r["wmid"] else None
        cap = None
        if strat == "ours_all":          # 20 分位 → 2/3，10 分位 → 1/3，5 分位 → 清仓
            ns = sum(1 for sl in (*r["sl"], r["s05"]) if y <= sl)
            if ns:
                cap = peak * (2 / 3, 1 / 3, 0.0)[ns - 1]
        else:                            # 20 分位卖一半，10 分位清仓
            ns = sum(1 for sl in r["sl"] if y <= sl)
            if ns:
                cap = peak * (0.5, 0.0)[ns - 1]
        return buy, cap
    if strat in ("ours", "ours_hold", "hyb"):
        if not r["lv"]:
            return None, None
        nb = sum(1 for lv in r["lv"] if y >= lv)
        if strat == "hyb" and y >= 6.0:
            nb = max(nb, 1)
        buy = [0, 1 / 3, 2 / 3, 1.0][nb] if nb and r["wlow"] <= r["adj"] <= r["wmid"] else None
        cap = None
        if strat == "ours":
            ns = sum(1 for sl in r["sl"] if y <= sl)
            if ns:
                cap = peak * (1 / 3 if ns >= 2 else 2 / 3)
        elif strat == "hyb":
            ns = sum(1 for sl, ab in zip(r["sl"], (4.5, 4.0)) if y <= sl and y <= ab)
            if y <= 3.5:
                cap = 0.0
            elif ns:
                cap = peak * (1 / 3 if ns >= 2 else 2 / 3)
        return buy, cap
    # 网格及其变体：strat 形如 "fg2|ext|warn|pace"
    base, *opts = strat.split("|")
    ext = "ext" in opts
    levels = FG_BUY + ((6.5, 7.0) if ext else ())
    sells = FG_SELL[base] + (((5.0, 5.5) if base == "fg2" else (6.0, 6.5)) if ext else ())
    nb = sum(1 for lv in levels if y >= lv)
    buy = nb / 3 if nb and r.get("wmid") and r["adj"] <= r["wmid"] else None
    if buy is not None and "warn" in opts and (y > 8.0 or (r.get("med") and y > 2 * r["med"])):
        buy = None                                           # 股息率异常高：先查分红，不自动买
    if buy is not None and "pace" in opts:
        buy = min(buy, (int(frac * 3 + 1e-6) + 1) / 3)       # 每周最多加一格
    allowed = sum(1 for th in sells if y > th)
    cap = allowed / 3 if allowed < len(levels) else None
    return buy, cap


def simulate_s(rows, strat):
    cash, sh, frac, peak = 1.0, 0.0, 0.0, 0.0
    navs, inv = [], []
    for r in rows:
        cash *= 1 + CASH_W
        p = r["adj"]
        buy, cap = strat_targets(strat, r, frac, peak)
        if cap is not None and frac > cap + 1e-9:
            part = (frac - cap) / frac
            cash += sh * part * p * (1 - COST); sh *= 1 - part; frac = cap
        elif buy is not None and buy > frac + 1e-9:
            amt = min(buy - frac, cash / (1 + COST))
            sh += amt / p; cash -= amt * (1 + COST); frac += amt; peak = max(peak, frac)
        nav = cash + sh * p
        navs.append(nav); inv.append(sh * p / nav if nav else 0)
    return navs, inv


def portfolio_s(stocks, targets, over, weeks, strat, ov_grid=None, ov_roll=None, ov_scale=0.3, ov_pot=None):
    """账户级：和 portfolio() 一样按部分封顶、候选超额，但买卖规则换成 strat。
    ov_grid：每只股票的波段网格规则（如 "fg"）或 None，波段仓计划 = 底仓计划 × ov_scale；
    ov_roll：每只股票的滚动信号 {wk: 是否持仓} 或 None，每次买已持有底仓的 ov_scale。
    波段仓在计划之外、用现金买，市值计入所在部分（和看板一致）。返回额外的波段占比。"""
    nb_bucket = {}
    for b, _ in stocks:
        nb_bucket[b] = nb_bucket.get(b, 0) + 1
    cash = 1.0
    st = [{"sh": 0.0, "units": 0.0, "peak": 0.0, "p": None, "osh": 0.0, "ou": 0.0, "opk": 0.0} for _ in stocks]
    navs, inv, oinv = [], [], []
    sep = ov_pot is not None            # 波段用另外一笔钱（不占账户现金和部分额度）
    ocash = ov_pot or 0.0
    pot_navs = []
    omv = lambda: [x["osh"] * x["p"] if x["p"] else 0.0 for x in st]
    for wk in weeks:
        cash *= 1 + CASH_W
        for i, (b, rows) in enumerate(stocks):
            r = rows.get(wk)
            if r:
                st[i]["p"] = r["adj"]
        mv = [x["sh"] * x["p"] if x["p"] else 0.0 for x in st]
        nav = cash + sum(mv) + (0 if sep else sum(omv()))
        if sep:
            ocash *= 1 + CASH_W
        plans = {b: t * over / max(1, nb_bucket.get(b, 1)) * nav for b, t in targets.items()}
        acts = []
        for i, (b, rows) in enumerate(stocks):
            r = rows.get(wk); x = st[i]
            if not r or b not in plans or not x["p"]:
                continue
            frac = x["units"] / plans[b] if plans[b] else 0
            peakf = x["peak"] / plans[b] if plans[b] else 0
            sk = strat[i] if isinstance(strat, list) else strat
            buy, cap = strat_targets(sk, r, frac, peakf)
            if cap is not None and frac > cap + 1e-9 and x["units"] > 0:
                part = (frac - cap) / frac
                cash += x["sh"] * part * x["p"] * (1 - COST); x["sh"] *= 1 - part; x["units"] *= 1 - part
            elif buy is not None and buy > frac + 1e-9:
                # 混合时统一按“股息率比第 1 档高出多少”排序，先买最便宜的
                key = r.get("pct", 0) if sk in ("ours", "ours_hold", "hyb", "ours_all", "ours_half") else min(1.0, 0.5 + (r["y"] - FG_BUY[0]) / 4)
                acts.append((b, key, i, (buy - frac) * plans[b]))
        mv = [x["sh"] * x["p"] if x["p"] else 0.0 for x in st]
        om = [0.0] * len(st) if sep else omv()
        nav = cash + sum(mv) + sum(om)
        room = {bk: t * nav - sum(m + o for (b, _), m, o in zip(stocks, mv, om) if b == bk) for bk, t in targets.items()}
        for b, _, i, want in sorted(acts, key=lambda a: -a[1]):
            x = st[i]
            amt = min(want, room[b], cash / (1 + COST))
            if amt <= 1e-9:
                continue
            x["sh"] += amt / x["p"]; x["units"] += amt; x["peak"] = max(x["peak"], x["units"])
            cash -= amt * (1 + COST); room[b] -= amt
        # 波段仓（底仓买完之后，用剩下的现金）
        if ov_grid or ov_roll:
            for i, (b, rows) in enumerate(stocks):
                r = rows.get(wk); x = st[i]
                if not r or b not in plans or not x["p"]:
                    continue
                g = ov_grid[i] if ov_grid else None
                fl = ov_roll[i] if ov_roll else None
                if g:
                    oplan = ov_scale * plans[b]            # 波段仓最多 = 这只股票计划（底仓买满）的 30%
                    if oplan <= 1e-9:
                        continue
                    frac = x["ou"] / oplan if oplan else 0
                    buy, cap = strat_targets(g, r, frac, x["opk"] / oplan if oplan else 0)
                    if cap is not None and frac > cap + 1e-9 and x["ou"] > 0:
                        part = (frac - cap) / frac
                        got = x["osh"] * part * x["p"] * (1 - COST)
                        if sep: ocash += got
                        else: cash += got
                        x["osh"] *= 1 - part; x["ou"] *= 1 - part
                    elif buy is not None and buy > frac + 1e-9:
                        amt = min((buy - frac) * oplan, (ocash if sep else cash) / (1 + COST))
                        if amt > 1e-9:
                            x["osh"] += amt / x["p"]; x["ou"] += amt; x["opk"] = max(x["opk"], x["ou"])
                            if sep: ocash -= amt * (1 + COST)
                            else: cash -= amt * (1 + COST)
                elif fl:
                    on = fl.get(wk)
                    if on is False and x["osh"] > 0:
                        got = x["osh"] * x["p"] * (1 - COST); x["osh"] = 0.0
                        if sep: ocash += got
                        else: cash += got
                    elif on and x["osh"] == 0 and (sep or x["sh"] > 0):
                        size = ov_scale * plans[b] if sep else ov_scale * x["sh"] * x["p"]
                        amt = min(size, (ocash if sep else cash) / (1 + COST))
                        if amt > 1e-9:
                            x["osh"] = amt / x["p"]
                            if sep: ocash -= amt * (1 + COST)
                            else: cash -= amt * (1 + COST)
        mv = [x["sh"] * x["p"] if x["p"] else 0.0 for x in st]
        om = omv()
        if sep:
            nav = cash + sum(mv)
            pot = ocash + sum(om)
            pot_navs.append(pot)
            navs.append(nav + pot); inv.append(sum(mv) / nav if nav else 0); oinv.append(sum(om) / pot if pot else 0)
        else:
            nav = cash + sum(mv) + sum(om)
            navs.append(nav); inv.append(sum(mv) / nav if nav else 0); oinv.append(sum(om) / nav if nav else 0)
    if sep:
        return navs, inv, sum(oinv) / len(oinv) * 100, pot_navs
    if ov_grid or ov_roll:
        return navs, inv, sum(oinv) / len(oinv) * 100
    return navs, inv


# ---------------- 波段：三种规则 ----------------
RT_FEE = {"A": 0.0015, "HK": 0.0035, "SG": 0.0025}   # 一买一卖的总费用（近似）
SWINGS = [("ma", "5 日线上穿 13 日线买，下穿卖"),
          ("ma250", "同上，但只在 250 日线上方做（跌破 250 日线也卖）"),
          ("roll", "滚动四条规则（日线下轨+周线中下轨+税后≥5%+250 日线上；周线上轨卖；周收破 250 日线止损）")]


def _ma(xs, n):
    out, run = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        run += x
        if i >= n:
            run -= xs[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def _boll_series(xs, n=20):
    out = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        out[i] = (m - 2 * sd, m, m + 2 * sd)
    return out


def swing_test(h, mk, tax, days):
    """逐日模拟，信号用当天收盘，第二天收盘成交；持有期间的收益用复权价（含分红），空仓现金年化 1.5%。"""
    adj, raw, dates = h["adj"], h["raw"], h["dates"]
    n = len(adj)
    t0 = max(260, n - days)
    if n - t0 < 250:
        return None
    ma5, ma13, ma250 = _ma(adj, 5), _ma(adj, 13), _ma(adj, 250)
    bd = _boll_series(adj)
    dd, dv = [x for x, _ in h["divs"]], [v for _, v in h["divs"]]
    # 周线：每天对应“截至当天”的周线布林（本周用当天收盘）
    wk_close, wk_key, wb, wk_end = [], None, [None] * n, [False] * n
    for i in range(n):
        k = date.fromisoformat(dates[i]).isocalendar()[:2]
        if k != wk_key:
            wk_close.append(adj[i]); wk_key = k
        else:
            wk_close[-1] = adj[i]
        if len(wk_close) >= 20:
            w = wk_close[-20:]
            m = sum(w) / 20
            sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
            wb[i] = (m - 2 * sd, m, m + 2 * sd)
        if i + 1 < n and date.fromisoformat(dates[i + 1]).isocalendar()[:2] != k:
            wk_end[i] = True
    fee = RT_FEE.get(mk, 0.0025) / 2
    res = {}
    res["wpos"] = {}
    for name, _ in SWINGS:
        cash, sh, pos = 1.0, 0.0, False
        pending, nav, trades, wins, entry, inmkt = None, [], 0, 0, 0.0, 0
        wpos = res["wpos"][name] = {}
        for i in range(t0, n):
            p = adj[i]
            if pending == "buy" and not pos:
                sh = cash * (1 - fee) / p; cash = 0.0; pos = True; entry = p; trades += 1
            elif pending == "sell" and pos:
                cash = sh * p * (1 - fee); sh = 0.0; pos = False
                wins += p > entry * (1 + 2 * fee)
            pending = None
            wpos[date.fromisoformat(dates[i]).isocalendar()[:2]] = pos
            if not pos:
                cash *= 1 + 0.015 / 252
            else:
                inmkt += 1
            nav.append(cash + sh * p)
            # 当天收盘出信号
            up = ma5[i] > ma13[i] and ma5[i - 1] <= ma13[i - 1]
            dn = ma5[i] < ma13[i] and ma5[i - 1] >= ma13[i - 1]
            above = ma250[i] is not None and p >= ma250[i]
            if name == "ma":
                pending = "buy" if up and not pos else ("sell" if dn and pos else None)
            elif name == "ma250":
                if not pos and up and above:
                    pending = "buy"
                elif pos and (dn or not above):
                    pending = "sell"
            else:
                if not pos:
                    if bd[i] and wb[i] and above:
                        ttm = build.ttm_div(dd, dv, dates[i])
                        yn = ttm / raw[i] * 100 * (1 - tax / 100) if raw[i] > 0 else 0
                        if p <= bd[i][0] * 1.02 and wb[i][0] * 0.98 <= p <= wb[i][1] and yn >= 5:
                            pending = "buy"
                else:
                    if wb[i] and p >= wb[i][2] * 0.98:
                        pending = "sell"
                    elif wk_end[i] and not above:
                        pending = "sell"
        yrs = len(nav) / 252
        mdd, top = 0.0, nav[0]
        for v in nav:
            top = max(top, v); mdd = min(mdd, v / top - 1)
        res[name] = {"cagr": ((nav[-1] / nav[0]) ** (1 / yrs) - 1) * 100, "mdd": mdd * 100,
                     "tpy": trades / yrs, "win": (wins / trades * 100) if trades else None,
                     "inmkt": inmkt / len(nav) * 100}
    res["hold"] = ((adj[-1] / adj[t0]) ** (252 / (n - t0)) - 1) * 100
    res["from"] = dates[t0]
    return res


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
    results, failed, prepared, swing_res = [], [], [], []
    for s in cfg["stocks"]:
        mk = s.get("market", "A")
        tax = float(s["tax"]) if s.get("tax") is not None else float(d.get({"A": "tax_a", "HK": "tax_hk", "SG": "tax_sg"}[mk], 0))
        try:
            h = build.history(s["code"], mk)
            sw_ = swing_test(h, mk, tax, YEARS * 252)
            if sw_:
                swing_res.append((s, sw_))
            rows = prepare(h, tax, floor_net)
        except Exception as e:
            failed.append(f"{s['name']}：{str(e)[:80]}")
            print("失败", s["code"], e)
            continue
        rows = [r for r in rows if r["lv"]][-YEARS * 52:]
        bucket = s.get("bucket") or build.BUCKET[mk]
        if rows:
            prepared.append((bucket, {r["wk"]: r for r in rows}, mk, (sw_ or {}).get("wpos"), tax))
        if len(rows) < 104:
            failed.append(f"{s['name']}：历史不足")
            continue
        res = {"s": s}
        for v, _ in VARIANTS:
            res[v] = stats(*simulate(rows, v))
        for k, _ in STRATS:
            res[k] = stats(*simulate_s(rows, k))
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
    strat_rows = ""
    for k, label in STRATS:
        rs = results
        beat = sum(1 for r in rs if r[k]["cagr"] > r["ours"]["cagr"] + 0.05) if k != "ours" else None
        strat_rows += (f"<tr><td>每只股票单独算</td><td style='white-space:normal;max-width:380px'>{label}</td><td>{avg(k):.2f}%</td><td>{med(k):.2f}%</td>"
                       f"<td>{avg(k, 'mdd'):.1f}%</td><td>{avg(k, 'inv'):.0f}%</td><td>{'-' if beat is None else f'{beat}/{len(rs)}'}</td></tr>")
        print("策略", k, round(avg(k), 2), round(avg(k, "inv")))

    # ---- 账户级：分批比例 × 候选超额 ----
    targets = {b: float(v) / 100 for b, v in (cfg.get("targets") or {}).items() if b not in ("现金",) and float(v) > 0}
    # ---- 波段汇总 ----
    def sw_avg(k, f):
        xs = [r[k][f] for _, r in swing_res if r[k][f] is not None]
        return sum(xs) / len(xs) if xs else 0

    def sw_med(k, f="cagr"):
        xs = sorted(r[k][f] for _, r in swing_res)
        return xs[len(xs) // 2] if xs else 0
    hold_avg = sum(r["hold"] for _, r in swing_res) / len(swing_res) if swing_res else 0
    sw_rows = ""
    for k, label in SWINGS:
        beat = sum(1 for _, r in swing_res if r[k]["cagr"] > r["hold"])
        cashbeat = sum(1 for _, r in swing_res if r[k]["cagr"] > 1.5)
        sw_rows += (f"<tr><td style='white-space:normal;max-width:340px'>{label}</td><td>{sw_avg(k, 'cagr'):.2f}%</td><td>{sw_med(k):.2f}%</td>"
                    f"<td>{sw_avg(k, 'mdd'):.1f}%</td><td>{sw_avg(k, 'tpy'):.1f}</td><td>{sw_avg(k, 'win'):.0f}%</td>"
                    f"<td>{sw_avg(k, 'inmkt'):.0f}%</td><td>{beat}/{len(swing_res)}</td><td>{cashbeat}/{len(swing_res)}</td></tr>")
        print("波段", k, round(sw_avg(k, "cagr"), 2), "胜持有", beat)
    sw_rows += f"<tr><td>一直持有（对照）</td><td>{hold_avg:.2f}%</td><td>-</td><td>-</td><td>-</td><td>-</td><td>100%</td><td>-</td><td>-</td></tr>"
    sw_body = "".join(
        f"<tr><td>{s_['name']} <span class='c'>{s_['code']}</span></td>"
        + "".join(f"<td>{r[k]['cagr']:.1f}%</td>" for k, _ in SWINGS)
        + f"<td>{r['hold']:.1f}%</td><td>{r['ma']['tpy']:.1f}</td></tr>"
        for s_, r in sorted(swing_res, key=lambda x: -(x[1]['ma']['cagr'] - x[1]['hold'])))

    port_rows, crowd_rows, hi_rows, sw2_rows, keep_rows = "", "", "", "", ""
    for acct, buckets in (("新币账户（新加坡+港股，现金 15%）", {b: t for b, t in targets.items() if b != "A股"}),
                          ("A股账户（全部可投）", {"A股": 1.0})):
        ss = [(b, rows) for b, rows, _, _, _ in prepared if b in buckets]
        swp = [wp for b, rows, _, wp, _ in prepared if b in buckets]
        kinds = [("SG" if mk == "SG" else ("HKL" if mk == "HK" and tx == 0 else ("H" if mk == "HK" else "A")))
                 for b, rows, mk, _, tx in prepared if b in buckets]
        if not ss:
            continue
        weeks = sorted({wk for _, rows in ss for wk in rows})[-YEARS * 52:]
        for w, ov in COMBOS:
            navs, inv = portfolio(ss, buckets, w, ov, weeks)
            st_ = stats(navs, inv)
            port_rows += (f"<tr><td>{acct}</td><td>{COMBO_NAME[w]} · 超额 {ov} 倍</td><td>{st_['cagr']:.2f}%</td>"
                          f"<td>{st_['tot']:.0f}%</td><td>{st_['mdd']:.1f}%</td><td>{st_['inv']:.0f}%</td></tr>")
            print("组合", acct[:4], w, ov, {k: round(v, 2) for k, v in st_.items()})
        # 底仓策略对比（账户级，超额 3 倍）
        for k, label in STRATS:
            navs, inv = portfolio_s(ss, buckets, 3, weeks, k)
            st3 = stats(navs, inv)
            strat_rows += (f"<tr><td>{acct}</td><td style='white-space:normal;max-width:380px'>{label.split('：')[0]}</td><td>{st3['cagr']:.2f}%</td><td>-</td>"
                           f"<td>{st3['mdd']:.1f}%</td><td>{st3['inv']:.0f}%</td><td>-</td></tr>")
            print("账户策略", acct[:4], k, {kk: round(v, 2) for kk, v in st3.items()})
        # 混合：新加坡 + 香港本地公司用我们的规则，A 股和 H 股（内地公司）用网格
        for fgk, fgname in (("fg", "网格（原帖）"), ("fg2", "网格卖得更晚"),
                            ("fg3", "网格：上面两份做差价（6%→5.5%、5.5%→5% 卖），第一份拿到 3.5%")):
            mix = ["ours" if kd in ("SG", "HKL") else fgk for kd in kinds]
            navs, inv = portfolio_s(ss, buckets, 3, weeks, mix)
            st4 = stats(navs, inv)
            strat_rows += (f"<tr><td>{acct}</td><td style='white-space:normal;max-width:380px'><b>混合</b>：新加坡和香港本地用我们的规则，A 股 / H 股用{fgname}</td>"
                           f"<td>{st4['cagr']:.2f}%</td><td>-</td><td>{st4['mdd']:.1f}%</td><td>{st4['inv']:.0f}%</td><td>-</td></tr>")
            print("混合", acct[:4], fgk, {kk: round(v, 2) for kk, v in st4.items()})
        # 混合 2 的变体：6% 以上怎么办
        for opt, oname in (("", "6% 买满封顶（对照）"), ("|warn", "+ 股息率异常（>8% 或 >2 倍中位数）不买"),
                           ("|pace", "+ 每周最多加一格"), ("|warn|pace", "+ 异常不买 + 每周最多一格"),
                           ("|ext", "6.5% / 7% 继续各加一格（最多计划的 5/3）"), ("|ext|warn|pace", "继续加到 7% + 异常不买 + 每周一格")):
            mix = ["ours" if kd in ("SG", "HKL") else "fg2" + opt for kd in kinds]
            navs, inv = portfolio_s(ss, buckets, 3, weeks, mix)
            st5 = stats(navs, inv)
            hi_rows += (f"<tr><td>{acct}</td><td style='white-space:normal;max-width:380px'>{oname}</td><td>{st5['cagr']:.2f}%</td>"
                        f"<td>{st5['mdd']:.1f}%</td><td>{st5['inv']:.0f}%</td></tr>")
            print("高股息", acct[:4], opt or "base", {kk: round(v, 2) for kk, v in st5.items()})
        # 新加坡/香港本地的卖法：留 1/3 还是卖光；并分前后两段看是否稳定
        half = len(weeks) // 2
        for sk2, sname in (("ours", "留 1/3（现在）"), ("ours_all", "20 分位 → 2/3、10 分位 → 1/3、5 分位清仓"),
                           ("ours_half", "20 分位卖一半、10 分位清仓"), ("ours_hold", "永远不卖")):
            mix = [sk2 if kd in ("SG", "HKL") else "fg2" for kd in kinds]
            if acct.startswith("A股") and sk2 != "ours":
                continue                                   # A股账户没有新加坡/香港本地股票
            res_p = []
            for wks in (weeks, weeks[:half], weeks[half:]):
                navs, inv = portfolio_s(ss, buckets, 3, wks, mix)
                res_p.append(stats(navs, inv))
            keep_rows += (f"<tr><td>{acct}</td><td style='white-space:normal;max-width:340px'>{sname}</td><td>{res_p[0]['cagr']:.2f}%</td>"
                          f"<td>{res_p[0]['mdd']:.1f}%</td><td>{res_p[1]['cagr']:.2f}%</td><td>{res_p[2]['cagr']:.2f}%</td></tr>")
            print("卖法", acct[:4], sk2, [round(x["cagr"], 2) for x in res_p])
        # A/H 股用哪种规则：分前后两段检查网格的优势是否稳定
        for ak, aname in (("fg2", "A/H 股：网格晚卖（混合 2）"), ("fg", "A/H 股：网格原帖"), ("ours", "A/H 股：我们的分位规则")):
            mix = ["ours" if kd in ("SG", "HKL") else ak for kd in kinds]
            res_p = []
            for wks in (weeks, weeks[:half], weeks[half:]):
                navs, inv = portfolio_s(ss, buckets, 3, wks, mix)
                res_p.append(stats(navs, inv))
            keep_rows += (f"<tr><td>{acct}</td><td style='white-space:normal;max-width:340px'>{aname}</td><td>{res_p[0]['cagr']:.2f}%</td>"
                          f"<td>{res_p[0]['mdd']:.1f}%</td><td>{res_p[1]['cagr']:.2f}%</td><td>{res_p[2]['cagr']:.2f}%</td></tr>")
            print("AH规则", acct[:4], ak, [round(x["cagr"], 2) for x in res_p])
        # 底仓混合 2 + 另外一笔钱做波段（波段仓最多 = 该股底仓的 30%）
        base_mix = ["ours" if kd in ("SG", "HKL") else "fg2" for kd in kinds]
        roll_flags = [(wp or {}).get("roll") for wp in swp]
        pot0 = 0.3 * sum(buckets.values())          # 波段资金 = 股票部分的 30%
        base_navs6, base_inv6 = portfolio_s(ss, buckets, 3, weeks, base_mix)
        b6 = stats(base_navs6, base_inv6)
        sw2_rows += (f"<tr><td>{acct}</td><td>只有底仓（对照，波段资金放现金 1.5%）</td>"
                     f"<td>{stats([n + pot0 * (1 + CASH_W) ** (k + 1) for k, n in enumerate(base_navs6)], base_inv6)['cagr']:.2f}%</td>"
                     f"<td>{b6['mdd']:.1f}%</td><td>1.50%</td><td>0%</td></tr>")
        for oname, og, orl in (("波段：全部用滚动四条", None, roll_flags),
                               ("波段：A/H 股用富贵原帖网格，新加坡/香港本地用滚动四条",
                                ["fg" if kd in ("A", "H") else None for kd in kinds],
                                [fl if kd in ("SG", "HKL") else None for fl, kd in zip(roll_flags, kinds)]),
                               ("波段：全部用富贵原帖网格", ["fg"] * len(kinds), None),
                               ("波段：A/H 股用富贵原帖网格，新加坡/香港本地不做", ["fg" if kd in ("A", "H") else None for kd in kinds], None)):
            navs, inv, osw, pots = portfolio_s(ss, buckets, 3, weeks, base_mix, ov_grid=og, ov_roll=orl, ov_pot=pot0)
            st6 = stats(navs, inv)
            pst = stats(pots, [0] * len(pots))
            sw2_rows += (f"<tr><td>{acct}</td><td style='white-space:normal;max-width:380px'>{oname}</td><td>{st6['cagr']:.2f}%</td>"
                         f"<td>{st6['mdd']:.1f}%</td><td>{pst['cagr']:.2f}%</td><td>{osw:.0f}%</td></tr>")
            print("混合2+波段", acct[:4], oname[:14], round(st6["cagr"], 2), "波段资金", round(pst["cagr"], 2), round(osw))
        # 波段仓会不会挤掉底仓（每档 1/3 · 超额 3 倍）
        base_navs, base_inv = portfolio(ss, buckets, "equal", 3, weeks)
        base_st = stats(base_navs, base_inv)
        crowd_rows += (f"<tr><td>{acct}</td><td>不做波段（对照）</td><td>{base_st['cagr']:.2f}%</td><td>{base_st['inv']:.0f}%</td>"
                       f"<td>-</td><td>-</td><td>-</td><td>{sum(1 for _ in [0]) and '-'}</td></tr>")
        for k, label in SWINGS:
            for sz, szname in (("plan", "计划的 30%"), ("hold", "已持有底仓的 30%")):
                flags = [(wp or {}).get(k) for wp in swp]
                navs, inv, ex = portfolio(ss, buckets, "equal", 3, weeks, swing=flags, sizing=sz)
                st2 = stats(navs, inv)
                crowd_rows += (f"<tr><td>{acct}</td><td style='white-space:normal'>{label.split('（')[0]} · 每次 {szname}</td>"
                               f"<td>{st2['cagr']:.2f}%</td><td>{st2['inv']:.0f}%</td><td>{ex['sw']:.1f}%</td>"
                               f"<td>{ex['blocked']} / {ex['weeks']} 周</td><td>{base_st['inv'] - st2['inv']:.1f} 个百分点</td><td>{ex['cash_out']} 周</td></tr>")
                print("挤占", acct[:4], k, sz, round(st2["cagr"], 2), ex)
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
<div class="card"><h2>账户级回测：每档比例 × 候选超额（跌破下轨都按“等止跌”）</h2>
<div class="tw"><table><tr><th>账户</th><th>规则</th><th>年化</th><th>累计</th><th>最大回撤</th><th>平均股票占比</th></tr>{port_rows}</table></div>
<div class="hint" style="margin-top:6px">从全部现金开始，每周检查。同一部分里每只股票的计划 = 部分目标 × 超额倍数 ÷ 池子里的股票数；股息率分位高的先买，部分总额不超过目标。港股按港币价格计算，没算汇率变化。</div></div>
<div class="card"><h2>底仓买卖规则对比：我们的规则 vs 吴富贵网格</h2>
<div class="tw"><table><tr><th>范围</th><th>规则</th><th>平均年化</th><th>中位数</th><th>最大回撤</th><th>平均股票占比</th><th>比我们的规则好的股票</th></tr>{strat_rows}</table></div>
<div class="hint" style="margin-top:6px">网格按博主公开的做法：股息率用税前、全年分红，5% 起买、每 0.5 个点一格，只在周线中轨下买；“卖得更晚”是另一种记忆中的版本。
账户级同样按“部分封顶 + 候选超额 3 倍”，网格版先买股息率最高的。</div></div>
<div class="card"><h2>网格买到 6% 以后怎么办（混合 2：新加坡/香港本地用我们的规则，A/H 股用网格卖得更晚）</h2>
<div class="tw"><table><tr><th>账户</th><th>A/H 股网格的做法</th><th>年化</th><th>最大回撤</th><th>平均股票占比</th></tr>{hi_rows}</table></div>
<div class="hint" style="margin-top:6px">继续加格时，多出来的两格先卖：股息率回到 5.5%、5% 时各卖一格，之后照常 4.5% / 4% / 3.5%。“中位数”用该股近 5 年股息率。</div></div>
<div class="card"><h2>稳定性检查：留不留最后 1/3、A/H 股用哪种规则（前后两段分别看）</h2>
<div class="tw"><table><tr><th>账户</th><th>新加坡/香港本地的卖法</th><th>全期年化</th><th>最大回撤</th><th>前半段年化</th><th>后半段年化</th></tr>{keep_rows}</table></div>
<div class="hint" style="margin-top:6px">前半段 / 后半段：把回测期分成两半各自从现金开始算，看结论是否两段都成立。</div></div>
<div class="card"><h2>底仓用混合 2，波段仓用哪种规则？</h2>
<div class="tw"><table><tr><th>账户</th><th>波段做法</th><th>底仓 + 波段合计年化</th><th>合计最大回撤</th><th>波段资金自己的年化</th><th>波段资金平均使用率</th></tr>{sw2_rows}</table></div>
<div class="hint" style="margin-top:6px">底仓：新加坡/香港本地用我们的规则，A/H 股用网格卖得更晚。波段网格：5% / 5.5% / 6% 各买 1/3，每份涨回 0.5 个点卖（6→5.5、5.5→5、5→4.5）。
波段用另外一笔钱（= 股票部分的 30%，不占账户现金和部分额度），每只股票的波段仓最多 = 该股计划仓位（底仓买满时）的 30%，没有底仓也可以做；闲置时按年化 1.5% 计息。</div></div>
<div class="card"><h2>波段仓会不会挤掉底仓的钱？（账户级，每档 1/3 · 超额 3 倍）</h2>
<div class="tw"><table><tr><th>账户</th><th>波段做法</th><th>账户年化</th><th>平均底仓占比</th><th>平均波段占比</th><th>底仓被挤的周数</th><th>底仓占比比不做波段少</th><th>现金用光的周数</th></tr>{crowd_rows}</table></div>
<div class="hint" style="margin-top:6px">波段仓在计划之外、用账户现金买，市值和看板一样计入所在部分；“被挤”= 如果没有波段仓，那一周本来能多买的底仓。
波段按周末是否持仓近似（实际是日线进出）。</div></div>
<div class="card"><h2>波段规则回测（一笔钱全进全出，和“一直持有同一只股票”比）</h2>
<div class="tw"><table><tr><th>规则</th><th>平均年化</th><th>中位数</th><th>平均最大回撤</th><th>每年交易次数</th><th>胜率</th><th>持股时间</th><th>跑赢持有</th><th>跑赢现金 1.5%</th></tr>{sw_rows}</table></div>
<div class="hint" style="margin-top:6px">逐日模拟最近约 {YEARS} 年：当天收盘出信号、第二天收盘成交；持股期间含分红，空仓时现金年化 1.5%；
扣一买一卖费用（A股 0.15%、港股 0.35%、新加坡 0.25%）。A股波段卖出若持有不满一年还要补分红税，这里没算。</div>
<details style="margin-top:8px"><summary class="hint">每只股票明细</summary><div class="tw"><table>
<tr><th>股票</th><th>5/13 均线</th><th>5/13 + 250 日线</th><th>滚动四条</th><th>一直持有</th><th>均线每年交易</th></tr>{sw_body}</table></div></details></div>
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
