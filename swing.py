"""波段（区间高抛低吸）识别、信号、历史回测

只用于“波段仓”（默认占该股计划仓位的 30%），底仓仍按股息率网格管理。

一、什么样才算“横盘区间”（全部满足）
  1. 在 5 天前为止的最近 N 个交易日（N 依次试 160/120/90/60，取最长满足的）里，
     区间上沿 hi / 下沿 lo 取收盘价的 97% / 3% 分位（去掉个别极端针），宽度 hi/lo-1 在 8%–30% 之间
  2. 上沿区（区间上 20%）和下沿区（下 20%）各至少被碰过 2 次（两次之间隔 ≥8 个交易日）
  3. 周线布林带宽度 (上轨-下轨)/中轨 ≤ 20%
  4. 周线 20 均线走平：和 4 周前相比变化 ≤ 3%
  区间用 5 天前为止的数据画，这样最近几天的突破/跌破能被识别出来，而不是被“吸收”进区间。

二、信号
  波段买入区：价格在区间下 20%；“确认”= 收盘站在日线布林下轨之上且比前一天收高（止跌）
  波段卖出区：价格在区间上 20%
  跌破止损：收盘 < lo × (1-1.5%) → 卖掉波段仓（底仓不动）
  向上突破：收盘 > hi × (1+1.5%) → 停止波段，不追高
  邮件只在“当天刚出现”时提醒，避免天天重复。

三、回测（近 3 年，逐日滚动，不用未来数据）
  空仓时出现“确认买入”→ 收盘买入，冻结当时的区间；
  持有时：到卖出区卖出 / 跌破止损卖出 / 持有满 120 个交易日卖出。
  每次买卖扣双边费用（默认 0.4%）。与同期一直持有对比。
"""
from datetime import date

DEFAULTS = {
    "enabled": True, "sleeve": 30, "windows": [160, 120, 90, 60], "lag": 5,
    "min_width": 8.0, "max_width": 30.0, "max_bandwidth": 20.0, "max_ma_slope": 3.0,
    "edge_pct": 20.0, "edge_q": 3.0, "zone_pct": 20.0, "stop_pct": 1.5, "min_touches": 2, "touch_gap": 8,
    "fee_roundtrip": 0.4, "max_hold": 120, "bt_days": 750,
}


def conf(cfg):
    c = dict(DEFAULTS)
    c.update((cfg.get("defaults") or {}).get("swing") or {})
    return c


# ---------------- 基础计算 ----------------
def _boll(vals, n=20, k=2):
    if len(vals) < n:
        return None
    w = vals[-n:]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return m - k * sd, m, m + k * sd


def week_index(dates):
    """每个交易日属于第几周（从 0 开始）以及每周最后一个交易日的下标。"""
    wid, last_key, ends = [], None, []
    for i, d in enumerate(dates):
        key = date.fromisoformat(d).isocalendar()[:2]
        if key != last_key:
            ends.append(i)
            last_key = key
        else:
            ends[-1] = i
        wid.append(len(ends) - 1)
    return wid, ends


def weekly_upto(closes, wid, ends, t):
    """截至第 t 天的周线收盘序列（本周用第 t 天收盘代替）。"""
    w = wid[t]
    return [closes[ends[j]] for j in range(w)] + [closes[t]]


def count_touches(seg, lo_line, hi_line, gap):
    """seg 中进入 ≤lo_line 区、≥hi_line 区的次数（两次之间至少 gap 天）。"""
    bot = top = 0
    last_bot = last_top = -10 ** 9
    in_bot = in_top = False
    for i, c in enumerate(seg):
        if c <= lo_line:
            if not in_bot and i - last_bot >= gap:
                bot += 1
            in_bot, last_bot = True, i
        else:
            in_bot = False
        if c >= hi_line:
            if not in_top and i - last_top >= gap:
                top += 1
            in_top, last_top = True, i
        else:
            in_top = False
    return bot, top


def _q(sorted_vals, q):
    i = (len(sorted_vals) - 1) * q
    a, b = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return sorted_vals[a] + (sorted_vals[b] - sorted_vals[a]) * (i - a)


def detect_box(closes, t, weekly, c, manual=None):
    """以第 t 天为“今天”，识别区间。返回 dict 或 None。"""
    if manual:
        lo, hi = float(manual["lo"]), float(manual["hi"])
        return {"lo": lo, "hi": hi, "days": None, "touch": None, "manual": True}
    lag = int(c["lag"])
    wb = _boll(weekly)
    if not wb or wb[1] <= 0:
        return None
    bandwidth = (wb[2] - wb[0]) / wb[1] * 100
    if bandwidth > c["max_bandwidth"]:
        return None
    if len(weekly) >= 24:
        ma_now = sum(weekly[-20:]) / 20
        ma_prev = sum(weekly[-24:-4]) / 20
        if abs(ma_now / ma_prev - 1) * 100 > c["max_ma_slope"]:
            return None
    end = t - lag + 1
    for n in sorted(c["windows"], reverse=True):
        start = end - n
        if start < 0:
            continue
        seg = closes[start:end]
        ss = sorted(seg)
        lo, hi = _q(ss, c["edge_q"] / 100), _q(ss, 1 - c["edge_q"] / 100)
        if lo <= 0:
            continue
        width = (hi / lo - 1) * 100
        if not (c["min_width"] <= width <= c["max_width"]):
            continue
        edge = (hi - lo) * c["edge_pct"] / 100
        bot, top = count_touches(seg, lo + edge, hi - edge, int(c["touch_gap"]))
        if bot >= c["min_touches"] and top >= c["min_touches"]:
            return {"lo": lo, "hi": hi, "days": n, "touch": [bot, top], "bandwidth": round(bandwidth, 1), "manual": False}
    return None


