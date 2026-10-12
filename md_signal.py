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
APP_TEMPLATE = os.path.join(ROOT, "app.html")

VN_TZ = dt.timezone(dt.timedelta(hours=7))
HISTORY_DAYS = 760          # khoảng 2 năm dữ liệu ngày để tính chỉ báo + thống kê
VN_CLOSE_MIN = 15 * 60      # sau 15:00 giờ VN coi như nến ngày đã đóng

DEFAULT_CONFIG = {
    "symbols": [
        "TCB", "SSI", "DBC", "CII", "DXG", "FPT", "VIX", "GEX", "PLX", "GVR",
        "DGW", "MWG", "MSN", "VCB", "BID", "HDC", "TPB", "BTCUSDT", "ETHUSDT",
    ],
    "sma_short": 10,
    "sma_long": 15,
    "use_trend": True,
    "trend_len": 50,
    "use_vol": True,
    "vol_mult": 1.0,
    "use_adx": True,
    "adx_min": 20,
    "atr_mult": 1.5,
    "rr": 2.0,
    "telegram_chat_id": None,
}

EVENT_TEXT = {1: "MUA", -1: "BÁN", -2: "CHẠM STOP", 2: "ĐẠT MỤC TIÊU"}
EVENT_ICON = {1: "🟢", -1: "🔴", -2: "⛔", 2: "🎯"}


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
            "to": int(time.time()) + 86400, "countBack": 520}
    j = http_json(url, data=body, headers={
        "Referer": "https://trading.vietcap.com.vn/",
        "Origin": "https://trading.vietcap.com.vn"})
    x = j[0] if isinstance(j, list) else j
    return _bars_from_arrays(x["t"], x["o"], x["h"], x["l"], x["c"], x["v"], VN_TZ)


def vn_tcbs(sym):
    url = ("https://apipubaws.tcbs.com.vn/stock-insight/v2/stock/bars-long-term?"
           f"ticker={sym}&type=stock&resolution=D&to={int(time.time()) + 86400}&countBack=520")
    j = http_json(url)
    bars = []
    for x in j.get("data", []):
        bars.append({"d": str(x["tradingDate"])[:10], "o": float(x["open"]),
                     "h": float(x["high"]), "l": float(x["low"]),
                     "c": float(x["close"]), "v": float(x.get("volume") or 0)})
    return bars


def crypto_binance(sym):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1d&limit=760"
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
    for _ in range(8):  # mỗi lần 100 nến
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
        if len(bars) >= 700:
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


# ───────────────────────────── Chỉ báo ─────────────────────────────
def sma(x, n):
    out, s, q = [None] * len(x), 0.0, []
    for i, v in enumerate(x):
        if v is None:
            q, s = [], 0.0
            continue
        q.append(v)
        s += v
        if len(q) > n:
            s -= q.pop(0)
        if len(q) == n:
            out[i] = s / n
    return out


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


