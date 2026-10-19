"""MT5 Basket-Layering Autotrade Controller — server lokal, stdlib-only.

Dashboard web (index.html) kontrol bot di lokalhost. Strategy:
- Entry follow tren (EMA50/EMA200 di timeframe terpilih)
- Layering/martingale multiplier, tanpa SL/TP per posisi
- Close basket saat total profit >= target $
- Kill-switch: floating minus kena batas -> close all + stop otomatis
- Jam kerja per hari (start/end + off) atau disable -> 24 jam

Satu-satunya dependency: paket MetaTrader5 (sudah terinstall).
MT5 terminal HARUS jalan dan "Algo Trading" ON.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
PORT = 8895

try:
    import MetaTrader5 as mt5
except Exception:  # paket belum terinstall — server tetap jalan, koneksi gagal
    mt5 = None

MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
MAGIC = 990001          # penanda posisi milik bot ini
TICK_SECONDS = 2.0
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TF_MAP = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
          "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4"}


def default_config() -> dict:
    return {
        "symbol": "",            # dipilih dari web (dropdown simbol akun)
        "timeframe": "M15",      # timeframe deteksi tren
        "initial_lot": 0.01,
        "multiplier": 1.5,       # pengali lot layer berikutnya
        "max_layers": 6,         # cap layer — WAJIB ada, tidak bisa dihapus
        "step_price": 0.0,       # 0 = auto (500 * point simbol)
        "tp_basket_usd": 10.0,   # target profit basket ($) -> close all
        "killswitch_usd": 150.0, # floating minus ($) -> panic close + stop
        "schedule_enabled": True,  # false = bot jalan 24 jam
        "week": {d: {"off": d in ("sat", "sun"), "start": "09:00", "end": "23:00"} for d in DAYS},
    }


_lock = threading.Lock()


def load_state() -> dict:
    base = {"config": default_config(), "mode": "demo", "running": False, "basket_log": []}
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        for k, v in raw.items():
            if k == "config":
                merged = default_config()
                merged.update({k2: v2 for k2, v2 in v.items() if k2 in merged})
                merged["week"] = {d: {**default_config()["week"][d], **merged["week"].get(d, {})} for d in DAYS}
                base["config"] = merged
            else:
                base[k] = v
        base["running"] = False  # selalu mulai dari STOP setelah restart server
    except Exception:
        pass
    return base


def save_state() -> None:
    try:
        STATE_FILE.write_text(json.dumps(APP["state"], indent=2), encoding="utf-8")
    except Exception as e:
        add_log(f"Gagal simpan state: {e}", err=True)


APP = {
    "state": load_state(),
    "connected": False,
    "last_error": "",
    "algo_allowed": None,
    "trend": "flat",
    "logs": [],          # ring buffer log terakhir (maks 200)
}


def add_log(msg: str, err: bool = False) -> None:
    line = {"at": datetime.now().strftime("%H:%M:%S"), "msg": msg, "err": err}
    APP["logs"].insert(0, line)
    del APP["logs"][200:]
    print(("[ERR] " if err else "") + msg)


# ------------------------------------------------------------------ koneksi
def connect() -> bool:
    if mt5 is None:
        APP["last_error"] = "Paket MetaTrader5 tidak terinstall (pip install MetaTrader5)"
        return False
    if APP["connected"]:
        return True
    if not mt5.initialize(MT5_PATH):
        APP["last_error"] = (
            "MT5 init gagal: %s — pastikan terminal MT5 terbuka (login akun yang mau dipakai)"
            % (mt5.last_error(),)
        )
        APP["connected"] = False
        return False
    APP["connected"] = True
    APP["last_error"] = ""
    ti = mt5.terminal_info()
    APP["algo_allowed"] = bool(getattr(ti, "trade_allowed", False)) if ti else None
    acc = mt5.account_info()
    if acc:
        add_log("Terhubung MT5 — login %s (%s)" % (acc.login, "DEMO" if acc.trade_mode == 0 else "REAL"))
    return True


# ------------------------------------------------------------------ helper market
def symbol_info_or_log(symbol: str):
    si = mt5.symbol_info(symbol)
    if si is None:
        add_log("Simbol %s tidak dikenal MT5" % symbol, err=True)
    return si


def pick_filling(si) -> int:
    fm = int(getattr(si, "filling_mode", 0) or 0)
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def normalize_lot(lot: float, si) -> float:
    step = float(si.volume_step or 0.01)
    lo = float(si.volume_min or 0.01)
    hi = float(si.volume_max or 100.0)
    lot = max(lo, min(hi, math.floor(lot / step) * step))
    return round(lot, 8)


def market_order(symbol: str, side: str, lot: float, comment: str):
    si = symbol_info_or_log(symbol)
    if si is None:
        return None
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        add_log("Tick %s tidak tersedia" % symbol, err=True)
        return None
    is_buy = side == "buy"
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if is_buy else tick.bid,
        "deviation": 50,
        "magic": MAGIC,
        "comment": comment,
        "type_filling": pick_filling(si),
    }
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        add_log("Order %s %s %s GAGAL (retcode=%s %s)" % (
            side, lot, symbol, getattr(res, "retcode", None), getattr(res, "comment", mt5.last_error())), err=True)
        return None
    add_log("Entry %s %s %s @ %s (%s)" % (side, lot, symbol, res.price, comment))
    return res


def close_position(p) -> bool:
    is_buy = p.type == mt5.POSITION_TYPE_BUY
    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        return False
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": p.symbol,
        "volume": p.volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": p.ticket,
        "price": tick.bid if is_buy else tick.ask,
        "deviation": 100,
        "magic": MAGIC,
        "comment": "basket_close",
        "type_filling": pick_filling(mt5.symbol_info(p.symbol)),
    }
    res = mt5.order_send(req)
    ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
    if not ok:
        add_log("Close %s GAGAL (retcode=%s)" % (p.ticket, getattr(res, "retcode", None)), err=True)
    return ok


def basket_positions(symbol: str | None = None) -> list:
    if not APP["connected"]:
        return []
    ps = mt5.positions_get() or []
    return [p for p in ps if p.magic == MAGIC and (symbol is None or p.symbol == symbol)]


def close_all_basket(reason: str) -> None:
    sym = APP["state"]["config"]["symbol"]
    ps = basket_positions(sym)
    if not ps:
        return
    floating = sum(p.profit + p.swap for p in ps)
    ok = 0
    for p in sorted(ps, key=lambda x: x.ticket, reverse=True):
        if close_position(p):
            ok += 1
    add_log("Close basket [%s]: %s/%s posisi ditutup, floating $%.2f" % (reason, ok, len(ps), floating))
    APP["state"]["basket_log"].insert(0, {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": sym, "layers": len(ps), "profit": round(floating, 2), "reason": reason,
    })
    del APP["state"]["basket_log"][100:]
    save_state()


# ------------------------------------------------------------------ tren & jadwal
def ema(vals: list, period: int):
    if len(vals) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(vals[:period]) / period
    for v in vals[period:]:
        e = v * k + e * (1 - k)
    return e


def compute_trend() -> str:
    cfg = APP["state"]["config"]
    sym = cfg["symbol"]
    if not APP["connected"] or not sym:
        return "flat"
    tf = getattr(mt5, TF_MAP.get(cfg["timeframe"], "TIMEFRAME_M15"))
    rates = mt5.copy_rates_from_pos(sym, tf, 0, 260)
    if rates is None or len(rates) < 205:
        return "flat"
    closes = [r["close"] for r in rates]
    e50, e200 = ema(closes, 50), ema(closes, 200)
    if e50 is None or e200 is None:
        return "flat"
    c = closes[-1]
    if c > e50 > e200:
        return "up"
    if c < e50 < e200:
        return "down"
    return "flat"


def schedule_check(now: datetime | None = None):
    cfg = APP["state"]["config"]
    if not cfg["schedule_enabled"]:
        return True, "jadwal OFF — bot jalan 24 jam"
    now = now or datetime.now()
    key = DAYS[now.weekday()]
    d = cfg["week"][key]
    if d["off"]:
        return False, "%s hari off" % key.capitalize()
    t = now.time().replace(second=0, microsecond=0)
    ok = d["start"] <= now.strftime("%H:%M") <= d["end"]
    try:
        ok = datetime.strptime(d["start"], "%H:%M").time() <= t <= datetime.strptime(d["end"], "%H:%M").time()
    except Exception:
        pass
    return ok, "%s: %s-%s" % (key.capitalize(), d["start"], d["end"])


# ------------------------------------------------------------------ strategy loop
def manage() -> None:
    st = APP["state"]
    cfg = st["config"]
    if not st["running"] or not APP["connected"]:
        return
    if not cfg["symbol"]:
        return
    APP["trend"] = compute_trend()

    ps = basket_positions(cfg["symbol"])
    if not ps:
        mt5.symbol_select(cfg["symbol"], True)
    floating = sum(p.profit + p.swap for p in ps)
    tp = float(cfg["tp_basket_usd"] or 0)
    kill = float(cfg["killswitch_usd"] or 0)

    # 1) kill-switch — WAJIB, tidak bisa dihapus
    if ps and kill > 0 and floating <= -kill:
        add_log("KILL SWITCH: floating $%.2f <= -$%s. Close all + STOP." % (floating, kill), err=True)
        close_all_basket("killswitch")
        st["running"] = False
        save_state()
        return

    # 2) target basket tercapai -> close all
    if ps and tp > 0 and floating >= tp:
        add_log("TP basket kena: floating $%.2f >= $%.2f" % (floating, tp))
        close_all_basket("tp")
        return

    # 3) di luar jam kerja: tidak ada entry baru (basket tetap dikelola soal TP/kill)
    sched_ok, _sched_txt = schedule_check()
    if not sched_ok:
        return

    # 4) entry pertama follow tren
    if not ps:
        si = symbol_info_or_log(cfg["symbol"])
        if si is None:
            return
        lot = normalize_lot(cfg["initial_lot"], si)
        if APP["trend"] == "up":
            market_order(cfg["symbol"], "buy", lot, "L1 trend-up")
        elif APP["trend"] == "down":
            market_order(cfg["symbol"], "sell", lot, "L1 trend-down")
        return

    # 5) layering: harga jalan melawan basket >= step -> tambah layer
    if len(ps) >= int(cfg["max_layers"]):
        return
    is_buy = ps[0].type == mt5.POSITION_TYPE_BUY
    total_vol = sum(p.volume for p in ps)
    avg = sum(p.price_open * p.volume for p in ps) / total_vol
    tick = mt5.symbol_info_tick(cfg["symbol"])
    if tick is None:
        return
    cur = tick.bid if is_buy else tick.ask
    adverse = (avg - cur) if is_buy else (cur - avg)
    si = symbol_info_or_log(cfg["symbol"])
    step = float(cfg["step_price"] or 0) or float(si.point) * 500
    if adverse >= step:
        lot = normalize_lot(float(cfg["initial_lot"]) * (float(cfg["multiplier"]) ** len(ps)), si)
        add_log("Layer %d: adverse %.5f >= step %.5f, lot %s" % (len(ps) + 1, adverse, step, lot))
        market_order(cfg["symbol"], "buy" if is_buy else "sell", lot, "L%d (avg %.5f)" % (len(ps) + 1, avg))


def loop() -> None:
    while True:
        try:
            with _lock:
                manage()
        except Exception as e:
            add_log("Loop error: %r" % e, err=True)
        time.sleep(TICK_SECONDS)


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "M5Basket/1.0"

    def log_message(self, fmt, *args):
        return

    # ---------------------------------------------------------- helpers
    def _send(self, code: int, data: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------------------------------------------------------- GET
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            f = ROOT / "index.html"
            self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._json(status_payload())
            return
        if path == "/api/symbols":
            self._json(symbols_payload())
            return
        if path == "/api/basket-log":
            self._json({"items": APP["state"]["basket_log"]})
            return
        self._send(404, b"not found", "text/plain")

    # ---------------------------------------------------------- POST
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._body()
        with _lock:
            if path == "/api/control":
                self._json(api_control(body))
            elif path == "/api/config":
                self._json(api_config(body))
            elif path == "/api/mode":
                self._json(api_mode(body))
            elif path == "/api/panic":
                self._json(api_panic())
            elif path == "/api/connect":
                ok = connect()
                self._json({"ok": ok, "error": APP["last_error"]})
            else:
                self._send(404, b"not found", "text/plain")


def api_control(body: dict) -> dict:
    st = APP["state"]
    action = body.get("action")
    if action == "start":
        if not APP["connected"] and not connect():
            return {"ok": False, "error": APP["last_error"]}
        cfg = st["config"]
        if not cfg["symbol"]:
            return {"ok": False, "error": "Pilih pair dulu di dashboard."}
        if APP["algo_allowed"] is False:
            return {"ok": False, "error": "Algo Trading OFF di terminal MT5 — nyalakan tombol 'Algo Trading' di toolbar MT5."}
        if not mt5.symbol_select(cfg["symbol"], True):
            return {"ok": False, "error": "Simbol %s gagal di-select." % cfg["symbol"]}
        # guard mode vs akun yang terlogin di terminal
        acc = mt5.account_info()
        if acc:
            is_real = acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_REAL
            if st["mode"] == "demo" and is_real:
                return {"ok": False, "error": "Mode=DEMO tapi akun terlogin di MT5 adalah REAL. Buka akun demo dulu, atau ganti mode ke LIVE (ketik konfirmasi)."}
            if st["mode"] == "live" and not is_real:
                return {"ok": False, "error": "Mode=LIVE tapi akun terlogin masih DEMO. Buka akun real dulu di MT5."}
        st["running"] = True
        save_state()
        add_log("BOT START — %s %s (mode %s)" % (cfg["symbol"], cfg["timeframe"], st["mode"]))
        return {"ok": True}
    if action == "stop":
        st["running"] = False
        save_state()
        add_log("BOT STOP (disuruh dari web)")
        return {"ok": True}
    return {"ok": False, "error": "action? start|stop"}


def api_config(body: dict) -> dict:
    st = APP["state"]
    cfg = st["config"]
    old_symbol = cfg["symbol"]
    incoming = {k: v for k, v in body.items() if k in cfg}
    # angka -> float/int sesuai tipe (patch parsial aman)
    for k in ("initial_lot", "multiplier", "step_price", "tp_basket_usd", "killswitch_usd"):
        if k in incoming:
            incoming[k] = float(incoming[k] or 0)
    if "max_layers" in incoming:
        incoming["max_layers"] = int(incoming["max_layers"] or 1)
    if incoming.get("max_layers") is not None and incoming["max_layers"] < 2:
        incoming["max_layers"] = 2
    if incoming.get("killswitch_usd") is not None and incoming["killswitch_usd"] <= 0:
        return {"ok": False, "error": "killswitch_usd wajib > 0 (guard minimum)."}
    if st["running"] and incoming.get("symbol", old_symbol) != old_symbol:
        return {"ok": False, "error": "Bot lagi jalan — STOP dulu sebelum ganti pair."}
    if "week" in incoming:
        incoming["week"] = {d: {**cfg["week"][d], **incoming["week"].get(d, {})} for d in DAYS}
        for d in DAYS:
            w = incoming["week"][d]
            for k2 in ("start", "end"):
                if not isinstance(w.get(k2), str) or len(w[k2]) != 5:
                    w[k2] = cfg["week"][d][k2]
    cfg.update(incoming)
    save_state()
    add_log("Config diupdate dari web")
    return {"ok": True, "config": cfg}


def api_mode(body: dict) -> dict:
    st = APP["state"]
    mode = body.get("mode")
    if mode not in ("demo", "live"):
        return {"ok": False, "error": "mode? demo|live"}
    if mode == "live":
        if st["mode"] != "live":
            if body.get("confirm") != "LIVE":
                return {"ok": False, "need_confirm": True,
                        "error": "Ganti ke LIVE: ketik LIVE di konfirmasi (uang beneran, tanpa SL)."}
            add_log("MODE DIGANTI -> LIVE (konfirmasi user)", err=True)
    if mode == "demo" and st["mode"] == "live":
        add_log("Mode diganti -> DEMO")
    st["mode"] = mode
    save_state()
    return {"ok": True, "mode": st["mode"]}


def api_panic() -> dict:
    st = APP["state"]
    if not APP["connected"]:
        return {"ok": False, "error": APP["last_error"] or "MT5 belum terhubung."}
    add_log("PANIC: close all basket (disuruh dari web)")
    close_all_basket("manual")
    st["running"] = False
    save_state()
    return {"ok": True}


def symbols_payload() -> dict:
    if not APP["connected"] and not connect():
        return {"ok": False, "error": APP["last_error"], "items": []}
    syms = mt5.symbols_get() or []
    items = sorted({s.name for s in syms if getattr(s, "visible", True)})
    return {"ok": True, "items": items[:400]}


def status_payload() -> dict:
    st = APP["state"]
    cfg = st["config"]
    acc = None
    bp: list = []
    floating = 0.0
    lots = 0.0
    avg = None
    next_layer_lot = None
    if APP["connected"]:
        ai = mt5.account_info()
        if ai:
            acc = {"login": ai.login, "name": ai.name, "currency": ai.currency,
                   "balance": round(ai.balance, 2), "equity": round(ai.equity, 2),
                   "margin_free": round(ai.margin_free, 2),
                   "trade_mode": ai.trade_mode, "server": ai.server}
        if cfg["symbol"]:
            ps = basket_positions(cfg["symbol"])
            tick = mt5.symbol_info_tick(cfg["symbol"]) if ps else None
            bp = [{"ticket": p.ticket,
                   "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                   "lot": p.volume, "open": p.price_open,
                   "cur": (tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask) if tick else 0,
                   "profit": round(p.profit + p.swap, 2)} for p in ps]
            floating = round(sum(p.profit + p.swap for p in ps), 2)
            lots = round(sum(p.volume for p in ps), 2)
            if ps:
                tot_vol = sum(p.volume for p in ps)
                avg = round(sum(p.price_open * p.volume for p in ps) / tot_vol, 5)
            si = mt5.symbol_info(cfg["symbol"])  # diam-diam (walau None) — jangan spam log tiap poll
            if si and len(ps) < int(cfg["max_layers"]):
                next_layer_lot = normalize_lot(float(cfg["initial_lot"]) * (float(cfg["multiplier"]) ** len(ps)), si)
    sched_ok, sched_txt = schedule_check()
    tp = float(cfg["tp_basket_usd"] or 0)
    step = float(cfg["step_price"] or 0)
    return {
        "ok": True,
        "connected": APP["connected"],
        "error": APP["last_error"],
        "algo_allowed": APP["algo_allowed"],
        "running": st["running"],
        "mode": st["mode"],
        "trend": APP["trend"],
        "config": cfg,
        "account": acc,
        "basket": {"count": len(bp), "lots": lots, "floating": floating,
                   "avg": avg, "next_layer_lot": next_layer_lot,
                   "tp_progress": (round((floating + tp) / (2 * tp), 4) if tp > 0 else None),
                   "positions": bp,
                   "step_effective": step or None},
        "schedule": {"ok": sched_ok, "text": sched_txt, "enabled": cfg["schedule_enabled"]},
        "logs": APP["logs"][:20],
    }


# ------------------------------------------------------------------ main
def run() -> None:
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("MT5 Basket Bot dashboard: http://127.0.0.1:%d/" % PORT)
    print("MT5 terminal: %s" % MT5_PATH)
    print("Ctrl+C untuk stop server.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stop.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()
