"""三地红利看板 · 每日数据生成 + 邮件提醒（A股 / 港股 / 新加坡）

每个交易日 17:30（新加坡时间）运行：
  1. 读 watchlist.yaml
  2. 取近 11 年日线和分红：A股、港股用 akshare（不复权 + 后复权，从复权因子还原每次分红），
     新加坡用 yfinance（自带分红记录）
  3. 每只股票：近 12 个月分红、税后股息率、过去 10 年股息率分位 → 买入区 / 持有 / 减仓区，
     日/周/月布林带、股息率网格、计划触发、AH 溢价
  4. 写 docs/data.json（网页读取），有提醒就发邮件

环境变量：SMTP_USER / SMTP_PASS / MAIL_TO / ALWAYS_SEND / PAGE_URL / MOCK=1（模拟数据）
"""
import json
import os
import random
import smtplib
import time
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import yaml

import swing

HERE = Path(__file__).resolve().parent
OUT = HERE / "docs" / "data.json"
SGT = timezone(timedelta(hours=8))
MOCK = bool(os.getenv("MOCK"))
YEARS = 11
QCCY = {"A": "CNY", "HK": "HKD", "SG": "SGD"}
BUCKET = {"A": "A股", "HK": "港股", "SG": "新加坡"}
MOCK_RANGE = {"00728", "O39", "Z74", "601088", "600036", "C38U"}
GRID_YIELDS = [3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0]


# ======================= 数据获取 =======================
def history(code, market):
    """返回 dict: dates, raw(不复权收盘), adj(前复权收盘，用于布林带), chg(涨跌幅%), divs[(date, 每股现金分红)]"""
    if MOCK:
        return mock_history(code, market)
    start = (date.today() - timedelta(days=365 * YEARS + 30)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    if market == "SG":
        dates, raw, adj, divs = yf_history(code + ".SI", start)
        src = "Yahoo"
    else:
        # 东方财富接口在海外服务器上经常被限流：依次试 东方财富(重试) → 新浪 → Yahoo
        # 港股的新浪复权数据推算分红不准，港股优先用 Yahoo（直接给分红记录）
        errs = []
        srcs = [("东财", lambda: em_history(code, market, start, end)),
                ("东财重试", lambda: (time.sleep(3), em_history(code, market, start, end))[1])]
        alt = [("新浪", lambda: sina_history(code, market, start)),
               ("Yahoo", lambda: yf_history(yf_symbol(code, market), start))]
        srcs += alt[::-1] if market == "HK" else alt
        for name, fn in srcs:
            try:
                dates, raw, adj, divs = fn()
                if len(dates) <= 200:
                    errs.append(f"{name}: 数据太少"); continue
                ttm = ttm_div_raw([x for x, _ in divs], [v for _, v in divs], dates[-1])
                if ttm / raw[-1] > 0.15:            # 股息率超过 15% 基本是数据错了，换下一个来源
                    errs.append(f"{name}: 分红数据异常（近12月 {ttm:.3g}，股价 {raw[-1]:.3g}）"); continue
                src = name
                break
            except Exception as e:
                errs.append(f"{name}: {type(e).__name__} {str(e)[:80]}")
        else:
            raise RuntimeError(" | ".join(errs))
    # 涨跌幅用复权价算（除权日不显示成大跌）
    chg = [0.0] + [(adj[i] / adj[i - 1] - 1) * 100 for i in range(1, len(adj))]
    return {"dates": dates, "raw": raw, "adj": adj, "chg": chg, "divs": divs, "src": src}


def yf_symbol(code, market):
    if market == "HK":
        return f"{int(code):04d}.HK"
    return code + (".SS" if code.startswith(("6", "9")) else ".SZ")


def yf_history(sym, start):
    import yfinance as yf

    df = yf.Ticker(sym).history(start=f"{start[:4]}-{start[4:6]}-{start[6:]}", auto_adjust=False, actions=True)
    df = df.dropna(subset=["Close"])
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    raw = [float(x) for x in df["Close"]]
    adj = [float(x) for x in df["Adj Close"]]
    k = raw[-1] / adj[-1]
    adj = [x * k for x in adj]
    divs = [(d.strftime("%Y-%m-%d"), float(v)) for d, v in zip(df.index, df["Dividends"]) if v and v > 0]
    return dates, raw, adj, divs


def _merge_raw_hfq(pairs_raw, pairs_hfq):
    hm = {d: c for d, c in pairs_hfq}
    dates, raw, hfq = [], [], []
    for d, c in pairs_raw:
        if d in hm and c > 0:
            dates.append(d), raw.append(c), hfq.append(hm[d])
    k = raw[-1] / hfq[-1]
    return dates, raw, [x * k for x in hfq], divs_from_factor(dates, raw, hfq)


def em_history(code, market, start, end):
    import akshare as ak

    fn = ak.stock_zh_a_hist if market == "A" else ak.stock_hk_hist
    r = fn(symbol=code, period="daily", start_date=start, end_date=end, adjust="")
    h = fn(symbol=code, period="daily", start_date=start, end_date=end, adjust="hfq")
    return _merge_raw_hfq([(str(d)[:10], float(c)) for d, c in zip(r["日期"], r["收盘"])],
                          [(str(d)[:10], float(c)) for d, c in zip(h["日期"], h["收盘"])])


def sina_history(code, market, start):
    import akshare as ak

    if market == "A":
        sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
        end = date.today().strftime("%Y%m%d")
        r = ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end, adjust="")
        h = ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end, adjust="hfq")
    else:
        r = ak.stock_hk_daily(symbol=code, adjust="")
        h = ak.stock_hk_daily(symbol=code, adjust="hfq")
    s0 = f"{start[:4]}-{start[4:6]}-{start[6:]}"
    pr = [(str(d)[:10], float(c)) for d, c in zip(r["date"], r["close"]) if str(d)[:10] >= s0]
    ph = [(str(d)[:10], float(c)) for d, c in zip(h["date"], h["close"]) if str(d)[:10] >= s0]
    return _merge_raw_hfq(pr, ph)


