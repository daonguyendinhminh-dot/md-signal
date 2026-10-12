"""
Điểm AI (chế độ dự thính) cho tín hiệu MUA của MD Signal.

Mô hình cây tăng cường học trên mọi phiên của các mã cổ phiếu VN:
"mua mở cửa phiên sau, giữ 20 phiên, có lời trên 2% không?".
Điểm 0–100 là thứ hạng phần trăm của dự đoán so với các phiên đã học (50 = trung bình).

Chống nhìn trước tương lai:
- Lệnh trong năm Y được chấm bằng mô hình chỉ học dữ liệu có kết quả trước 01/12 năm Y-1.
- Tín hiệu mới được chấm bằng mô hình học tới hiện tại.
- Điểm đã chấm được lưu cố định trong docs/ai_scores.json, không bao giờ chấm lại.
"""
import json
import os

AI_MIN_ROWS = 2000
FEATURE_NAMES = ["vượt đỉnh", "cách MA200", "độ dốc MA200", "KL/TB20", "ATR%", "RSI14",
                 "lời 20 phiên", "lời 60 phiên", "độ rộng thị trường", "đà thị trường"]


def _sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _rma(x, n):
    out, prev, seed = [None] * len(x), None, []
    for i, v in enumerate(x):
        if prev is None:
            seed.append(v)
            if len(seed) == n:
                prev = sum(seed) / n
                out[i] = prev
        else:
            prev = (v + (n - 1) * prev) / n
            out[i] = prev
    return out


def _prep(bars):
    c = [b["c"] for b in bars]; h = [b["h"] for b in bars]; l = [b["l"] for b in bars]
    v = [b["v"] for b in bars]; o = [b["o"] for b in bars]; n = len(c)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    up = [0.0] + [max(c[i] - c[i - 1], 0) for i in range(1, n)]
    dn = [0.0] + [max(c[i - 1] - c[i], 0) for i in range(1, n)]
    au, ad = _rma(up, 14), _rma(dn, 14)
    rsi = [None if au[i] is None else 100 - 100 / (1 + au[i] / (ad[i] or 1e-9)) for i in range(n)]
    return {"d": [b["d"] for b in bars], "o": o, "h": h, "c": c, "v": v,
            "ma": _sma(c, 200), "vma": _sma(v, 20), "atr": _rma(tr, 14), "rsi": rsi,
            "ix": {b["d"]: i for i, b in enumerate(bars)}}


def _market(P):
    """Độ rộng (% mã trên MA200) và đà chung (TB lời 60 phiên) theo ngày."""
    acc = {}
    for I in P.values():
        for i, d in enumerate(I["d"]):
            a = acc.setdefault(d, [0, 0, 0.0, 0])
            if I["ma"][i]:
                a[0] += I["c"][i] > I["ma"][i]
                a[1] += 1
            if i >= 60:
                a[2] += I["c"][i] / I["c"][i - 60] - 1
                a[3] += 1
    return {d: ((a[0] / a[1]) if a[1] else None, (a[2] / a[3]) if a[3] else None) for d, a in acc.items()}


def _feats(I, i, mk):
    if i < 260 or I["ma"][i] is None or I["ma"][i - 20] is None or not I["atr"][i]:
        return None
    c = I["c"]
    b, m = mk.get(I["d"][i], (None, None))
    if b is None or m is None:
        return None
    hh = max(I["h"][i - 55:i])
    return [c[i] / hh - 1, c[i] / I["ma"][i] - 1, I["ma"][i] / I["ma"][i - 20] - 1,
            I["v"][i] / (I["vma"][i] or 1), I["atr"][i] / c[i], I["rsi"][i] or 50,
            c[i] / c[i - 20] - 1, c[i] / c[i - 60] - 1, b, m]


def _rows(P, mk):
    rows = []
    for I in P.values():
        n = len(I["c"])
        for i in range(260, n - 21):
            f = _feats(I, i, mk)
            if f:
                y = I["o"][i + 21] / I["o"][i + 1] - 1
                rows.append((I["d"][i], f, 1 if y > 0.02 else 0))
    return rows


def _train(rows, cutoff):
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    X = np.array([r[1] for r in rows if r[0] < cutoff])
    y = np.array([r[2] for r in rows if r[0] < cutoff])
    if len(X) < AI_MIN_ROWS or len(set(y.tolist())) < 2:
        return None
    md = HistGradientBoostingClassifier(max_depth=3, max_iter=150, learning_rate=0.05, random_state=7).fit(X, y)
    ref = np.sort(md.predict_proba(X)[:, 1])
    return md, ref


def _score(model, f):
    import numpy as np
    md, ref = model
    p = md.predict_proba(np.array([f]))[:, 1][0]
    return int(round(100 * np.searchsorted(ref, p) / len(ref)))


def apply_ai(results, bars_by_sym, path, log=print):
    """Gắn điểm AI vào trades / vị thế / tín hiệu mới (chỉ cổ phiếu VN). Trả về trạng thái."""
    try:
        import numpy  # noqa: F401
        import sklearn  # noqa: F401
    except Exception:
        return "thiếu thư viện (cần cập nhật file workflow)"
    store = {}
    try:
        with open(path, encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        pass
    vn = {r["sym"]: r for r in results if "error" not in r and not r["crypto"] and r["sym"] in bars_by_sym}
    if len(vn) < 3:
        return "quá ít mã cổ phiếu để học"
    P = {s: _prep(bars_by_sym[s]) for s in vn}
    mk = _market(P)
    rows = None
    models = {}

    def model_for(cutoff):
        nonlocal rows
        if cutoff not in models:
            if rows is None:
                rows = _rows(P, mk)
            models[cutoff] = _train(rows, cutoff)
        return models[cutoff]

    latest = max(r["date"] for r in vn.values())
    new = 0
    for s, r in vn.items():
        I = P[s]
        items = list(r.get("trades", []))
        if r.get("pos"):
            items.append(r["pos"])
        pend = {"sig_d": r["date"]} if r["status"] == "buy" else None
        if pend:
            items.append(pend)
        for t in items:
            sig = t.get("sig_d")
            if not sig:
                continue
            key = f"{s}|{sig}"
            if key not in store:
                i = I["ix"].get(sig)
                f = _feats(I, i, mk) if i is not None else None
                # tín hiệu mới: học tới hiện tại; lệnh cũ: chỉ học trước 01/12 năm trước đó
                cutoff = "9999" if sig == latest else f"{int(sig[:4]) - 1}-12-01"
                m = model_for(cutoff) if f else None
                store[key] = _score(m, f) if m else None
                new += 1
            t["ai"] = store[key]
        if pend:
            r["ai"] = pend["ai"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, separators=(",", ":"))
    log(f"AI: chấm mới {new} tín hiệu, {len(models)} mô hình")
    return "ok"
