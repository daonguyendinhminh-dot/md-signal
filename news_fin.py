"""
MD Signal — báo cáo tài chính (B) và tin tức đọc bằng DeepSeek (C).

- Chỉ số tài chính theo quý: Vietcap IQ (statistics-financial), lưu docs/f/<MÃ>.json
- Tin tức theo mã: Vietcap IQ (dự phòng KBS), lưu docs/news/<MÃ>.json
- DeepSeek đọc tin mới: tóm tắt 1 câu + chấm điểm -2..+2; nhận xét BCTC khi có quý mới
- docs/info.json: tóm tắt theo mã để app và tin nhắn Telegram dùng
Biến môi trường: DEEPSEEK_API_KEY (không có thì vẫn lấy tin/BCTC, chỉ bỏ phần AI đọc).
"""
import datetime as dt
import html
import json
import os
import re
import time
import urllib.error
import urllib.request

VCIQ = "https://iq.vietcap.com.vn/api/iq-insight-service"
KBS = "https://kbbuddywts.kbsec.com.vn/iis-server/investment/stockinfo"
VCI_HEAD = {"Referer": "https://trading.vietcap.com.vn/", "Origin": "https://trading.vietcap.com.vn"}
DS_URL = "https://api.deepseek.com/chat/completions"
DS_MODELS = ["deepseek-flash", "deepseek-chat"]
MAX_NEW_PER_SYM = 5
MAX_NEW_TOTAL = 60
KEEP_NEWS = 30