def divs_from_factor(dates, raw, hfq):
    """复权因子跳变 → 每股现金分红。价格只到分有舍入噪声：跳变>0.3%且次日保持才算；送转(>20%)忽略。"""
    f = [h / p for p, h in zip(raw, hfq)]
    out = []
    for i in range(1, len(f) - 1):
        if f[i] / f[i - 1] > 1.003 and f[i + 1] / f[i - 1] > 1.003:
            D = raw[i - 1] * (1 - f[i - 1] / f[i])
            if 0 < D / raw[i - 1] < 0.2:
                out.append((dates[i], D))
    return out


def fetch_valuation_a(code):
    """A股 PE(TTM)/PB 当前值及 1年/10年 分位。"""
    if MOCK:
        r = random.Random(code)
        return {"pe": round(r.uniform(5, 25), 1), "pb": round(r.uniform(0.6, 4), 2),
                "pe_p1": r.randint(1, 99), "pe_p10": r.randint(1, 99), "pb_p1": r.randint(1, 99), "pb_p10": r.randint(1, 99)}
    try:
        import akshare as ak

        df = ak.stock_value_em(symbol=code).dropna(subset=["PE(TTM)", "市净率"])
        pe, pb = df["PE(TTM)"].astype(float).tolist(), df["市净率"].astype(float).tolist()
        pct = lambda s, n: round(sum(1 for x in s[-n:] if x <= s[-1]) / len(s[-n:]) * 100)
        return {"pe": round(pe[-1], 1), "pb": round(pb[-1], 2), "pe_p1": pct(pe, 245), "pe_p10": pct(pe, 2450),
                "pb_p1": pct(pb, 245), "pb_p10": pct(pb, 2450)}
    except Exception as e:
        print("估值失败", code, e)
        return None


def fetch_valuation_yf(code, market):
    """港股 / 新加坡 当前 PE、PB（yfinance，无历史分位）。"""
    if MOCK:
        r = random.Random(code + "v")
        return {"pe": round(r.uniform(6, 20), 1), "pb": round(r.uniform(0.6, 2.5), 2)}
    try:
        import yfinance as yf

        t = code + ".SI" if market == "SG" else f"{int(code):04d}.HK"
        info = yf.Ticker(t).info
        pe, pb = info.get("trailingPE"), info.get("priceToBook")
        if pe is None and pb is None:
            return None
        return {"pe": round(pe, 1) if pe else None, "pb": round(pb, 2) if pb else None}
    except Exception as e:
        print("估值失败", code, e)
        return None


def fetch_fx(cfg):
    """返回 1 单位外币 = ? SGD"""
    fb = {"CNY": 0.18, "HKD": 0.167, "SGD": 1.0}
    fb.update(cfg.get("fx_fallback") or {})
    if MOCK:
        return fb
    try:
        import requests

        r = requests.get("https://open.er-api.com/v6/latest/SGD", timeout=15).json()["rates"]
        return {"CNY": 1 / r["CNY"], "HKD": 1 / r["HKD"], "SGD": 1.0}
    except Exception as e:
        print("汇率失败，用备用值", e)
        return fb