def indicators(bars, cfg):
    c = [b["c"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    v = [b["v"] for b in bars]
    n = len(bars)
    tr = [None] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    pdm, mdm = [None], [None]
    for i in range(1, n):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    atr = rma(tr, 14)
    p_s, m_s = rma(pdm, 14), rma(mdm, 14)
    dx = [None] * n
    for i in range(n):
        if atr[i] and p_s[i] is not None and m_s[i] is not None:
            p, m = 100 * p_s[i] / atr[i], 100 * m_s[i] / atr[i]
            dx[i] = abs(p - m) / (p + m if p + m else 1)
    adx = [None if x is None else 100 * x for x in rma(dx, 14)]
    return {
        "c": c, "v": v,
        "s": sma(c, cfg["sma_short"]), "L": sma(c, cfg["sma_long"]),
        "t": sma(c, cfg["trend_len"]), "vma": sma(v, 20),
        "atr": atr, "adx": adx,
    }


def run_signals(bars, cfg):
    """Mô phỏng y hệt MD Signal Scanner trên TradingView. Trả về sự kiện từng nến + lệnh."""
    ind = indicators(bars, cfg)
    c, s, L = ind["c"], ind["s"], ind["L"]
    n = len(bars)
    st, stp, tgt, tgt_done = 0, None, None, False
    ev = [0] * n
    trades, open_trade = [], None
    for i in range(n):
        ok = i > 0 and None not in (s[i], L[i], s[i - 1], L[i - 1])
        cross_up = ok and s[i] > L[i] and s[i - 1] <= L[i - 1]
        cross_dn = ok and s[i] < L[i] and s[i - 1] >= L[i - 1]
        vol_ok = (not cfg["use_vol"]) or (ind["vma"][i] is not None and
                                          ind["v"][i] > ind["vma"][i] * cfg["vol_mult"])
        adx_ok = (not cfg["use_adx"]) or (ind["adx"][i] is not None and ind["adx"][i] > cfg["adx_min"])
        up_ok = (not cfg["use_trend"]) or (ind["t"][i] is not None and c[i] > ind["t"][i])
        buy = cross_up and vol_ok and adx_ok and up_ok and ind["atr"][i] is not None

        e = 0
        if st == 1:
            if c[i] <= stp:
                e, st = -2, 0
            elif c[i] >= tgt and not tgt_done:
                e, tgt_done = 2, True
        if cross_dn and st == 1:
            e, st = -1, 0
        if st == 0 and open_trade:
            open_trade.update(exit=c[i], exit_d=bars[i]["d"], why=EVENT_TEXT[e],
                              ret=(c[i] / open_trade["entry"] - 1) * 100,
                              days=i - open_trade["i"])
            trades.append(open_trade)
            open_trade = None
        if buy:
            e, st = 1, 1
            stp = c[i] - cfg["atr_mult"] * ind["atr"][i]
            tgt = c[i] + cfg["rr"] * cfg["atr_mult"] * ind["atr"][i]
            tgt_done = False
            open_trade = {"entry": c[i], "entry_d": bars[i]["d"], "i": i,
                          "stop": stp, "target": tgt}
        if st == 0:
            stp = tgt = None
        ev[i] = e
    return ind, ev, trades, open_trade, st, stp, tgt


def stats(trades):
    if not trades:
        return {"n": 0}
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    total = 1.0
    for r in rets:
        total *= 1 + r / 100
    return {
        "n": len(rets),
        "win_rate": round(100 * len(wins) / len(rets), 1),
        "avg": round(sum(rets) / len(rets), 2),
        "best": round(max(rets), 2),
        "worst": round(min(rets), 2),
        "total": round((total - 1) * 100, 1),
        "avg_days": round(sum(t["days"] for t in trades) / len(trades), 1),
        "since": trades[0]["entry_d"],
    }


def r2(x, crypto):
    if x is None:
        return None
    return round(x, 4 if crypto and x < 1 else 2)


def analyse(sym, cfg):
    bars, src = fetch_bars(sym)
    crypto = is_crypto(sym)
    ind, ev, trades, open_trade, st, stp, tgt = run_signals(bars, cfg)
    i = len(bars) - 1
    events = [{"d": bars[k]["d"], "type": ev[k], "price": r2(bars[k]["c"], crypto)}
              for k in range(len(bars)) if ev[k]]
    tail = 90
    res = {
        "sym": sym, "crypto": crypto, "source": src,
        "date": bars[i]["d"],
        "close": r2(bars[i]["c"], crypto),
        "chg": round((bars[i]["c"] / bars[i - 1]["c"] - 1) * 100, 2),
        "trend_up": bool(ind["s"][i] is not None and ind["L"][i] is not None and ind["s"][i] > ind["L"][i]),
        "sma_s": r2(ind["s"][i], crypto), "sma_l": r2(ind["L"][i], crypto),
        "adx": None if ind["adx"][i] is None else round(ind["adx"][i], 1),
        "state": st, "stop": r2(stp, crypto), "target": r2(tgt, crypto),
        "event": ev[i],
        "events": events[-8:][::-1],
        "open": None,
        "stats": stats(trades),
        "recent_trades": [{k: (r2(t[k], crypto) if k in ("entry", "exit") else
                               round(t[k], 2) if k == "ret" else t[k])
                           for k in ("entry_d", "entry", "exit_d", "exit", "ret", "why")}
                          for t in trades[-5:][::-1]],
        "chart": {
            "d": [b["d"] for b in bars[-tail:]],
            "c": [r2(x, crypto) for x in ind["c"][-tail:]],
            "s": [r2(x, crypto) for x in ind["s"][-tail:]],
            "l": [r2(x, crypto) for x in ind["L"][-tail:]],
        },
    }
    if open_trade:
        res["open"] = {"entry": r2(open_trade["entry"], crypto), "entry_d": open_trade["entry_d"],
                       "pnl": round((bars[i]["c"] / open_trade["entry"] - 1) * 100, 2)}
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
    line = f"{EVENT_ICON[e]} <b>{EVENT_TEXT[e]} {r['sym']}</b> · giá {fmt_px(r['close'], cr)}"
    if e == 1:
        line += f"\n      Stop {fmt_px(r['stop'], cr)} · Mục tiêu {fmt_px(r['target'], cr)}"
    elif e == 2:
        line += "\n      Cân nhắc chốt lời một phần / dời stop"
    elif e in (-1, -2) and r["recent_trades"]:
        t = r["recent_trades"][0]
        line += f"\n      Lệnh mua {vn_date(t['entry_d'])} giá {fmt_px(t['entry'], cr)} → {t['ret']:+.1f}%"
    return line


def summary_text(results):
    lines = []
    for r in results:
        if "error" in r:
            lines.append(f"❓ {r['sym']}: lỗi dữ liệu")
            continue
        trend = "🟩" if r["trend_up"] else "🟥"
        hold = " · <b>đang giữ</b>" if r["state"] == 1 else ""
        lines.append(f"{trend} <b>{r['sym']}</b> {fmt_px(r['close'], r['crypto'])} ({r['chg']:+.1f}%){hold}")
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

SERVICE_WORKER = """const CACHE = 'mdsignal-v1';
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


def main():
    mode = (os.environ.get("MODE") or "scan").strip().lower()
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    cfg = load_config()
    build_site()

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

    # Sự kiện mới (nến cuối) chưa báo
    new_events = []
    for r in results:
        if "error" in r or not r["event"]:
            continue
        key = f"{r['date']}|{r['event']}"
        if alerted.get(r["sym"]) != key:
            new_events.append(r)
            alerted[r["sym"]] = key

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
            if not chat_id:
                chat_id = tg_find_chat(token)
                if chat_id:
                    user_cfg = load_json(CONFIG_PATH, {}) or {}
                    user_cfg["telegram_chat_id"] = chat_id
                    save_json(CONFIG_PATH, user_cfg)
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
                    tg_send(token, chat_id, text + link_line)
                elif new_events:
                    order = {1: 0, -2: 1, -1: 2, 2: 3}
                    new_events.sort(key=lambda r: (order[r["event"]], r["sym"]))
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
    log("Telegram:", tg_status, "| sự kiện mới:", [f"{r['sym']}:{r['event']}" for r in new_events])

    public_cfg = {k: v for k, v in cfg.items() if k != "telegram_chat_id"}
    save_json(DATA_PATH, {
        "updated": now_vn.isoformat(timespec="minutes"),
        "mode": mode,
        "telegram": tg_status,
        "config": public_cfg,
        "results": results,
        "alerted": alerted,
    }, pretty=False)
    log(f"Xong: {len(ok)} mã ok, {len(bad)} lỗi.")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