def zones(box, c):
    lo, hi = box["lo"], box["hi"]
    r = hi - lo
    z = r * c["zone_pct"] / 100
    return {"buy_hi": lo + z, "sell_lo": hi - z, "stop": lo * (1 - c["stop_pct"] / 100),
            "breakout": hi * (1 + c["stop_pct"] / 100)}


def state_at(closes, t, box, c):
    """第 t 天相对区间的状态。"""
    p = closes[t]
    z = zones(box, c)
    if p < z["stop"]:
        return "跌破"
    if p > z["breakout"]:
        return "突破"
    if p <= z["buy_hi"]:
        db = _boll(closes[: t + 1])
        confirm = db is not None and p >= db[0] and t > 0 and p > closes[t - 1]
        return "买入确认" if confirm else "买入区"
    if p >= z["sell_lo"]:
        return "卖出区"
    return "中部"


# ---------------- 回测 ----------------
def backtest(dates, closes, c, manual=None):
    n = len(closes)
    days = int(c["bt_days"])
    t0 = max(200, n - days)
    if n - t0 < 120:
        return None
    wid, ends = week_index(dates)
    fee = c["fee_roundtrip"] / 100
    pos, entry, entry_t, frozen = False, 0.0, 0, None
    trades, in_mkt = [], 0
    for t in range(t0, n):
        if pos:
            in_mkt += 1
            z = zones(frozen, c)
            p = closes[t]
            why = None
            if p >= z["sell_lo"]:
                why = "卖出区"
            elif p < z["stop"]:
                why = "止损"
            elif t - entry_t >= c["max_hold"]:
                why = "超时"
            if why:
                trades.append({"in": dates[entry_t], "out": dates[t], "buy": round(entry, 4), "sell": round(p, 4),
                               "ret": round((p / entry - 1 - fee) * 100, 2), "why": why, "days": t - entry_t})
                pos = False
            continue
        weekly = weekly_upto(closes, wid, ends, t)
        box = detect_box(closes, t, weekly, c, manual)
        if box and state_at(closes, t, box, c) == "买入确认":
            pos, entry, entry_t, frozen = True, closes[t], t, box
    open_ret = None
    if pos:
        open_ret = round((closes[-1] / entry - 1) * 100, 2)
    if not trades and not pos:
        return {"n": 0, "from": dates[t0], "hold": round((closes[-1] / closes[t0] - 1) * 100, 1)}
    comp = 1.0
    for x in trades:
        comp *= 1 + x["ret"] / 100
    wins = sum(1 for x in trades if x["ret"] > 0)
    return {
        "n": len(trades), "wins": wins, "win_rate": round(wins / len(trades) * 100) if trades else None,
        "avg": round(sum(x["ret"] for x in trades) / len(trades), 2) if trades else None,
        "comp": round((comp - 1) * 100, 1), "stops": sum(1 for x in trades if x["why"] == "止损"),
        "hold": round((closes[-1] / closes[t0] - 1) * 100, 1), "from": dates[t0],
        "in_mkt": round(in_mkt / (n - t0) * 100), "open": open_ret, "trades": trades[-6:],
    }


# ---------------- 对外接口 ----------------
def analyze(dates, closes, cfg, s):
    """返回给网页/邮件用的波段信息。closes 用前复权收盘价。"""
    c = conf(cfg)
    if not c["enabled"] or s.get("swing") is False or len(closes) < 220:
        return None
    manual = s.get("swing_box")
    wid, ends = week_index(dates)
    t = len(closes) - 1
    box = detect_box(closes, t, weekly_upto(closes, wid, ends, t), c, manual)
    bt = backtest(dates, closes, c, manual)
    out = {"active": bool(box), "bt": bt, "sleeve": c["sleeve"]}
    if not box:
        return out
    z = zones(box, c)
    st = state_at(closes, t, box, c)
    prev = state_at(closes, t - 1, box, c)
    p = closes[t]
    lo, hi = box["lo"], box["hi"]
    out.update({
        "lo": round(lo, 4), "hi": round(hi, 4), "width": round((hi / lo - 1) * 100, 1),
        "days": box["days"], "touch": box["touch"], "manual": box["manual"], "bandwidth": box.get("bandwidth"),
        "buy_hi": round(z["buy_hi"], 4), "sell_lo": round(z["sell_lo"], 4),
        "stop": round(z["stop"], 4), "breakout": round(z["breakout"], 4),
        "pos": round((p - lo) / (hi - lo) * 100, 1) if hi > lo else None,
        "state": st, "new": st != prev and st not in ("买入区", "中部"),
        "space": round((z["sell_lo"] / z["buy_hi"] - 1 - c["fee_roundtrip"] / 100) * 100, 1),
        "chart": [round(x, 4) for x in closes[-(max(box["days"] or 120, 120) + 10):]],
        "chart_from": dates[-(max(box["days"] or 120, 120) + 10)],
    })
    return out


def alert_text(sw):
    """当天需要邮件提醒的波段信号（只在状态刚变化时）。"""
    if not sw or not sw.get("active") or not sw.get("new"):
        return None
    st = sw["state"]
    if st == "买入确认":
        return f"波段买入：区间 {sw['lo']:.3g}–{sw['hi']:.3g} 下沿止跌，波段卖出区 ≥ {sw['sell_lo']:.3g}，止损 < {sw['stop']:.3g}"
    if st == "卖出区":
        return f"波段卖出：进入区间上沿（≥ {sw['sell_lo']:.3g}），卖出波段仓"
    if st == "跌破":
        return f"区间跌破：收盘低于止损 {sw['stop']:.3g}，卖出波段仓（底仓不动）"
    if st == "突破":
        return f"区间突破：收盘高于 {sw['breakout']:.3g}，停止波段、不追高"
    return None