def mock_history(code, market):
    r = random.Random(code)
    base = {"601318": 52.26, "600036": 42.5, "601336": 51.4, "000333": 85.5, "000651": 36.65, "000423": 48.4,
            "000538": 53.7, "600750": 21.6, "600132": 37.46, "600066": 28.6, "601088": 40.1, "000807": 25.3,
            "600096": 26.7, "02318": 48.28, "03968": 52.10, "01336": 30.2, "01088": 33.5, "00941": 82.0,
            "00006": 48.0, "00002": 77.15, "00728": 4.395, "01038": 50.0, "00003": 6.5, "00883": 19.0, "00857": 7.5, "00386": 4.6, "00939": 7.6, "01398": 6.0, "03988": 4.6, "00177": 10.0, "00548": 8.0, "00144": 13.0,
            "CJLU": 0.92, "BN4": 7.2, "U96": 6.2, "C52": 1.45, "V03": 13.5, "OV8": 1.95, "F34": 3.1, "G13": 0.75, "M44U": 1.30, "N2IU": 1.30,
            "AJBU": 2.32, "BUOU": 0.98, "C2PU": 4.0, "HMN": 0.95, "601939": 9.0, "601988": 5.6, "600938": 26.0, "601857": 8.5, "601728": 6.79,
            "600548": 13.0, "600900": 28.0, "600941": 105.0, "601398": 7.5, "601288": 6.2, "601166": 22.0, "600028": 6.2, "601225": 22.0,
            "601006": 6.3, "600377": 13.0, "000895": 26.0, "600887": 28.0, "000858": 120.0, "600398": 7.5, "00823": 38.0, "06823": 10.5, "02388": 35.0, "00005": 100.0, "00066": 27.0, "D05": 58.8, "O39": 17.5, "U11": 36.0, "Z74": 4.1, "S68": 15.0,
            "S63": 7.6, "C38U": 2.44, "A17U": 2.7, "ME8U": 2.3, "J69U": 2.25}.get(code, 30)
    y0 = r.uniform(0.035, 0.07)
    d = date.today() - timedelta(days=int(365 * YEARS))
    p = base * r.uniform(0.6, 1.2)
    dates, raw, adj, divs, fct = [], [], [], [], 1.0
    paid = set()
    n_est = int(365 * YEARS * 5 / 7)
    drift = (base / p) ** (1 / n_est)
    while d < date.today():
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        p = p * drift * (1 + r.gauss(0, 0.014))
        for m in (5, 11) if market != "A" else (7,):
            key = (d.year, m)
            if d.month == m and d.day >= 8 and key not in paid:
                paid.add(key)
                D = p * y0 / (2 if market != "A" else 1) * r.uniform(0.85, 1.15)
                fct *= p / (p - D)
                p -= D
                divs.append((d.isoformat(), D))
        dates.append(d.isoformat()), raw.append(p), adj.append(p * fct)
    if code in MOCK_RANGE:  # 模拟最近半年横盘，测试波段识别
        import math
        n = 150
        anchor = raw[-n - 1]
        ph = r.uniform(0, 6.28)
        for i in range(n):
            j = len(raw) - n + i
            raw[j] = anchor * (1 + 0.06 * math.sin(i / 9.0 + ph) + r.gauss(0, 0.008))
            adj[j] = raw[j] * fct
    k = base / raw[-1]
    raw = [round(x * k, 3) for x in raw]
    divs = [(a, b * k) for a, b in divs]
    adj = [x * raw[-1] / adj[-1] for x in adj]
    dates[-1] = date.today().isoformat()
    chg = [0.0] + [(adj[i] / adj[i - 1] - 1) * 100 for i in range(1, len(adj))]
    return {"dates": dates, "raw": raw, "adj": adj, "chg": chg, "divs": divs}


# ======================= 计算 =======================
def resample(dates, closes, key):
    out, last = [], None
    for d, c in zip(dates, closes):
        dt = date.fromisoformat(d)
        k = dt.isocalendar()[:2] if key == "W" else (dt.year, dt.month)
        if k == last:
            out[-1] = c
        else:
            out.append(c)
            last = k
    return out


def boll(closes, n=20, k=2):
    if len(closes) < n:
        return None
    w = closes[-n:]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return [round(m - k * sd, 4), round(m, 4), round(m + k * sd, 4)]


def zone(price, b):
    if not b:
        return None
    lo, _, up = b
    if up == lo:
        return "中"
    p = (price - lo) / (up - lo)
    return "上" if p >= 2 / 3 else ("下" if p <= 1 / 3 else "中")


