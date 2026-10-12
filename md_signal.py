#!/usr/bin/env python3
"""
MD Signal Bot — quét chỉ báo MD Signal (SMA 10/15 + bộ lọc) cho danh sách mã,
báo tín hiệu qua Telegram và cập nhật trang app theo dõi (thư mục docs/).

Chạy tự động trên GitHub Actions. Chỉ dùng thư viện chuẩn của Python.

Biến môi trường:
  TELEGRAM_TOKEN   token bot Telegram (lấy từ @BotFather)  — bắt buộc để gửi tin
  MODE             "scan" (mặc định) hoặc "test" (gửi bảng tổng hợp để kiểm tra)
  GITHUB_REPOSITORY  owner/repo (GitHub tự đặt) — dùng để tạo link tới trang app
"""
import base64
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
DOCS = os.path.join(ROOT, "docs")
DATA_PATH = os.path.join(DOCS, "data.json")
HIST_DIR = os.path.join(DOCS, "h")
INFO_PATH = os.path.join(DOCS, "info.json")
AI_PATH = os.path.join(DOCS, "ai_scores.json")
BARS = {}
APP_TEMPLATE = os.path.join(ROOT, "app.html")

VN_TZ = dt.timezone(dt.timedelta(hours=7))
HISTORY_DAYS = 1900         # khoảng 5 năm dữ liệu ngày
VN_CLOSE_MIN = 15 * 60      # sau 15:00 giờ VN coi như nến ngày đã đóng

DEFAULT_CONFIG = {
    "symbols": [
        "TCB", "SSI", "DBC", "CII", "DXG", "FPT", "VIX", "GEX", "PLX", "GVR",
        "DGW", "MWG", "MSN", "VCB", "BID", "HDC", "TPB", "BTCUSDT", "ETHUSDT",
    ],
    "breakout_len": 55,   # MUA khi đóng cửa vượt đỉnh N phiên
    "exit_len": 20,       # BÁN khi đóng cửa thủng đáy N phiên
    "ma_len": 200,        # chỉ mua khi giá trên MA này (xu hướng lớn tăng)
    "stop_atr": 3.0,      # cắt lỗ = giá − ATR × hệ số
    "be_atr": 2.0,        # lời được ATR × hệ số thì dời stop về giá mua
    "sw_ma": 20,          # Lướt sóng: MUA khi giá cắt lên MA này, BÁN khi đóng cửa dưới
    "sw_vol": 1.5,        # Lướt sóng: khối lượng phải lớn hơn TB20 × hệ số
    "sw_stop_atr": 3.0,   # Lướt sóng: cắt lỗ = giá − ATR × hệ số
    "usd_vnd": 26000,     # tỷ giá quy đổi crypto (USD) sang VND cho tài khoản ảo
    "paper": None,        # tài khoản giao dịch ảo (app tự ghi)
    "telegram_chat_id": None,
}



# ───────────────────────────── Tiện ích ─────────────────────────────
def log(*a):
    print(*a, flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj, pretty=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2 if pretty else None,
                  separators=None if pretty else (",", ":"))
        f.write("\n")


def http_json(url, data=None, headers=None, timeout=25):
    h = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def is_crypto(sym):
    return sym.upper().endswith(("USDT", "USDC", "BUSD"))


def fmt_px(x, crypto=False):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if crypto:
        return f"{x:,.2f}" if x >= 1 else f"{x:,.4f}"
    return f"{x:,.0f}"


# ───────────────────────────── Dữ liệu giá ─────────────────────────────
def _bars_from_arrays(ts, o, h, l, c, v, tz):
    bars = []
    for i in range(len(c)):
        t = int(float(ts[i]))
        if t > 10**11:  # mili-giây
            t //= 1000
        d = dt.datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d")
        bars.append({"d": d, "o": float(o[i]), "h": float(h[i]), "l": float(l[i]),
                     "c": float(c[i]), "v": float(v[i] or 0)})
    return bars


def vn_dnse(sym):
    to = int(time.time()) + 86400
    frm = to - HISTORY_DAYS * 86400
    url = ("https://services.entrade.com.vn/chart-api/v2/ohlcs/stock?"
           f"from={frm}&to={to}&symbol={sym}&resolution=1D")
    j = http_json(url)
    return _bars_from_arrays(j["t"], j["o"], j["h"], j["l"], j["c"], j["v"], VN_TZ)


def vn_vci(sym):
    url = "https://trading.vietcap.com.vn/api/chart/OHLCChart/gap-chart"
    body = {"timeFrame": "ONE_DAY", "symbols": [sym],
            "to": int(time.time()) + 86400, "countBack": 1300}
    j = http_json(url, data=body, headers={
        "Referer": "https://trading.vietcap.com.vn/",
        "Origin": "https://trading.vietcap.com.vn"})
    x = j[0] if isinstance(j, list) else j
    return _bars_from_arrays(x["t"], x["o"], x["h"], x["l"], x["c"], x["v"], VN_TZ)