def _get(url, headers=None, data=None, timeout=25):
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
         "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def _pick(d, *names):
    low = {k.lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if isinstance(v, (int, float)):
            return v
    return None


def _clean(s, n):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


# ───────────────────────────── B: báo cáo tài chính ─────────────────────────────
def fetch_fin(sym):
    j = _get(f"{VCIQ}/v1/company/{sym}/statistics-financial", VCI_HEAD)
    data = j.get("data") if isinstance(j, dict) else j
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    q = []
    for r in rows:
        y, l = _pick(r, "yearReport", "year"), _pick(r, "lengthReport", "quarter")
        if not y or not l or not (1 <= l <= 4):
            continue
        q.append({
            "q": f"{int(y)}-Q{int(l)}",
            "upd": _pick(r, "updateDate"),
            "rev": _pick(r, "revenue"), "rev_g": _pick(r, "revenueGrowth"),
            "np": _pick(r, "netProfit"), "np_g": _pick(r, "netProfitGrowth"),
            "roe": _pick(r, "roe"), "pe": _pick(r, "pe"), "pb": _pick(r, "pb"),
            "eps": _pick(r, "eps"), "de": _pick(r, "de", "debtToEquity"),
            "nim": _pick(r, "nim"), "gm": _pick(r, "grossMargin", "grossProfitMargin"),
        })
    q.sort(key=lambda x: x["q"])
    if not q:
        raise ValueError("không có dữ liệu quý")
    return q


def fin_grade(q):
    """Đánh giá đơn giản sức khỏe tài chính quý gần nhất."""
    last = q[-1]
    pts, notes = 0, []
    g, roe, np_, de = last.get("np_g"), last.get("roe"), last.get("np"), last.get("de")
    if np_ is not None and np_ < 0:
        return "Yếu", ["Quý gần nhất lỗ"]
    if g is not None:
        if g > 0.1:
            pts += 1; notes.append(f"Lợi nhuận tăng {g*100:.0f}% so với cùng kỳ")
        elif g < -0.2:
            pts -= 1; notes.append(f"Lợi nhuận giảm {abs(g)*100:.0f}% so với cùng kỳ")
        else:
            notes.append(f"Lợi nhuận {g*100:+.0f}% so với cùng kỳ")
    if roe is not None:
        if roe >= 0.15:
            pts += 1; notes.append(f"ROE cao {roe*100:.0f}%")
        elif roe < 0.08:
            pts -= 1; notes.append(f"ROE thấp {roe*100:.0f}%")
    if de is not None and de > 3 and (last.get("nim") is None):
        pts -= 1; notes.append(f"Nợ/vốn cao {de:.1f} lần")
    return ("Tốt" if pts >= 2 else "Khá" if pts == 1 else "Trung bình" if pts == 0 else "Yếu"), notes


# ───────────────────────────── C: tin tức ─────────────────────────────
def fetch_news(sym):
    to = dt.date.today() + dt.timedelta(days=1)
    frm = to - dt.timedelta(days=60)
    items = []
    try:
        j = _get(f"{VCIQ}/v1/news?ticker={sym}&fromDate={frm:%Y%m%d}&toDate={to:%Y%m%d}&languageId=1&page=0&size=15", VCI_HEAD)
        data = j.get("data", {})
        rows = data.get("content", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        for x in rows:
            t = x.get("publicDate")
            d = dt.datetime.fromtimestamp(t / 1000, dt.timezone(dt.timedelta(hours=7))).strftime("%Y-%m-%d %H:%M") if isinstance(t, (int, float)) else str(t or "")[:16]
            items.append({"id": f"v{x.get('id') or x.get('newsId')}", "t": _clean(x.get("newsTitle"), 200), "d": d,
                          "s": _clean(x.get("newsShortContent"), 400),
                          "body": _clean(x.get("newsFullContent") or x.get("newsShortContent"), 1500),
                          "url": x.get("newsSourceLink") or ""})
    except Exception as e:
        print(f"  tin VCI {sym} lỗi: {str(e)[:100]}")
    if not items:
        j = _get(f"{KBS}/news/{sym}?l=1&p=1&s=15")
        rows = j if isinstance(j, list) else j.get("data", []) if isinstance(j, dict) else []
        for x in rows:
            low = {k.lower(): v for k, v in x.items()}
            url = low.get("url") or ""
            if url.startswith("/"):
                url = "https://www.kbsec.com.vn" + url
            items.append({"id": f"k{low.get('articleid')}", "t": _clean(low.get("title"), 200),
                          "d": str(low.get("publishtime") or "")[:16].replace("T", " "),
                          "s": _clean(low.get("head"), 400), "body": _clean(low.get("head"), 1500), "url": url})
    return [i for i in items if i["t"]]


def deepseek(key, system, user, log=print):
    last = None
    for model in DS_MODELS:
        try:
            j = _get(DS_URL, {"Authorization": f"Bearer {key}"}, {
                "model": model, "temperature": 0.2, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout=90)
            txt = j["choices"][0]["message"]["content"]
            u = j.get("usage", {})
            return json.loads(txt), (u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        except urllib.error.HTTPError as e:
            last = f"{e.code} {e.read().decode('utf-8', 'ignore')[:200]}"
            if e.code in (400, 404):   # tên model không có → thử model kế tiếp
                continue
            break
        except Exception as e:
            last = str(e)[:200]
            break
    raise RuntimeError(last)


SYS_NEWS = ("Bạn là chuyên viên phân tích chứng khoán Việt Nam. Đọc các tin về một mã cổ phiếu và đánh giá tác động "
            "tới giá cổ phiếu trong vài tuần tới. Trả lời JSON: {\"items\":[{\"id\":\"...\",\"score\":-2..2,"
            "\"tom_tat\":\"1 câu tiếng Việt ngắn gọn, nêu con số quan trọng nếu có\"}]}. "
            "Thang điểm: 2 rất tích cực, 1 tích cực, 0 trung tính/thủ tục (họp cổ đông, giấy tờ, thay đổi nhân sự nhỏ), "
            "-1 tiêu cực, -2 rất tiêu cực. Tin thủ tục hành chính chấm 0. Không bịa thông tin ngoài tin.")
SYS_FIN = ("Bạn là chuyên viên phân tích báo cáo tài chính doanh nghiệp Việt Nam. Dựa trên số liệu các quý, viết nhận xét "
           "ngắn cho nhà đầu tư cá nhân. Trả lời JSON: {\"nhan_xet\":\"2-3 câu tiếng Việt dễ hiểu, nêu xu hướng doanh thu, "
           "lợi nhuận, hiệu quả (ROE), định giá (P/E) và điểm cần lưu ý\",\"danh_gia\":\"Tốt|Khá|Trung bình|Yếu\"}. "
           "Chỉ dựa vào số liệu được cung cấp.")


def _fin_table(q):
    def f(v, pct=False, big=False):
        if v is None:
            return "-"
        if pct:
            return f"{v*100:.1f}%"
        if big:
            return f"{v/1e9:,.0f} tỷ"
        return f"{v:.2f}"
    lines = ["Quý | Doanh thu | Tăng DT | LNST | Tăng LN | ROE | P/E | P/B | EPS | Nợ/Vốn"]
    for x in q[-8:]:
        lines.append(f"{x['q']} | {f(x['rev'], big=True)} | {f(x['rev_g'], True)} | {f(x['np'], big=True)} | {f(x['np_g'], True)} | "
                     f"{f(x['roe'], True)} | {f(x['pe'])} | {f(x['pb'])} | {f(x['eps'])} | {f(x['de'])}")
    return "\n".join(lines)


def run(symbols, docs, key=None, log=print):
    """Chạy thu thập + đọc AI. Trả về (thông điệp Telegram hoặc '', thống kê)."""
    info = _load(os.path.join(docs, "info.json"), {})
    tok_in = tok_out = 0
    budget = MAX_NEW_TOTAL
    digest, fin_new, errs = [], [], []
    ai_err = None
    for sym in symbols:
        if sym.upper().endswith(("USDT", "USDC", "BUSD")):
            continue
        ent = info.setdefault(sym, {})
        # ── B: BCTC ──
        fpath = os.path.join(docs, "f", f"{sym}.json")
        old = _load(fpath, {})
        try:
            q = fetch_fin(sym)
            grade, notes = fin_grade(q)
            fin = {"q": q, "grade": grade, "notes": notes, "ai": old.get("ai"), "ai_q": old.get("ai_q")}
            if key and old.get("ai_q") != q[-1]["q"] and not ai_err:
                try:
                    out, (a, b) = deepseek(key, SYS_FIN, f"Mã {sym}. Số liệu theo quý (mới nhất ở dưới):\n{_fin_table(q)}", log)
                    tok_in += a; tok_out += b
                    fin["ai"] = {"text": str(out.get("nhan_xet", ""))[:600], "grade": out.get("danh_gia")}
                    fin["ai_q"] = q[-1]["q"]
                    if old.get("q"):   # chỉ báo khi có quý mới (không báo lần đầu)
                        fin_new.append(f"📊 <b>{sym} {q[-1]['q'].replace('-', ' ')}</b>: {fin['ai']['text']}")
                except Exception as e:
                    ai_err = str(e)
            _save(fpath, fin)
            last = q[-1]
            ent["fin"] = {"q": last["q"], "grade": grade, "notes": notes, "np_g": last["np_g"], "rev_g": last["rev_g"],
                          "roe": last["roe"], "pe": last["pe"], "pb": last["pb"], "eps": last["eps"],
                          "ai": (fin.get("ai") or {}).get("text")}
        except Exception as e:
            errs.append(f"BCTC {sym}: {str(e)[:80]}")
        # ── C: tin tức ──
        npath = os.path.join(docs, "news", f"{sym}.json")
        known = {x["id"]: x for x in _load(npath, [])}
        try:
            items = fetch_news(sym)
        except Exception as e:
            errs.append(f"Tin {sym}: {str(e)[:80]}")
            items = []
        fresh = [x for x in items if x["id"] not in known]
        first_time = not known
        merged = [{k: v for k, v in x.items() if k != "body"} for x in fresh] + list(known.values())
        merged.sort(key=lambda x: x.get("d", ""), reverse=True)
        merged = merged[:KEEP_NEWS]
        todo = [x for x in fresh if x["id"] in {m["id"] for m in merged}][:min(MAX_NEW_PER_SYM, budget)]
        if key and todo and not ai_err:
            try:
                payload = "\n\n".join(f"[id={x['id']}] {x['d']} — {x['t']}\n{x['body']}" for x in todo)
                out, (a, b) = deepseek(key, SYS_NEWS, f"Mã cổ phiếu: {sym}\n\n{payload}", log)
                tok_in += a; tok_out += b
                budget -= len(todo)
                res = {str(r.get("id")): r for r in out.get("items", []) if isinstance(r, dict)}
                for m in merged:
                    r = res.get(m["id"])
                    if r:
                        try:
                            m["score"] = max(-2, min(2, int(r.get("score", 0))))
                        except Exception:
                            m["score"] = 0
                        m["ai"] = str(r.get("tom_tat", ""))[:300]
                        if not first_time and m["score"] != 0:
                            icon = "🟢" if m["score"] > 0 else "🔴"
                            digest.append(f"{icon} <b>{sym}</b> ({m['score']:+d}): {m['ai'] or m['t']}")
            except Exception as e:
                ai_err = str(e)
        _save(npath, merged)
        scored = [m for m in merged[:10] if "score" in m]
        recent = [m for m in merged if m.get("d", "") >= (dt.date.today() - dt.timedelta(days=14)).isoformat() and "score" in m]
        ent["news"] = {"n": len(merged), "latest": merged[0] if merged else None,
                       "sent14": sum(m["score"] for m in recent) if recent else None,
                       "top": [m for m in scored if m.get("score")][:3]}
        time.sleep(0.3)
    info["_updated"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=7))).isoformat(timespec="minutes")
    info["_ai"] = "ok" if key and not ai_err else ("chưa cài DEEPSEEK_API_KEY" if not key else f"lỗi: {ai_err[:160]}")
    info["_errors"] = errs[:20]
    _save(os.path.join(docs, "info.json"), info)
    log(f"Tin/BCTC: token vào {tok_in}, ra {tok_out}; AI: {info['_ai']}; lỗi: {len(errs)}")
    msg = ""
    if digest or fin_new:
        msg = "📰 <b>MD Signal – tin đáng chú ý</b>\n\n" + "\n\n".join(digest[:12] + fin_new[:6])
    return msg, {"tok_in": tok_in, "tok_out": tok_out, "ai": info["_ai"], "errors": errs}