def ttm_div(divs_dates, divs_vals, d):
    """近 12 个月分红，按派息次数校正派息日漂移：
    - 近 365 天的派息次数 比 前一个 365 天 多（且超过 2 次）→ 去掉最早的几次
      （例：半年派一次的信托，今年派息日比去年早几天，窗口里会出现 3 次；银行把末期派息提前，也会出现 3 次）
    - 比前一年少 → 补上 400 天内最近的几次（例：今年中期还没派、去年同期已派）
    公司从“一年一次”改成“一年两次”的（如招行）不受影响。"""
    dlo = date.fromisoformat(d)
    lo, plo, lim = ((dlo - timedelta(days=n)).isoformat() for n in (365, 730, 400))
    i1 = bisect_right(divs_dates, d)
    ilo = bisect_right(divs_dates, lo)
    iplo = bisect_right(divs_dates, plo)
    cur = list(range(ilo, i1))
    pn = ilo - iplo
    if pn and len(cur) > max(pn, 2):
        # 只有窗口里最早和最晚两次相隔接近一年（同一期分红出现了两次）才去掉；
        # 信托提前派的“预付分红”（如 CICT 增发前的 advanced distribution）间隔短，不去掉
        gaps = sorted(g for g in ((date.fromisoformat(divs_dates[k]) - date.fromisoformat(divs_dates[k - 1])).days
                                  for k in range(max(1, i1 - 8), i1)) if g >= 30)
        gap = gaps[len(gaps) // 2] if gaps else 180
        while len(cur) > max(pn, 2) and \
                (date.fromisoformat(divs_dates[cur[-1]]) - date.fromisoformat(divs_dates[cur[0]])).days >= 365 - 0.5 * gap:
            cur.pop(0)
    elif pn and len(cur) < pn:
        j = ilo - 1
        while j >= 0 and divs_dates[j] > lim and len(cur) < pn:
            cur.insert(0, j)
            j -= 1
    return sum(divs_vals[i] for i in cur)


def ttm_div_raw(divs_dates, divs_vals, d):
    """d 当日（含）往前 365 天的分红合计（不校正）。"""
    lo = (date.fromisoformat(d) - timedelta(days=365)).isoformat()
    i1 = bisect_right(divs_dates, d)
    total, i = 0.0, i1 - 1
    while i >= 0 and divs_dates[i] > lo:
        total += divs_vals[i]
        i -= 1
    return total


def yield_history(h):
    """每周一个点：(date, 近12月分红/当时股价 %)，从有满 1 年数据开始。"""
    dd = [d for d, _ in h["divs"]]
    dv = [v for _, v in h["divs"]]
    start = (date.fromisoformat(h["dates"][0]) + timedelta(days=366)).isoformat()
    out, last_wk = [], None
    for d, p in zip(h["dates"], h["raw"]):
        if d < start:
            continue
        wk = date.fromisoformat(d).isocalendar()[:2]
        y = ttm_div(dd, dv, d) / p * 100
        if wk == last_wk:
            out[-1] = (d, y)
        else:
            out.append((d, y))
            last_wk = wk
    return out


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    i = (len(sorted_vals) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def roll_rules_at(h, n, dps_net, d):
    """吴富贵“滚动游戏”四条硬规则，用前 n 根日线（含第 n-1 根）判断。
    ① 日线到布林下轨（差不多就行：≤ 下轨 ×1.02）
    ② 周线在中轨到下轨之间（允许略低于下轨 2%）
    ③ 税后股息率 ≥ roll_min_net（默认 5%）
    ④ 没跌破 250 日线
    卖：到周线上轨（≥ 上轨 ×0.98）；跌破 250 日线：停止滚动"""
    adj, dates = h["adj"][:n], h["dates"][:n]
    if len(adj) < 260:
        return None
    p = adj[-1]
    bd, bw = boll(adj), boll(resample(dates, adj, "W"))
    ma250 = sum(adj[-250:]) / 250
    raw_p = h["raw"][n - 1]
    yn = dps_net / raw_p * 100 if dps_net > 0 and raw_p > 0 else 0
    if not bd or not bw:
        return None
    r = [p <= bd[0] * 1.02,
         bw[0] * 0.98 <= p <= bw[1],
         yn >= float(d.get("roll_min_net", 5.0)),
         p >= ma250]
    return {"r": r, "buy": all(r), "sell": p >= bw[2] * 0.98, "stop": p < ma250,
            "ma250": round(ma250, 4), "dlow": round(bd[0], 4), "wlow": round(bw[0], 4),
            "wmid": round(bw[1], 4), "wup": round(bw[2], 4), "yn": round(yn, 2)}


def weekly_stop(h):
    """止损：周线收盘跌破 250 日线（只看已收完的周，避免一两天插针）。
    new_wstop 只在这一周刚收完的那天为真，邮件不会每天重复。"""
    dates, adj = h["dates"], h["adj"]
    n = len(adj)
    if n < 260:
        return {"wstop": False, "new_wstop": False}
    ends, last = [], None
    for i, d0 in enumerate(dates):
        k = date.fromisoformat(d0).isocalendar()[:2]
        if k == last:
            ends[-1] = i
        else:
            ends.append(i); last = k
    today = date.fromisoformat(dates[-1])
    if today.weekday() < 4:            # 本周还没收完（周五才算收周）
        ends = ends[:-1]
    if len(ends) < 2 or ends[-2] < 250:
        return {"wstop": False, "new_wstop": False}
    below = lambda i: adj[i] < sum(adj[i - 249:i + 1]) / 250
    e, ep = ends[-1], ends[-2]
    w, wp = below(e), below(ep)
    fresh = e == n - 1 or (e == n - 2 and date.fromisoformat(dates[e]).weekday() < 4)  # 周五休市时下周一补报
    return {"wstop": w, "new_wstop": w and not wp and fresh, "wclose": round(adj[e], 4),
            "wclose_date": dates[e]}


def build_stock(s, cfg, fx):
    d = cfg["defaults"]
    mk = s.get("market", "A")
    qccy = QCCY[mk]
    h = history(s["code"], mk)
    price, chg, trade_date = h["raw"][-1], h["chg"][-1], h["dates"][-1]
    chg5 = (h["adj"][-1] / h["adj"][-6] - 1) * 100 if len(h["adj"]) > 6 else None

    # ---- 分红 ----
    dd, dv = [x for x, _ in h["divs"]], [v for _, v in h["divs"]]
    ttm = ttm_div(dd, dv, trade_date)
    if s.get("dps") not in (None, ""):
        ccy = s.get("dps_ccy", qccy)
        dps = float(s["dps"]) * fx.get(ccy, 1) / fx[qccy]  # 换算到股价币种
        dps_src = "手动"
    else:
        dps, dps_src = ttm, "近12月"
    tax = float(s["tax"]) if s.get("tax") is not None else float(d.get({"A": "tax_a", "HK": "tax_hk", "SG": "tax_sg"}[mk], 0))
    dps_net = dps * (1 - tax / 100)
    yg = dps / price * 100 if dps > 0 else None
    yn = dps_net / price * 100 if dps > 0 else None

    # ---- 历史股息率分位 ----
    yh_full = yield_history(h)
    win_years = float(s.get("hist_years") or d.get("hist_years", 10))   # 分位用最近几年（“换挡”的股票用 5 年）
    # 自动识别“近几年才改成高分红”的股票（如东阿阿胶、云天化）：
    # 分位只从“近 18 个月平均分红一直不低于现在的 25%”那段开始算，避免拿以前不分红的年份来比
    regime = False
    if dps > 0 and yh_full:
        dd_, dv_ = [x for x, _ in h["divs"]], [v for _, v in h["divs"]]
        cut = 0
        # 用近 18 个月分红 × 2/3 判断（派息日每年前后差几周时，近 12 月会短暂少算一次，不能当成“分红变少”）
        def avg18(x):
            lo = (date.fromisoformat(x) - timedelta(days=548)).isoformat()
            return sum(v for dx, v in zip(dd_, dv_) if lo < dx <= x) * 365 / 548
        first = date.fromisoformat(h["dates"][0]) + timedelta(days=548)
        for i, (x, _) in enumerate(yh_full):
            if date.fromisoformat(x) >= first and avg18(x) < 0.25 * dps:
                cut = i + 1
        auto_years = (len(yh_full) - cut) / 52
        if cut and auto_years < win_years:
            win_years, regime = round(max(auto_years, 0.1), 1), True
    yh = yh_full[-int(win_years * 52):]
    hist_years = len(yh) / 52
    sv = sorted(y for _, y in yh)
    sv5 = sorted(y for _, y in yh_full[-260:])
    buy_pct, sell_pct = float(d.get("buy_pct", 80)), float(d.get("sell_pct", 20))
    if hist_years >= 3 and sv and sv[-1] > 0 and yg is not None:
        mode = "分位"
        ypct = round(bisect_right(sv, yg) / len(sv) * 100)
        ypct5 = round(bisect_right(sv5, yg) / len(sv5) * 100) if len(sv5) >= 156 else None
        y_buy, y_sell, y_med = quantile(sv, buy_pct / 100), quantile(sv, sell_pct / 100), quantile(sv, 0.5)
    else:
        mode = "固定"
        ypct = ypct5 = None
        # 固定规则以税后为准，换回税前便于统一比较
        y_buy = float(s.get("buy_yield", d["buy_yield"])) / (1 - tax / 100) if tax < 100 else None
        y_sell = float(s.get("sell_yield", d["sell_yield"])) / (1 - tax / 100) if tax < 100 else None
        y_med = None
    buy_price = dps / (y_buy / 100) if dps > 0 and y_buy else None
    sell_price = dps / (y_sell / 100) if dps > 0 and y_sell else None

    # ---- 分批阶梯（税前股息率口径；价格 = 每股分红 ÷ 股息率） ----
    # 买：第1档=历史中位数，第2档=80分位，第3档=90分位；历史不足：买入线 −0.5 / 买入线 / +0.5 个百分点（税后）
    #   （模拟显示：80/90/95 起步时 5 年平均只有约 4 成资金能投出去；中位数起步 + 候选股超额后约 7 成以上）
    # 卖：第1档=减仓线(20分位)，第2档=10分位；历史不足用 减仓线 −0.5 个百分点（税后）
    # 相邻两档至少拉开约 6%，避免历史分布太集中时几档挤在一起
    ladder = None
    if dps > 0 and y_buy and y_sell:
        if mode == "分位":
            b1, b2, b3 = quantile(sv, 0.50), y_buy, quantile(sv, 0.90)
            s1, s2 = y_sell, quantile(sv, 0.10)
        else:
            k = 1 - tax / 100
            b1, b2, b3 = y_buy - 0.5 / k, y_buy, y_buy + 0.5 / k
            s1, s2 = y_sell, y_sell - 0.5 / k
        # 股息率底线（借鉴吴富贵：股息率 ≥ 5% 才起买）：第1档税后不低于 floor_net
        floor_g = float(d.get("floor_net", 4.5)) / (1 - tax / 100) if tax < 100 else 0
        b1 = max(b1, floor_g)
        b2 = max(b2, b1 * 1.06)
        b3 = max(b3, b2 * 1.06)
        s2 = min(s2, s1 * 0.94)
        lv = lambda y: {"y": round(y, 3), "yn": round(y * (1 - tax / 100), 2), "price": round(dps / (y / 100), 4)}
        ladder = {"buy": [lv(b1), lv(b2), lv(b3)], "sell": [lv(s1), lv(s2)] if s2 > 0 else [lv(s1)]}
    if yg is None:
        yzone = None
    elif yg >= y_buy:
        yzone = "买入区"
    elif yg <= y_sell:
        yzone = "减仓区"
    else:
        yzone = "持有区"

    # ---- 布林带 ----
    b_d = boll(h["adj"])
    b_w = boll(resample(h["dates"], h["adj"], "W"))
    b_m = boll(resample(h["dates"], h["adj"], "M"))

    # ---- 250 日线（借鉴吴富贵：牛熊分界）+ 滚动四条规则 ----
    ma250 = sum(h["adj"][-250:]) / 250 if len(h["adj"]) >= 250 else None
    roll = roll_rules_at(h, len(h["adj"]), dps_net, d)
    roll_prev = roll_rules_at(h, len(h["adj"]) - 1, dps_net, d)
    if roll:
        rp = roll_prev or {}
        roll["new_buy"] = roll["buy"] and not rp.get("buy")
        roll["new_sell"] = roll["sell"] and not rp.get("sell")
        roll["new_stop"] = roll["stop"] and not rp.get("stop", True)
        # 第二笔：已有波段仓时，再次碰到日线下轨、且还在 250 日线上
        roll["add2"] = roll["r"][0] and roll["r"][3]
        roll["new_add2"] = roll["add2"] and not (rp.get("r") or [False])[0]
        roll.update(weekly_stop(h))

    # ---- 网格（税后股息率对应价格） ----
    grid = []
    if dps_net > 0:
        for y in GRID_YIELDS:
            gp = dps_net / (y / 100)
            grid.append({"y": y, "price": round(gp, 3), "reached": price <= gp, "dist": round((gp / price - 1) * 100, 1)})

    # ---- 信号 ----
    sig, alert = [], []
    plan_out = None
    plan = s.get("plan") or {}
    if plan:
        if "price_le" in plan:
            target, cond = float(plan["price_le"]), f"现价 ≤ {plan['price_le']}"
        else:
            target = dps_net / (float(plan["yield_ge"]) / 100) if dps_net > 0 else None
            cond = f"税后股息率 ≥ {plan['yield_ge']}%"
        hit = target is not None and price <= target
        plan_out = {"cond": cond, "target": round(target, 3) if target else None,
                    "dist": round((target / price - 1) * 100, 2) if target else None, "hit": hit}
        if hit:
            sig.append("计划触发：" + cond)
            alert.append("计划触发：" + cond)

    near = float(d["boll_near_pct"])
    low = b_w is not None and price <= b_w[0] * (1 + near / 100)
    if yzone == "买入区":
        sig.append(f"买入区：股息率{'处于历史 ' + str(ypct) + ' 分位' if ypct is not None else ' ' + format(yn, '.2f') + '%'}")
        alert.append(sig[-1])
    if low:
        dl = (price / b_w[0] - 1) * 100
        t = "周线跌破下轨" if dl < 0 else f"周线低位：距下轨 {dl:.1f}%"
        sig.append(t)
    if yzone == "减仓区":
        t = f"减仓区：股息率{'处于历史 ' + str(ypct) + ' 分位' if ypct is not None else ' ' + format(yn, '.2f') + '%'}"
        sig.append(t)
        alert.append(t)
    if b_w is not None and price >= b_w[2]:
        sig.append("周线突破上轨")

    # ---- 滚动信号（波段仓）----
    # 买入第一笔：发给所有股票；第二笔 / 卖出 / 止损：只发给 watchlist 里 roll_holding 名单（正在做波段的）
    holding_roll = str(s["code"]) in {str(x) for x in (d.get("roll_holding") or [])}
    if roll:
        roll["holding"] = holding_roll
        if roll["new_buy"]:
            alert.append("滚动买入第一笔（波段仓的一半）：四条都满足（日线下轨 · 周线中轨到下轨 · 税后股息率 ≥ 5% · 在 250 日线上）")
        if holding_roll:
            if roll["new_add2"] and not roll["new_buy"]:
                alert.append(f"滚动第二笔：再次碰到日线下轨（约 {roll['dlow']:.4g}）且仍在 250 日线上；已买过两笔就不再加")
            if roll["new_sell"]:
                alert.append(f"滚动卖出：到周线上轨（约 {roll['wup']:.4g}），分批卖出波段仓")
            if roll["new_wstop"]:
                alert.append(f"滚动止损：周线收盘 {roll['wclose']:.4g} 跌破 250 日线（约 {roll['ma250']:.4g}）→ 卖出波段仓；"
                             "A 级且底仓这一档还没买够的，可以转入底仓不卖（看板上点“转入底仓”）")

    # ---- 波段 ----
    sw = swing.analyze(h["dates"], h["adj"], cfg, s)
    if sw and sw.get("active"):
        sig.append(f"波段：{sw['state']}（区间 {sw['lo']:.4g}–{sw['hi']:.4g}）")
        t = swing.alert_text(sw)
        if t:
            alert.append(t)
    star = yzone == "买入区" and (low or zone(price, b_w) == "下")
    if star:
        status = "低位机会"
    elif yzone == "减仓区" or zone(price, b_w) == "上":
        status = "高位观察"
    else:
        status = "持有观望"

    if mk == "A":
        val = fetch_valuation_a(s["code"])
    else:
        val = fetch_valuation_yf(s["code"], mk)

    step = max(1, len(yh) // 260)
    stepf = max(1, len(yh_full) // 260)
    return {
        "code": s["code"], "market": mk, "name": s["name"], "sector": s.get("sector", "其他"),
        "bucket": s.get("bucket") or BUCKET[mk], "pair": s.get("pair"), "date": trade_date,
        "price": round(price, 4), "chg": round(chg, 2), "chg5": round(chg5, 2) if chg5 is not None else None,
        "qccy": qccy, "fx": fx[qccy],
        "dps": round(dps, 4), "dps_src": dps_src, "ttm": round(ttm, 4), "tax": tax, "dps_net": round(dps_net, 4),
        "yield": round(yn, 2) if yn is not None else None, "yield_gross": round(yg, 2) if yg is not None else None,
        "mode": mode, "ypct": ypct, "ypct5": ypct5, "hist_years": round(hist_years, 1), "win_years": win_years, "regime": regime, "src": h.get("src"),
        "wbreak": bool(b_w and price < b_w[0]),
        "y_buy": round(y_buy, 2) if y_buy else None, "y_sell": round(y_sell, 2) if y_sell else None,
        "y_med": round(y_med, 2) if y_med else None,
        "buy_price": round(buy_price, 3) if buy_price else None, "sell_price": round(sell_price, 3) if sell_price else None,
        "tag": s.get("tag"), "yzone": yzone, "ladder": ladder, "lot": s.get("lot") or (100 if mk in ("SG", "A") else None),
        "boll": {"d": b_d, "w": b_w, "m": b_m},
        "zone": {"d": zone(price, b_d), "w": zone(price, b_w), "m": zone(price, b_m)},
        "grid": grid, "plan": plan_out, "signals": sig, "alerts": alert, "status": status, "star": star,
        "val": val, "spark": [round(c, 4) for c in h["adj"][-60:]],
        "yhist": [[x, round(y, 2)] for i, (x, y) in enumerate(yh_full) if i % stepf == 0 or i == len(yh_full) - 1],
        "divs_recent": [[x, round(v, 4)] for x, v in h["divs"][-12:]],
        "swing": sw, "roll": roll, "ma250": round(ma250, 4) if ma250 else None,
        "below250": bool(ma250 and price < ma250), "rating": s.get("rating"),
    }


def add_ah(stocks, fx):
    by = {(x["market"], x["code"]): x for x in stocks}
    for hs in stocks:
        if hs["market"] != "HK" or not hs["pair"]:
            continue
        a = by.get(("A", hs["pair"]))
        if not a:
            continue
        prem = (a["price"] * fx["CNY"]) / (hs["price"] * fx["HKD"]) - 1
        better = None
        if a["yield"] is not None and hs["yield"] is not None:
            better = "H" if hs["yield"] > a["yield"] else "A"
        info = {"premium": round(prem * 100, 1), "a_yield": a["yield"], "h_yield": hs["yield"], "better": better}
        hs["ah"] = dict(info, other=a["name"])
        a["ah"] = dict(info, other=hs["name"])


# ======================= 邮件 =======================
def mail_html(data):
    hits = [s for s in data["stocks"] if s["alerts"]]
    hits.sort(key=lambda s: (not s["star"], s["yzone"] != "买入区", -(s["ypct"] or 0)))
    url = os.getenv("PAGE_URL", "")
    unit = {"CNY": "¥", "HKD": "HK$", "SGD": "S$"}
    h = [f"<h3>三地红利看板 · {data['date']}</h3>"]
    if hits:
        h.append(f"<p>今日 <b>{len(hits)}</b> 只出现信号：</p><ul>")
        for s in hits:
            yl = "-" if s["yield"] is None else f"{s['yield']:.2f}%"
            h.append(f"<li><b>{'★ ' if s['star'] else ''}{s['name']} {s['code']}</b>（{s['bucket']}） "
                     f"{unit[s['qccy']]}{s['price']:.2f}（{s['chg']:+.2f}%），税后股息率 {yl}<br>{'；'.join(s['alerts'])}</li>")
        h.append("</ul>")
    else:
        h.append("<p>今日无提醒信号。</p>")
    if url:
        h.append(f"<p><a href='{url}'>打开红利看板 →</a></p>")
    h.append("<p style='color:#888;font-size:12px'>仅为计划提醒，不构成投资建议。</p>")
    return "\n".join(h), len(hits)


def send_mail(subject, html):
    user, pwd = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    if not (user and pwd):
        print("[未配置邮箱，不发邮件]")
        return
    to = os.getenv("MAIL_TO") or user
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
    print("邮件已发送 →", to)


# ======================= 主流程 =======================
def main():
    cfg = yaml.safe_load((HERE / "watchlist.yaml").read_text(encoding="utf-8"))
    fx = fetch_fx(cfg)
    stocks, errors = [], []
    for s in cfg["stocks"]:
        try:
            stocks.append(build_stock(s, cfg, fx))
            print("ok", s["market"], s["code"], s["name"])
            if s["market"] != "SG":
                time.sleep(0.5)          # 放慢一点，减少被行情接口限流
        except Exception as e:
            errors.append(f"{s['name']}({s['code']})：{str(e)[:300]}")
            print("失败", s["code"], repr(e))
    if not stocks:
        raise SystemExit("全部获取失败")
    add_ah(stocks, fx)

    today = date.today().isoformat()
    latest = max(s["date"] for s in stocks)
    data = {
        "updated": datetime.now(SGT).strftime("%Y-%m-%d %H:%M"),
        "date": latest, "fx": {k: round(v, 5) for k, v in fx.items()},
        "defaults": cfg["defaults"], "targets": cfg.get("targets", {}), "limits": cfg.get("limits", {}), "floor_net": cfg["defaults"].get("floor_net", 4.5),
        "stocks": stocks, "errors": errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"已写入 {OUT}（{len(stocks)} 只，失败 {len(errors)}）")

    if not any(s["date"] == today for s in stocks) and not os.getenv("FORCE"):
        print("今天各市场都没有新数据（休市），不发邮件。")
        return
    html, n = mail_html(data)
    if errors:
        html += "<p style='color:#c00'>获取失败：" + "；".join(x.split("：")[0] for x in errors) + "</p>"
    if n or os.getenv("ALWAYS_SEND"):
        send_mail(f"【红利提醒】{latest} {n} 只有信号" if n else f"【红利日报】{latest} 无信号", html)


if __name__ == "__main__":
    main()