def vn_tcbs(sym):
    url = ("https://apipubaws.tcbs.com.vn/stock-insight/v2/stock/bars-long-term?"
           f"ticker={sym}&type=stock&resolution=D&to={int(time.time()) + 86400}&countBack=1300")
    j = http_json(url)
    bars = []
    for x in j.get("data", []):
        bars.append({"d": str(x["tradingDate"])[:10], "o": float(x["open"]),
                     "h": float(x["high"]), "l": float(x["low"]),
                     "c": float(x["close"]), "v": float(x.get("volume") or 0)})
    return bars


def crypto_binance(sym):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1d&limit=1000"
    j = http_json(url)
    bars = []
    for k in j:
        d = dt.datetime.fromtimestamp(k[0] / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        bars.append({"d": d, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                     "c": float(k[4]), "v": float(k[5])})
    return bars


def crypto_okx(sym):
    base = sym.upper()
    for q in ("USDT", "USDC"):
        if base.endswith(q):
            inst = base[: -len(q)] + "-" + q
            break
    bars, after = [], ""
    for _ in range(18):  # mỗi lần 100 nến
        url = f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar=1Dutc&limit=100{after}"
        j = http_json(url)
        rows = j.get("data", [])
        if not rows:
            break
        for k in rows:
            if len(k) > 8 and k[8] == "0":
                continue  # nến chưa đóng
            d = dt.datetime.fromtimestamp(int(k[0]) / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
            bars.append({"d": d, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                         "c": float(k[4]), "v": float(k[5])})
        after = f"&after={rows[-1][0]}"
        if len(bars) >= 1700:
            break
    return bars


def clean_bars(bars, crypto):
    seen, out = set(), []
    for b in sorted(bars, key=lambda x: x["d"]):
        if b["d"] in seen or not b["c"]:
            continue
        seen.add(b["d"])
        out.append(b)
    if not crypto and out:
        # Một số nguồn trả giá theo nghìn đồng (32.35) → đổi về đồng (32,350)
        closes = sorted(b["c"] for b in out[-60:])
        if closes[len(closes) // 2] < 500:
            for b in out:
                for k in ("o", "h", "l", "c"):
                    b[k] = round(b[k] * 1000, 2)
    # Bỏ nến chưa đóng
    now_vn = dt.datetime.now(VN_TZ)
    if out:
        if crypto:
            if out[-1]["d"] >= dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"):
                out.pop()
        elif out[-1]["d"] == now_vn.strftime("%Y-%m-%d") and \
                now_vn.hour * 60 + now_vn.minute < VN_CLOSE_MIN:
            out.pop()
    return out


def fetch_bars(sym):
    crypto = is_crypto(sym)
    sources = [("Binance", crypto_binance), ("OKX", crypto_okx)] if crypto else \
              [("DNSE", vn_dnse), ("VCI", vn_vci), ("TCBS", vn_tcbs)]
    errors = []
    for name, fn in sources:
        try:
            bars = clean_bars(fn(sym), crypto)
            if len(bars) < 60:
                raise ValueError(f"chỉ có {len(bars)} nến")
            return bars, name
        except Exception as e:  # thử nguồn tiếp theo
            errors.append(f"{name}: {str(e)[:120]}")
    raise RuntimeError(" | ".join(errors))


# ───────────────────────────── Chiến lược ─────────────────────────────
# Chiến lược "Phá đỉnh xu hướng" (đã kiểm thử trên 5 năm dữ liệu thật):
#   MUA  : giá đóng cửa vượt đỉnh cao nhất 55 phiên trước đó VÀ nằm trên MA200
#   BÁN  : giá đóng cửa thủng đáy thấp nhất 20 phiên trước đó
#   CẮT LỖ: giá chạm (giá mua − 3 × ATR14)
#   DỜI STOP: khi giá đã lên được (giá mua + 2 × ATR14) → dời điểm cắt lỗ về giá mua (hòa vốn)
# Khớp lệnh ở giá MỞ CỬA phiên sau ngày có tín hiệu. Cổ phiếu VN không bán trước T+3.
# Phí: VN 0.15% mua, 0.15% + 0.1% thuế khi bán; crypto 0.1% mỗi chiều.
EV_BUY, EV_SELL, EV_STOP, EV_BE = 1, -1, -2, 3


def rma(x, n):
    """Wilder's moving average giống ta.rma của Pine (mở đầu bằng SMA)."""
    out, prev, seed = [None] * len(x), None, []
    for i, v in enumerate(x):
        if v is None:
            continue
        if prev is None:
            seed.append(v)
            if len(seed) == n:
                prev = sum(seed) / n
                out[i] = prev
        else:
            prev = (v + (n - 1) * prev) / n
            out[i] = prev
    return out


def sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def run_strategy(bars, cfg, crypto):
    o = [b["o"] for b in bars]; h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]; c = [b["c"] for b in bars]
    n = len(bars)
    B, E, M = cfg["breakout_len"], cfg["exit_len"], cfg["ma_len"]
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    atr = rma(tr, 14)
    ma = sma(c, M)
    hh = [None if i < B else max(h[i - B:i]) for i in range(n)]
    ll = [None if i < E else min(l[i - E:i]) for i in range(n)]
    fee_b = 0.001 if crypto else 0.0015
    fee_s = 0.001 if crypto else 0.0025
    tmin = 0 if crypto else 3

    events = [0] * n
    trades, pos, pending = [], None, None
    first = max(M, B, 15)
    for i in range(first, n):
        if pending == "buy" and pos is None:
            pos = {"i": i, "sig_d": bars[i - 1]["d"], "raw": o[i], "cost": o[i] * (1 + fee_b),
                   "stop": c[i - 1] - cfg["stop_atr"] * atr[i - 1], "be": False}
        elif pending == "sell" and pos is not None:
            trades.append(_close(pos, o[i], fee_s, i, bars, "Bán"))
            pos = None
        pending = None
        if pos is not None:
            held = i - pos["i"]
            if held >= tmin and l[i] <= pos["stop"]:
                t = _close(pos, min(o[i], pos["stop"]), fee_s, i, bars, "Cắt lỗ")
                if pos["be"] and t["ret"] > -1:
                    t["why"] = "Hòa vốn"
                trades.append(t)
                pos = None
                events[i] = EV_STOP
                continue
            if not pos["be"] and atr[i] and h[i] >= pos["raw"] + cfg["be_atr"] * atr[i]:
                pos["be"] = True
                pos["stop"] = max(pos["stop"], pos["raw"])
                events[i] = EV_BE
            if held + 1 >= tmin and ll[i] is not None and c[i] < ll[i]:
                pending = "sell"
                events[i] = EV_SELL
        elif hh[i] is not None and ma[i] is not None and atr[i] and c[i] > hh[i] and c[i] > ma[i]:
            pending = "buy"
            events[i] = EV_BUY

    last = n - 1
    nxt_hh = max(h[n - B:]) if n >= B else None    # đỉnh 55 phiên áp dụng cho phiên tới
    nxt_ll = min(l[n - E:]) if n >= E else None
    state = {"pending": pending, "pos": pos, "atr": atr[last], "ma": ma[last],
             "buy_level": nxt_hh, "sell_level": nxt_ll,
             "pending_stop": (c[last] - cfg["stop_atr"] * atr[last]) if pending == "buy" else None}
    return events, trades, state


def run_swing(bars, cfg, crypto):
    """Lướt sóng: MUA khi giá đóng cửa cắt lên MA20, khối lượng > 1,5 lần TB20 và giá trên MA200.
    BÁN khi giá đóng cửa dưới MA20. Cắt lỗ = giá − 3 ATR. Khớp mở cửa phiên sau, T+3."""
    o = [b["o"] for b in bars]; h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]; c = [b["c"] for b in bars]; v = [b["v"] for b in bars]
    n = len(bars)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    atr = rma(tr, 14)
    m20 = sma(c, cfg["sw_ma"])
    vma = sma(v, 20)
    ma = sma(c, cfg["ma_len"])
    fee_b = 0.001 if crypto else 0.0015
    fee_s = 0.001 if crypto else 0.0025
    tmin = 0 if crypto else 3
    events = [0] * n
    trades, pos, pending = [], None, None
    first = max(cfg["ma_len"], cfg["sw_ma"], 21)
    for i in range(first, n):
        if pending == "buy" and pos is None:
            pos = {"i": i, "sig_d": bars[i - 1]["d"], "raw": o[i], "cost": o[i] * (1 + fee_b),
                   "stop": c[i - 1] - cfg["sw_stop_atr"] * atr[i - 1], "be": False}
        elif pending == "sell" and pos is not None:
            trades.append(_close(pos, o[i], fee_s, i, bars, "Bán"))
            pos = None
        pending = None
        if pos is not None:
            held = i - pos["i"]
            if held >= tmin and l[i] <= pos["stop"]:
                trades.append(_close(pos, min(o[i], pos["stop"]), fee_s, i, bars, "Cắt lỗ"))
                pos = None
                events[i] = EV_STOP
                continue
            if held + 1 >= tmin and c[i] < m20[i]:
                pending = "sell"
                events[i] = EV_SELL
        elif (m20[i - 1] is not None and ma[i] is not None and vma[i] and atr[i]
              and c[i] > m20[i] and c[i - 1] <= m20[i - 1] and v[i] > cfg["sw_vol"] * vma[i] and c[i] > ma[i]):
            pending = "buy"
            events[i] = EV_BUY
    last = n - 1
    state = {"pending": pending, "pos": pos, "atr": atr[last], "ma": ma[last],
             "buy_level": m20[last], "sell_level": m20[last],
             "vol_need": cfg["sw_vol"] * vma[last] if vma[last] else None,
             "pending_stop": (c[last] - cfg["sw_stop_atr"] * atr[last]) if pending == "buy" else None}
    return events, trades, state


def _close(pos, px, fee_s, i, bars, why):
    return {"sig_d": pos["sig_d"], "in_d": bars[pos["i"]]["d"], "in": pos["raw"],
            "out_d": bars[i]["d"], "out": px, "why": why, "days": i - pos["i"],
            "ret": (px * (1 - fee_s) / pos["cost"] - 1) * 100}


def stats(trades, bars):
    if not trades:
        return {"n": 0}
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    loss = [r for r in rets if r <= 0]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r / 100
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    i0 = next(k for k, b in enumerate(bars) if b["d"] == trades[0]["in_d"])
    return {
        "n": len(rets), "wins": len(wins),
        "win_rate": round(100 * len(wins) / len(rets)),
        "avg_win": round(sum(wins) / len(wins), 1) if wins else 0,
        "avg_loss": round(sum(loss) / len(loss), 1) if loss else 0,
        "best": round(max(rets), 1), "worst": round(min(rets), 1),
        "total": round((eq - 1) * 100, 1), "mdd": round(mdd * 100, 1),
        "hold_all": round((bars[-1]["c"] / bars[i0]["o"] - 1) * 100, 1),
        "since": trades[0]["in_d"],
    }


def save_history(sym, bars):
    """Lưu lịch sử giá ngày (dùng cho biểu đồ nến trong app)."""
    os.makedirs(HIST_DIR, exist_ok=True)
    h = {"d": [b["d"] for b in bars]}
    for k in ("o", "h", "l", "c"):
        h[k] = [round(b[k], 4) for b in bars]
    h["v"] = [round(b["v"]) for b in bars]
    with open(os.path.join(HIST_DIR, f"{sym}.json"), "w", encoding="utf-8") as f:
        json.dump(h, f, separators=(",", ":"))


def r2(x, crypto):
    if x is None:
        return None
    return round(x, 4 if crypto and x < 1 else 2)


def strat_block(bars, events, trades, stt, crypto):
    i = len(bars) - 1
    close = bars[i]["c"]
    pos = stt["pos"]
    if stt["pending"] == "buy":
        status = "buy"
    elif pos is not None and stt["pending"] == "sell":
        status = "sell"
    elif pos is not None:
        status = "hold"
    elif events[i] == EV_STOP:
        status = "stopped"
    else:
        status = "wait"
    blk = {
        "status": status, "event": events[i],
        "buy_level": r2(stt["buy_level"], crypto),
        "sell_level": r2(stt["sell_level"], crypto),
        "to_buy": round((stt["buy_level"] / close - 1) * 100, 1) if stt["buy_level"] else None,
        "stop": r2(pos["stop"] if pos else stt["pending_stop"], crypto),
        "be": bool(pos and pos["be"]),
        "pos": None,
        "stats": stats(trades, bars),
        "trades": [{k: (r2(t[k], crypto) if k in ("in", "out") else round(t[k], 2) if k == "ret" else t[k])
                    for k in ("sig_d", "in_d", "in", "out_d", "out", "ret", "why", "days")} for t in trades],
    }
    if stt.get("vol_need") is not None:
        blk["vol_need"] = round(stt["vol_need"])
    if pos is not None:
        blk["pos"] = {"sig_d": pos["sig_d"], "in_d": bars[pos["i"]]["d"], "in": r2(pos["raw"], crypto),
                      "pnl": round((close / pos["raw"] - 1) * 100, 1), "days": i - pos["i"] + 1}
    return blk


def analyse(sym, cfg):
    bars, src = fetch_bars(sym)
    save_history(sym, bars)
    BARS[sym] = bars
    crypto = is_crypto(sym)
    events, trades, stt = run_strategy(bars, cfg, crypto)
    i = len(bars) - 1
    close = bars[i]["c"]
    res = {
        "sym": sym, "crypto": crypto, "source": src,
        "date": bars[i]["d"], "close": r2(close, crypto),
        "chg": round((close / bars[i - 1]["c"] - 1) * 100, 2),
        "uptrend": bool(stt["ma"] and close > stt["ma"]),
        "ma": r2(stt["ma"], crypto),
    }
    res.update(strat_block(bars, events, trades, stt, crypto))           # Phá đỉnh (mặc định)
    res["swing"] = strat_block(bars, *run_swing(bars, cfg, crypto), crypto)  # Lướt sóng
    return res


# ───────────────────────────── Telegram ─────────────────────────────
def tg_call(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=25) as r:
        return json.loads(r.read().decode())


def tg_find_chat(token):
    """Lấy chat id từ tin nhắn gần nhất người dùng gửi cho bot (ví dụ /start)."""
    j = tg_call(token, "getUpdates", {"limit": 100})
    for u in reversed(j.get("result", [])):
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post")
        if msg and "chat" in msg:
            return msg["chat"]["id"]
    return None


def tg_send(token, chat_id, text):
    # Telegram giới hạn 4096 ký tự / tin
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for ch in chunks:
        tg_call(token, "sendMessage", {"chat_id": chat_id, "text": ch, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"})


def app_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner.lower()}.github.io/"
    return f"https://{owner.lower()}.github.io/{name}/"


def vn_date(d):
    y, m, dd = d.split("-")
    return f"{dd}/{m}/{y}"


def event_line(r, e):
    cr = r["crypto"]
    sym, px = r["sym"], fmt_px(r["close"], cr)
    if r.get("kind") == "sw":
        if e == EV_BUY:
            return (f"🌊 <b>LƯỚT SÓNG · MUA {sym}</b> · giá {px}\n"
                    f"      Giá cắt lên MA{r_cfg('sw_ma')} ({fmt_px(r['buy_level'], cr)}) kèm khối lượng lớn. 👉 Mua đầu phiên tới\n"
                    f"      Bán khi đóng cửa dưới MA{r_cfg('sw_ma')} · cắt lỗ nếu chạm {fmt_px(r['stop'], cr)}") + extra_lines(r)
        if e == EV_SELL:
            line = f"🌊 <b>LƯỚT SÓNG · BÁN {sym}</b> · giá {px}\n      Đóng cửa dưới MA{r_cfg('sw_ma')}. 👉 Bán đầu phiên tới"
            if r.get("pos"):
                line += f"\n      Mua {vn_date(r['pos']['in_d'])} giá {fmt_px(r['pos']['in'], cr)} → đang {r['pos']['pnl']:+.1f}%"
            return line
        if e == EV_STOP and r["trades"]:
            t = r["trades"][-1]
            return (f"🌊 <b>LƯỚT SÓNG · CẮT LỖ {sym}</b>\n      Mua {vn_date(t['in_d'])} giá {fmt_px(t['in'], cr)} → "
                    f"bán {fmt_px(t['out'], cr)} ({t['ret']:+.1f}%). 👉 Bán ngay nếu chưa bán")
        return ""
    if e == EV_BUY:
        line = (f"🟢 <b>PHÁ ĐỈNH · MUA {sym}</b> · giá {px}\n"
                f"      Vượt đỉnh {r_cfg('breakout_len')} phiên. 👉 Mua đầu phiên tới\n"
                f"      Cắt lỗ nếu giá chạm {fmt_px(r['stop'], cr)}")
        return line + extra_lines(r)
    if e == EV_SELL:
        line = f"🔴 <b>PHÁ ĐỈNH · BÁN {sym}</b> · giá {px}\n      Thủng đáy {r_cfg('exit_len')} phiên. 👉 Bán đầu phiên tới"
        if r.get("pos"):
            line += f"\n      Mua {vn_date(r['pos']['in_d'])} giá {fmt_px(r['pos']['in'], cr)} → đang {r['pos']['pnl']:+.1f}%"
        return line
    if e == EV_STOP:
        line = f"⛔ <b>PHÁ ĐỈNH · CẮT LỖ {sym}</b> · giá chạm điểm dừng"
        if r["trades"]:
            t = r["trades"][-1]
            line += (f"\n      Mua {vn_date(t['in_d'])} giá {fmt_px(t['in'], cr)} → bán {fmt_px(t['out'], cr)}"
                     f" ({t['ret']:+.1f}%). 👉 Bán ngay nếu chưa bán")
        return line
    if e == EV_BE:
        return (f"🛡 <b>PHÁ ĐỈNH · DỜI STOP {sym}</b> · giá {px}\n"
                f"      Đã lời đủ: dời điểm cắt lỗ lên giá mua {fmt_px(r['stop'], cr)} → lệnh này không còn lỗ")
    return ""


_CFG = {}
_INFO = {}


def extra_lines(r):
    """Điểm AI, sức khỏe tài chính và tin gần nhất kèm theo tín hiệu MUA."""
    out = []
    if r.get("ai") is not None:
        a = r["ai"]
        out.append(f"      🤖 AI chấm {a}/100 ({'cao' if a >= 60 else 'trung bình' if a >= 40 else 'thấp'}, chỉ để tham khảo)")
    inf = _INFO.get(r["sym"], {})
    f = inf.get("fin")
    if f:
        g = f.get("np_g")
        out.append(f"      📊 BCTC {f['q'].replace('-', ' ')}: {f['grade']}" + (f", LN {g*100:+.0f}% cùng kỳ" if g is not None else ""))
    n = (inf.get("news") or {})
    if n.get("sent14") is not None:
        s = n["sent14"]
        out.append(f"      📰 Tin 2 tuần qua: {'tích cực' if s > 0 else 'tiêu cực' if s < 0 else 'trung tính'} ({s:+d})")
    return ("\n" + "\n".join(out)) if out else ""


def r_cfg(k):
    return _CFG.get(k, DEFAULT_CONFIG.get(k))


STATUS_LINE = {"hold": "🟢", "buy": "🆕", "sell": "🔴", "stopped": "⛔", "wait": "⏳"}


def summary_text(results):
    lines = []
    for r in results:
        if "error" in r:
            lines.append(f"❓ {r['sym']}: lỗi dữ liệu")
            continue
        cr = r["crypto"]
        st = r["status"]
        if st == "hold":
            tail = f"đang giữ {r['pos']['pnl']:+.1f}%"
        elif st == "buy":
            tail = "MUA phiên tới"
        elif st == "sell":
            tail = "BÁN phiên tới"
        elif not r["uptrend"]:
            tail = "xu hướng giảm, đứng ngoài"
        else:
            tail = f"chờ mua, còn {r['to_buy']:.1f}% tới điểm mua"
        icon = "⚪" if st == "wait" and not r["uptrend"] else STATUS_LINE.get(st, "⏳")
        lines.append(f"{icon} <b>{r['sym']}</b> {fmt_px(r['close'], cr)} · {tail}")
    sw = [r for r in results if "error" not in r and r["swing"]["status"] in ("hold", "sell", "buy")]
    if sw:
        lines.append("\n🌊 <b>Lướt sóng</b>: " + ", ".join(
            f"{r['sym']} " + (f"{r['swing']['pos']['pnl']:+.1f}%" if r["swing"]["pos"] else "MUA phiên tới") for r in sw))
    return "\n".join(lines)


# ───────────────────────────── Trang app ─────────────────────────────
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="112" fill="#0E1A14"/>
<path d="M88 340 C150 340 170 250 230 250 S320 330 380 250 S424 170 440 160" fill="none" stroke="#2ED644" stroke-width="34" stroke-linecap="round"/>
<path d="M88 380 C160 380 190 300 250 300 S340 350 424 290" fill="none" stroke="#F04B4B" stroke-width="22" stroke-linecap="round" opacity=".9"/>
<text x="256" y="150" text-anchor="middle" font-family="Arial,Helvetica,sans-serif" font-weight="700" font-size="128" fill="#F3F1EA">MD</text>
</svg>"""

MANIFEST = {
    "name": "MD Signal – Theo dõi tín hiệu",
    "short_name": "MD Signal",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "background_color": "#0E1A14",
    "theme_color": "#0E1A14",
    "lang": "vi",
    "icons": [
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"},
    ],
}

SERVICE_WORKER = """const CACHE = 'mdsignal-v4';
const SHELL = ['./', './index.html', './manifest.webmanifest', './icon-192.png', './icon-512.png', './icon.svg'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {})); self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))); self.clients.claim(); });
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return;
  // Mạng trước, mất mạng thì dùng bản đã lưu
  e.respondWith(fetch(e.request).then(r => { const cp = r.clone(); caches.open(CACHE).then(c => c.put(e.request, cp)); return r; })
    .catch(() => caches.match(e.request, {ignoreSearch: true})));
});
"""


def png_icon(size):
    """Vẽ icon PNG không cần thư viện ngoài (nền tối + 2 đường xanh/đỏ)."""
    import struct
    import zlib
    W = H = size
    bg, green, red, light = (14, 26, 20), (46, 214, 68), (240, 75, 75), (243, 241, 234)
    px = [[bg for _ in range(W)] for _ in range(H)]
    rad = int(W * 0.22)

    def inside_round(x, y):
        cx = min(max(x, rad), W - 1 - rad)
        cy = min(max(y, rad), H - 1 - rad)
        return (x - cx) ** 2 + (y - cy) ** 2 <= rad * rad

    def stroke(fn, color, width):
        steps = W * 4
        r = width / 2
        for k in range(steps + 1):
            t = k / steps
            x, y = fn(t)
            for yy in range(int(y - r), int(y + r) + 1):
                for xx in range(int(x - r), int(x + r) + 1):
                    if 0 <= xx < W and 0 <= yy < H and (xx - x) ** 2 + (yy - y) ** 2 <= r * r:
                        px[yy][xx] = color

    s = W / 512
    stroke(lambda t: (s * (90 + 340 * t), s * (330 - 120 * t - 60 * math.sin(t * 9.0))), green, 34 * s)
    stroke(lambda t: (s * (90 + 330 * t), s * (390 - 70 * t - 30 * math.sin(t * 7.0 + 1))), red, 22 * s)
    # Chữ "M" đơn giản: hai chân dọc + hai nét chéo gặp nhau ở giữa
    for yy in range(int(80 * s), int(200 * s)):
        for xx in list(range(int(186 * s), int(216 * s))) + list(range(int(296 * s), int(326 * s))):
            px[yy][xx] = light
    stroke(lambda t: (s * (201 + 55 * t), s * (88 + 80 * t)), light, 28 * s)
    stroke(lambda t: (s * (311 - 55 * t), s * (88 + 80 * t)), light, 28 * s)

    raw = b""
    for y in range(H):
        row = b"\x00"
        for x in range(W):
            r_, g_, b_ = px[y][x]
            a = 255 if inside_round(x, y) else 0
            row += bytes((r_, g_, b_, a))
        raw += row

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_if_changed(path, content):
    mode = "wb" if isinstance(content, bytes) else "w"
    try:
        with open(path, "rb" if mode == "wb" else "r", **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
            if f.read() == content:
                return
    except FileNotFoundError:
        pass
    with open(path, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
        f.write(content)


def build_site():
    os.makedirs(DOCS, exist_ok=True)
    if os.path.exists(APP_TEMPLATE):
        with open(APP_TEMPLATE, encoding="utf-8") as f:
            write_if_changed(os.path.join(DOCS, "index.html"), f.read())
    write_if_changed(os.path.join(DOCS, "manifest.webmanifest"),
                     json.dumps(MANIFEST, ensure_ascii=False, indent=2) + "\n")
    write_if_changed(os.path.join(DOCS, "sw.js"), SERVICE_WORKER)
    write_if_changed(os.path.join(DOCS, "icon.svg"), ICON_SVG)
    write_if_changed(os.path.join(DOCS, ".nojekyll"), "")
    for sz in (192, 512):
        p = os.path.join(DOCS, f"icon-{sz}.png")
        if not os.path.exists(p):
            with open(p, "wb") as f:
                f.write(png_icon(sz))


# ───────────────────────────── Chương trình chính ─────────────────────────────
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user = load_json(CONFIG_PATH, None)
    if isinstance(user, dict):
        cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    syms = []
    for s in cfg["symbols"]:
        s = str(s).strip().upper()
        if ":" in s:
            s = s.split(":", 1)[1]
        if s and s not in syms:
            syms.append(s)
    cfg["symbols"] = syms[:60]
    if user is None:
        save_json(CONFIG_PATH, cfg)
    return cfg


def get_chat(token, cfg):
    chat_id = cfg.get("telegram_chat_id")
    if not chat_id:
        chat_id = tg_find_chat(token)
        if chat_id:
            user_cfg = load_json(CONFIG_PATH, {}) or {}
            user_cfg["telegram_chat_id"] = chat_id
            save_json(CONFIG_PATH, user_cfg)
            cfg["telegram_chat_id"] = chat_id
    return chat_id


def main():
    mode = (os.environ.get("MODE") or "scan").strip().lower()
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    cfg = load_config()
    _CFG.update(cfg)
    build_site()
    _INFO.update(load_json(INFO_PATH, {}))

    if mode == "news":
        import news_fin
        key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip() or None
        msg, st = news_fin.run(cfg["symbols"], DOCS, key, log)
        chat_id = get_chat(token, cfg) if token else None
        if chat_id and msg:
            link = app_url()
            tg_send(token, chat_id, msg + (f'\n\n📱 <a href="{link}">Mở app MD Signal</a>' if link else ""))
        if chat_id and key and st["ai"] != "ok":
            tg_send(token, chat_id, f"⚠️ <b>MD Signal</b>: AI đọc tin đang lỗi: {st['ai']}")
        n_vn = sum(1 for x in cfg["symbols"] if not is_crypto(x))
        if chat_id and n_vn and len(st["errors"]) >= n_vn:
            tg_send(token, chat_id, "⚠️ <b>MD Signal</b>: không lấy được tin tức/BCTC từ máy chủ GitHub.\n"
                                    f"Ví dụ lỗi: {st['errors'][0]}\nGửi ảnh chụp mục Actions để được hỗ trợ đổi nguồn.")
        return

    prev = load_json(DATA_PATH, {})
    alerted = prev.get("alerted", {}) if isinstance(prev, dict) else {}

    results = []
    for sym in cfg["symbols"]:
        try:
            r = analyse(sym, cfg)
            log(f"✓ {sym:<8} {r['date']} {r['close']:>12} nguồn={r['source']} sự kiện={r['event']}")
        except Exception as e:
            r = {"sym": sym, "crypto": is_crypto(sym), "error": str(e)[:300]}
            log(f"✗ {sym:<8} {r['error']}")
        results.append(r)
        time.sleep(0.3)

    # Điểm AI (chế độ dự thính)
    ai_status = "tắt"
    try:
        import ai_score
        ai_status = ai_score.apply_ai(results, BARS, AI_PATH, log)
    except Exception as e:
        ai_status = f"lỗi: {str(e)[:150]}"
        log("AI lỗi:", e)

    # Sự kiện mới (nến cuối) chưa báo — mỗi chiến lược riêng
    new_events = []
    for r in results:
        if "error" in r:
            continue
        for kind, v in (("bo", r), ("sw", {**r, **r["swing"], "kind": "sw"})):
            if not v["event"]:
                continue
            key = f"{r['date']}|{v['event']}"
            akey = r["sym"] if kind == "bo" else r["sym"] + "|sw"
            if alerted.get(akey) != key:
                new_events.append(v)
                alerted[akey] = key

    ok = [r for r in results if "error" not in r]
    bad = [r for r in results if "error" in r]
    vn_total = [r for r in results if not r["crypto"]]
    vn_bad = [r for r in bad if not r["crypto"]]
    now_vn = dt.datetime.now(VN_TZ)

    # ── Telegram ──
    chat_id = cfg.get("telegram_chat_id")
    tg_status = "chưa cài token"
    if token:
        try:
            chat_id = get_chat(token, cfg)
            if not chat_id:
                tg_status = "chưa tìm thấy chat — hãy nhắn /start cho bot rồi chạy lại"
            else:
                link = app_url()
                link_line = f'\n\n📱 <a href="{link}">Mở app MD Signal</a>' if link else ""
                if mode == "test":
                    text = (f"✅ <b>Bot MD Signal đã kết nối!</b>\n"
                            f"Cập nhật {now_vn:%H:%M %d/%m/%Y}\n\n" + summary_text(results))
                    if bad:
                        text += f"\n\n⚠️ {len(bad)} mã chưa lấy được dữ liệu, xem chi tiết trong app."
                    text += f"\n\n🤖 Điểm AI: {ai_status}"
                    text += f"\n📰 Đọc tin/BCTC: {_INFO.get('_ai', 'chưa chạy lần nào (chạy chế độ news)')}"
                    tg_send(token, chat_id, text + link_line)
                elif new_events:
                    order = {EV_BUY: 0, EV_STOP: 1, EV_SELL: 2, EV_BE: 3}
                    new_events.sort(key=lambda r: (r.get("kind") == "sw", order[r["event"]], r["sym"]))
                    days = sorted({r["date"] for r in new_events})
                    head = "📢 <b>MD Signal – tín hiệu phiên " + ", ".join(vn_date(d) for d in days) + "</b>"
                    body = "\n\n".join(event_line(r, r["event"]) for r in new_events)
                    tg_send(token, chat_id, head + "\n\n" + body +
                            "\n\n<i>Tín hiệu kỹ thuật tham khảo, không phải khuyến nghị đầu tư.</i>" + link_line)
                # Cảnh báo lỗi dữ liệu VN (tối đa 1 lần/ngày)
                today = now_vn.strftime("%Y-%m-%d")
                if vn_total and len(vn_bad) == len(vn_total) and alerted.get("_warn") != today:
                    tg_send(token, chat_id,
                            "⚠️ <b>MD Signal</b>: không lấy được dữ liệu chứng khoán VN từ máy chủ GitHub "
                            "(các nguồn DNSE/VCI/TCBS đều lỗi). Gửi ảnh chụp lỗi trong mục Actions "
                            "để được hỗ trợ đổi nguồn dữ liệu.")
                    alerted["_warn"] = today
                tg_status = "ok"
        except Exception as e:
            tg_status = f"lỗi: {str(e)[:200]}"
            log("Telegram lỗi:", e)
    log("Telegram:", tg_status, "| sự kiện mới:", [f"{r['sym']}:{r.get('kind', 'bo')}:{r['event']}" for r in new_events])

    public_cfg = {k: v for k, v in cfg.items() if k != "telegram_chat_id"}
    save_json(DATA_PATH, {
        "updated": now_vn.isoformat(timespec="minutes"),
        "mode": mode,
        "telegram": tg_status,
        "ai": ai_status,
        "config": public_cfg,
        "results": results,
        "alerted": alerted,
    }, pretty=False)
    log(f"Xong: {len(ok)} mã ok, {len(bad)} lỗi.")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
