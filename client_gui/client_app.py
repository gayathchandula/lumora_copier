"""
Lumora Scalping - Client App (all-in-one, no MT5 EA required)
------------------------------------------------------------
This single app IS the client's trade copier. It connects straight to the
client's already-running, logged-in MT5 terminal using MetaQuotes' official
"MetaTrader5" Python package (local IPC - no chart, no attached EA, no
compiling anything in MetaEditor). It then:

  1. Walks a new customer through self-service registration, package
     selection and payment submission, and waits for admin approval
  2. Polls the Copier Server for new signals (OPEN / MODIFY / CLOSE) once
     the subscription is active
  3. Places/modifies/closes the matching trade on this MT5 account
  4. Reports the result + profit back to the server
  5. Sends a heartbeat (balance/equity) so the Admin GUI shows this client live
  6. Shows the client their own connection status, subscription and profit

Requirements (on the CLIENT's Windows machine):
  - MetaTrader 5 terminal installed, logged into their account, running
  - "Algo Trading" enabled in MT5 (top toolbar button)
  - pip install MetaTrader5

Run:      python client_app.py
Package:  pyinstaller --onefile --windowed --collect-all MetaTrader5 --name LumoraScalpingClient client_app.py
          (build ON WINDOWS to get a distributable .exe - MetaTrader5 is a
          Windows-only package, so this app only runs on Windows)
"""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

import urllib.error
import urllib.request

try:
    import MetaTrader5 as mt5
    MT5_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 - want the real reason, not just ImportError
    mt5 = None  # We still let the window open so the user sees a clear error message.
    MT5_IMPORT_ERROR = e

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    import qrcode
    CARD_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 - Download Profit Card is optional; the rest of the app must still work
    Image = ImageDraw = ImageFont = ImageTk = qrcode = None
    CARD_IMPORT_ERROR = e

APP_TITLE = "Lumora Scalping — Client"

# Bump this on every release that ships a new client .exe - the server's
# /api/client-version tells the app what the latest is, and this is what it
# compares against. Keep in sync with the version you set in
# server/client_version.js when you publish a build.
CLIENT_VERSION = "1.0.6"


def _parse_version(v):
    try:
        return tuple(int(p) for p in v.strip().split("."))
    except (ValueError, AttributeError):
        return (0,)


def _finish_pending_update():
    # Second half of the update flow started in _apply_update_and_restart:
    # this process is the newly-downloaded build, launched from a staging
    # path in %TEMP% rather than the app's real exe path. Swapping the real
    # path's file out from under the *old* process before relaunching it
    # (the previous approach) trips PyInstaller's bootloader security check
    # ("parent process has different executable"), since the parent it
    # spawns from would no longer be running the file its own path points
    # to. Launching from a distinct staged path sidesteps that, so this step
    # does the actual swap itself, after the old process (our parent) is
    # already gone.
    if "--finish-update" not in sys.argv:
        return
    target_path = sys.argv[sys.argv.index("--finish-update") + 1]
    staged_path = sys.executable
    old_backup = target_path + ".old"

    for _ in range(40):  # ~10s of retries while the old process's file lock clears
        try:
            if os.path.exists(old_backup):
                os.remove(old_backup)
            if os.path.exists(target_path):
                os.rename(target_path, old_backup)
            shutil.copy2(staged_path, target_path)  # copy, not move - we're still running from staged_path
            break
        except OSError:
            time.sleep(0.25)
    else:
        return  # couldn't swap in this time; stay running from the staged copy

    subprocess.Popen([target_path], env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    sys.exit(0)

# Hardcoded so clients never have to know or type a server address. This is
# a reserved ngrok static domain (dashboard.ngrok.com/domains), so unlike a
# plain "ngrok http" URL it stays the same across tunnel restarts — just make
# sure the tunnel is always started as:
#   ngrok http --url=https://clamor-alienable-scouts.ngrok-free.dev 4000
SERVER_URL = "https://lumora-copy-trading.onrender.com"

def _resource_path(*parts):
    # Frozen (PyInstaller onefile): --add-data "..\assets;assets" places the
    # folder directly under the extraction root, sys._MEIPASS - no ".." here.
    # Unfrozen (running client_app.py directly): assets/ is a sibling of this
    # script's own directory, one level up, so ".." is needed there instead.
    if getattr(sys, "_MEIPASS", None):
        return os.path.join(sys._MEIPASS, *parts)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", *parts)


# Profit-card ("Download Profit Card") constants.
# The source file has a flat light-gray JPEG background (JPEGs can't have
# transparency) - _load_logo_with_transparent_bg() below keys it out at
# runtime via flood-fill rather than needing a pre-cut PNG.
LUMORA_LOGO_PATH = _resource_path("assets", "lumora_logo.jpg")
PROFIT_CARD_QR_URL = "https://lumora-one-pi.vercel.app/"

COLORS = {
    "bg": "#0b0f1a",
    "bg2": "#121826",
    "panel": "#161d2e",
    "text": "#e6ecff",
    "muted": "#8290ab",
    "accent": "#00e5ff",
    "accent2": "#7c4dff",
    "good": "#33ff99",
    "warn": "#ffb020",
    "bad": "#ff4d6d",
}

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".lumora_scalping_client.json")
MAPPING_PATH = os.path.join(os.path.expanduser("~"), ".lumora_scalping_client_mapping.json")
SIGNAL_ID_PATH = os.path.join(os.path.expanduser("~"), ".lumora_scalping_client_signal_id.json")

DEFAULT_CFG = {
    "server_url": SERVER_URL,
    "client_id": "",
    "client_key": "",
    "symbol": "XAUUSD",
    "copy_sl": True,
    "copy_tp": True,
    "copy_modifications": True,
    "max_slippage_points": 30,
    "magic_number": 7788990,
    "poll_seconds": 2,
}

# Statuses that mean "no active subscription right now" -> show the
# package/payment screen (covers first-time payment and every renewal).
NEEDS_PAYMENT_STATUSES = ("PENDING_PAYMENT", "EXPIRED", "HALTED", "REJECTED")


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(default) if isinstance(default, dict) else default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


def api_call(server_url, client_id, client_key, path, method="GET", body=None):
    url = server_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Client-Id", client_id)
    req.add_header("X-Client-Key", client_key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


class CopierEngine:
    """Background trading engine: polls the server and drives MT5 directly."""

    def __init__(self, cfg, log_fn, status_fn):
        self.cfg = cfg
        self.log = log_fn
        self.status = status_fn
        self.running = False
        self.mapping = load_json(MAPPING_PATH, {})  # admin_position_id(str) -> client_ticket(int)
        self._thread = None

        # Persisted across restarts, so a client only ever replays signals it
        # actually missed while closed - never the admin's entire trade
        # history. A brand-new client (no saved state at all) has no "missed
        # while closed" period, so it skips straight to the latest signal
        # id instead of opening/closing every historical trade the admin
        # ever made.
        saved = load_json(SIGNAL_ID_PATH, {})
        self.last_signal_id = saved.get("last_signal_id")
        self._skip_backlog_on_start = self.last_signal_id is None
        if self.last_signal_id is None:
            self.last_signal_id = 0

    # ---------------------------------------------------------------- MT5
    def connect_mt5(self):
        if mt5 is None:
            raise RuntimeError(f"MetaTrader5 package failed to load ({MT5_IMPORT_ERROR}). "
                                "Run: pip install MetaTrader5")
        if not mt5.initialize():
            raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}. "
                                "Make sure MT5 is open, logged in, and 'Algo Trading' is enabled.")
        acc = mt5.account_info()
        if acc is None:
            raise RuntimeError("Connected to MT5 but couldn't read account info.")
        return acc

    # ------------------------------------------------------------- engine
    def start(self):
        if self.running:
            return
        self.connect_mt5()
        if self._skip_backlog_on_start:
            self._initialize_last_signal_id()
            self._skip_backlog_on_start = False
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.log("Copier engine started.")

    def _initialize_last_signal_id(self):
        # First-ever start for this install: find the current latest signal
        # id without acting on any of them, so polling then only ever sees
        # what's genuinely new from this point forward.
        try:
            signals = api_call(self.cfg["server_url"], self.cfg["client_id"], self.cfg["client_key"],
                                "/api/client/signals?since=0")
            if signals:
                self.last_signal_id = max(s["id"] for s in signals)
        except Exception as e:
            self.log(f"[warn] couldn't determine latest signal id, starting from 0: {e}")
        save_json(SIGNAL_ID_PATH, {"last_signal_id": self.last_signal_id})

    def stop(self):
        self.running = False
        self.log("Copier engine stopped.")

    def _loop(self):
        while self.running:
            try:
                self.poll_signals()
                self.heartbeat()
            except Exception as e:
                self.log(f"[error] {e}")
            time.sleep(max(1, int(self.cfg.get("poll_seconds", 2))))

    def poll_signals(self):
        signals = api_call(
            self.cfg["server_url"], self.cfg["client_id"], self.cfg["client_key"],
            f"/api/client/signals?since={self.last_signal_id}"
        )
        for sig in signals:
            try:
                self.handle_signal(sig)
            except Exception as e:
                self.log(f"[error] signal {sig.get('id')}: {e}")
            if sig["id"] > self.last_signal_id:
                self.last_signal_id = sig["id"]
        if signals:
            save_json(SIGNAL_ID_PATH, {"last_signal_id": self.last_signal_id})

    def handle_signal(self, sig):
        # No symbol-name matching against the admin's signal here on purpose:
        # every trade always executes against this client's own cfg["symbol"]
        # (see do_open/do_modify/do_close), which is set to whatever THIS
        # client's broker calls gold - it doesn't need to match the admin's
        # broker's literal symbol string (e.g. admin's "XAUUSD" vs a client's
        # broker-suffixed "XAUUSDm").
        action = sig["action"]
        if action == "OPEN":
            self.do_open(sig)
        elif action == "MODIFY" and self.cfg.get("copy_modifications", True):
            self.do_modify(sig)
        elif action == "PARTIAL_CLOSE":
            self.do_partial_close(sig)
        elif action == "CLOSE":
            self.do_close(sig)

    # ------------------------------------------------------------- lots
    def compute_lot(self, admin_volume):
        # Always mirrors the admin's exact trade size - no client-side lot
        # scaling or risk sizing. The only adjustment is snapping to the
        # broker's allowed volume step/min/max, which isn't a choice, it's
        # a requirement for the order to be accepted at all.
        info = mt5.symbol_info(self.cfg["symbol"])
        lot = admin_volume
        if info:
            step = info.volume_step or 0.01
            lot = max(info.volume_min, min(info.volume_max, round(lot / step) * step))
        return round(lot, 2)

    def _pick_filling_mode(self):
        # Different brokers support different order-filling modes per symbol
        # (a bitmask on symbol_info().filling_mode). Hardcoding one - as this
        # used to do - makes mt5.order_send() silently return None (not even
        # an error retcode) on any broker that doesn't support that exact
        # mode, which looks like every single OPEN failing for no reason.
        # The Python MetaTrader5 package doesn't expose the SYMBOL_FILLING_*
        # bitmask constants (only ORDER_FILLING_*), unlike native MQL5 - these
        # are the same stable, documented protocol values, just hardcoded.
        SYMBOL_FILLING_FOK = 1
        SYMBOL_FILLING_IOC = 2
        info = mt5.symbol_info(self.cfg["symbol"])
        mode = info.filling_mode if info else 0
        if mode & SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        if mode & SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    # ------------------------------------------------------------- actions
    def do_open(self, sig):
        lot = self.compute_lot(sig["volume"])
        if lot <= 0:
            self.log(f"Skipping OPEN (computed lot <= 0) for admin pos {sig['position_id']}")
            return

        tick = mt5.symbol_info_tick(self.cfg["symbol"])
        is_buy = sig["type"] == "BUY"
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = tick.ask if is_buy else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.cfg["symbol"],
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sig["sl"] if self.cfg.get("copy_sl", True) else 0.0,
            "tp": sig["tp"] if self.cfg.get("copy_tp", True) else 0.0,
            "deviation": int(self.cfg.get("max_slippage_points", 30)),
            "magic": int(self.cfg.get("magic_number", 7788990)),
            "comment": f"copy#{sig['position_id']}",
            "type_filling": self._pick_filling_mode(),
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            self.log(f"OPEN failed for admin pos {sig['position_id']}: {result} | mt5.last_error()={mt5.last_error()}")
            self.report(sig["id"], 0, "FAILED", 0, 0)
            return

        self.mapping[str(sig["position_id"])] = result.order
        save_json(MAPPING_PATH, self.mapping)
        self.log(f"Opened {sig['type']} {lot} lots, ticket={result.order} (mirrors admin pos {sig['position_id']})")
        self.report(sig["id"], result.order, "OPENED", result.price, 0)

    def do_modify(self, sig):
        ticket = self.mapping.get(str(sig["position_id"]))
        if not ticket:
            return
        pos = self._find_position(ticket)
        if pos is None:
            return
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": self.cfg["symbol"],
            "sl": sig["sl"] if self.cfg.get("copy_sl", True) else pos.sl,
            "tp": sig["tp"] if self.cfg.get("copy_tp", True) else pos.tp,
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log(f"Modified ticket {ticket}: SL={sig['sl']} TP={sig['tp']}")
            self.report(sig["id"], ticket, "MODIFIED", 0, 0)
        else:
            self.log(f"MODIFY failed for ticket {ticket}: {result} | mt5.last_error()={mt5.last_error()}")

    def do_partial_close(self, sig):
        ticket = self.mapping.get(str(sig["position_id"]))
        if not ticket:
            return
        pos = self._find_position(ticket)
        if pos is None:
            return

        # Mirrors the admin's exact closed volume (same principle as OPEN),
        # clamped to what's actually left on this position and snapped to
        # the broker's volume step.
        close_vol = min(sig["volume"], pos.volume)
        info = mt5.symbol_info(self.cfg["symbol"])
        min_vol = info.volume_min if info else 0.01
        if info:
            step = info.volume_step or 0.01
            close_vol = round(close_vol / step) * step
        if close_vol <= 0:
            return

        remaining = round(pos.volume - close_vol, 2)
        if remaining < min_vol:
            # Nothing tradeable would be left on this broker - close it fully instead.
            self.do_close(sig)
            return

        tick = mt5.symbol_info_tick(self.cfg["symbol"])
        is_buy = pos.type == mt5.ORDER_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": self.cfg["symbol"],
            "volume": close_vol,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": int(self.cfg.get("max_slippage_points", 30)),
            "magic": int(self.cfg.get("magic_number", 7788990)),
            "type_filling": self._pick_filling_mode(),
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log(f"Partially closed {close_vol} lots of ticket {ticket} (mirrors admin pos {sig['position_id']})")
            self.report(sig["id"], ticket, "PARTIAL_CLOSED", result.price, 0)
        else:
            self.log(f"PARTIAL_CLOSE failed for ticket {ticket}: {result} | mt5.last_error()={mt5.last_error()}")

    def do_close(self, sig):
        ticket = self.mapping.get(str(sig["position_id"]))
        if not ticket:
            return
        pos = self._find_position(ticket)
        if pos is None:
            self.mapping.pop(str(sig["position_id"]), None)
            save_json(MAPPING_PATH, self.mapping)
            return

        tick = mt5.symbol_info_tick(self.cfg["symbol"])
        is_buy = pos.type == mt5.ORDER_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": self.cfg["symbol"],
            "volume": pos.volume,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": int(self.cfg.get("max_slippage_points", 30)),
            "magic": int(self.cfg.get("magic_number", 7788990)),
            "type_filling": self._pick_filling_mode(),
        }
        profit_before = pos.profit
        entry_price = pos.price_open
        opened_at = datetime.fromtimestamp(pos.time, tz=timezone.utc).isoformat()
        trade_type = "BUY" if is_buy else "SELL"
        # Margin actually committed to this trade, so the profit card can show
        # a real ROI-on-margin percentage instead of a guessed formula.
        margin = mt5.order_calc_margin(
            mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
            self.cfg["symbol"], pos.volume, entry_price,
        )
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            self.log(f"Closed ticket {ticket} (mirrors admin pos {sig['position_id']}) P/L={profit_before:.2f}")
            self.report(sig["id"], ticket, "CLOSED", result.price, profit_before,
                        symbol=self.cfg["symbol"], type=trade_type, volume=pos.volume,
                        entry_price=entry_price, opened_at=opened_at, margin=margin or 0)
            self.mapping.pop(str(sig["position_id"]), None)
            save_json(MAPPING_PATH, self.mapping)
        else:
            self.log(f"CLOSE failed for ticket {ticket}: {result} | mt5.last_error()={mt5.last_error()}")

    def _find_position(self, ticket):
        positions = mt5.positions_get(ticket=ticket)
        return positions[0] if positions else None

    # ------------------------------------------------------------- server
    def report(self, signal_id, ticket, status, price, profit, **extra):
        try:
            body = {"signal_id": signal_id, "client_ticket": ticket, "status": status,
                    "price": price, "profit": profit}
            body.update(extra)
            api_call(self.cfg["server_url"], self.cfg["client_id"], self.cfg["client_key"],
                     "/api/client/report", method="POST", body=body)
        except Exception as e:
            self.log(f"[warn] report failed: {e}")

    def heartbeat(self):
        acc = mt5.account_info()
        if acc is None:
            return
        try:
            api_call(self.cfg["server_url"], self.cfg["client_id"], self.cfg["client_key"],
                     "/api/client/heartbeat", method="POST",
                     body={"balance": acc.balance, "equity": acc.equity,
                           "floating_profit": acc.equity - acc.balance})
        except Exception as e:
            self.log(f"[warn] heartbeat failed: {e}")


# ---------------------------------------------------------------- profit card
_logo_cache = {}


def _load_logo_with_transparent_bg(path):
    # The source is a flat-background JPEG (no alpha channel). A single
    # global color-distance key would also erase the logo's own near-white
    # letters, so instead flood-fill from the corners: this only spreads
    # through pixels connected to the background, stopping at the dark
    # outline stroke around each letter - the letter interiors survive even
    # when they're the same brightness as the background.
    if path in _logo_cache:
        return _logo_cache[path]

    img = Image.open(path).convert("RGB")
    w, h = img.size
    sentinel = (1, 2, 3)
    work = img.copy()
    seeds = [(2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3),
             (w // 2, 2), (2, h // 2), (w - 3, h // 2), (w // 2, h - 3)]
    for seed in seeds:
        if work.getpixel(seed) != sentinel:
            ImageDraw.floodfill(work, seed, sentinel, thresh=18)

    import numpy as np
    orig_arr = np.array(img)
    work_arr = np.array(work)
    mask = np.all(work_arr == sentinel, axis=2)
    out_arr = np.dstack([orig_arr, np.where(mask, 0, 255).astype(np.uint8)])
    result = Image.fromarray(out_arr, "RGBA")
    _logo_cache[path] = result
    return result


def _geometric_bull_silhouette(size=(520, 660), color=(55, 120, 100, 255)):
    # We don't have the original hand-painted bull artwork, so this is a
    # simple geometric approximation (horns + head) drawn from scratch as a
    # subtle decorative accent - not meant to be a pixel match.
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    w, h = size
    cx, cy = w * 0.52, h * 0.50

    def horn(base_x, base_y, tip_x, tip_y, base_w, tip_w, curve):
        n = 24
        mid_x = (base_x + tip_x) / 2 + curve
        mid_y = (base_y + tip_y) / 2
        left, right = [], []
        for i in range(n + 1):
            t = i / n
            x = (1 - t) ** 2 * base_x + 2 * (1 - t) * t * mid_x + t ** 2 * tip_x
            y = (1 - t) ** 2 * base_y + 2 * (1 - t) * t * mid_y + t ** 2 * tip_y
            width = base_w * (1 - t) + tip_w * t
            dx, dy = tip_x - base_x, tip_y - base_y
            length = math.hypot(dx, dy) or 1
            nx, ny = -dy / length, dx / length
            left.append((x + nx * width, y + ny * width))
            right.append((x - nx * width, y - ny * width))
        draw.polygon(left + right[::-1], fill=color)

    horn(cx - 60, cy - 40, cx - 220, cy - 260, 34, 6, -60)
    horn(cx + 60, cy - 40, cx + 220, cy - 260, 34, 6, 60)

    head = [
        (cx - 150, cy - 20), (cx - 130, cy + 60), (cx - 70, cy + 170),
        (cx, cy + 210), (cx + 70, cy + 170), (cx + 130, cy + 60),
        (cx + 150, cy - 20), (cx + 90, cy - 90), (cx, cy - 110), (cx - 90, cy - 90),
    ]
    draw.polygon(head, fill=color)
    draw.polygon([(cx - 150, cy - 40), (cx - 210, cy - 10), (cx - 150, cy + 10)], fill=color)
    draw.polygon([(cx + 150, cy - 40), (cx + 210, cy - 10), (cx + 150, cy + 10)], fill=color)
    return img


def _hex_to_rgb(hex_str):
    hex_str = hex_str.lstrip("#")
    return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))


def _format_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def _parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class ClientApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("640x780")
        self.configure(bg=COLORS["bg"])
        self._apply_style()

        self.cfg = {**DEFAULT_CFG, **load_json(CONFIG_PATH, DEFAULT_CFG)}
        # Always the hardcoded SERVER_URL, never whatever an older build saved
        # locally - otherwise a client who registered before the last ngrok
        # restart would keep silently pointing at a dead tunnel forever.
        self.cfg["server_url"] = SERVER_URL
        self.engine = None
        self.me = None
        self.packages = []
        self._screen_token = 0
        self._pending_update_path = None

        self._build_header()
        self._build_update_bar()
        self.body = tk.Frame(self, bg=COLORS["bg"])
        self.body.pack(fill="both", expand=True)

        self._route_initial()
        self.after(1500, self._check_for_update)

    # ---------------------------------------------------------- styling
    def _apply_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TButton", background=COLORS["panel"], foreground=COLORS["text"],
                         borderwidth=0, padding=8)
        style.map("TButton", background=[("active", COLORS["bg2"])])
        style.configure("Accent.TButton", background=COLORS["accent2"], foreground="#ffffff",
                         borderwidth=0, padding=8)
        style.map("Accent.TButton", background=[("active", COLORS["accent"])])
        style.configure("TCombobox", fieldbackground=COLORS["bg2"], background=COLORS["bg2"],
                         foreground=COLORS["text"])

    def _build_header(self):
        frm = tk.Frame(self, bg=COLORS["bg"])
        frm.pack(fill="x")
        logo_widget = self._make_logo_label(frm)
        if logo_widget is not None:
            logo_widget.pack(side="left", padx=14, pady=10)
        else:
            tk.Label(frm, text="LUMORA SCALPING", bg=COLORS["bg"], fg=COLORS["accent"],
                     font=("Segoe UI", 16, "bold")).pack(side="left", padx=14, pady=10)
        self._scan_canvas = tk.Canvas(frm, height=32, bg=COLORS["bg"], highlightthickness=0)
        self._scan_canvas.pack(side="right", fill="x", expand=True, padx=14)
        self._scan_x = 0
        self._animate_scan()

    def _make_logo_label(self, parent):
        # Real LUMORA logo in the header, falling back to plain text if PIL/
        # the asset aren't available for any reason (must never break the
        # rest of the app over a purely cosmetic image).
        if Image is None or not (LUMORA_LOGO_PATH and os.path.exists(LUMORA_LOGO_PATH)):
            return None
        try:
            logo = _load_logo_with_transparent_bg(LUMORA_LOGO_PATH).copy()
            logo.thumbnail((220, 48))
            flat = Image.new("RGB", logo.size, _hex_to_rgb(COLORS["bg"]))
            flat.paste(logo, (0, 0), logo)
            self._header_logo_img = ImageTk.PhotoImage(flat)
            return tk.Label(parent, image=self._header_logo_img, bg=COLORS["bg"])
        except Exception:
            return None

    def _animate_scan(self):
        # Lightweight "AI scanning the market" flourish: a few glowing dots
        # sweeping left to right. Pure Canvas/after() - no image/GIF
        # dependencies, so it can't reintroduce PyInstaller bundling issues.
        c = self._scan_canvas
        c.delete("scan")
        w = max(c.winfo_width(), 80)
        self._scan_x = (self._scan_x + 5) % (w + 30)
        c.create_line(0, 16, w, 16, fill=COLORS["panel"], tags="scan")
        for i, dx in enumerate((0, 9, 18, 27)):
            x = self._scan_x - dx
            if 0 <= x <= w:
                r = 4 - i * 0.7
                color = COLORS["accent"] if i == 0 else COLORS["accent2"]
                c.create_oval(x - r, 16 - r, x + r, 16 + r, fill=color, outline="", tags="scan")
        self.after(40, self._animate_scan)

    # ---------------------------------------------------------- auto-update
    # Auto-download + manual restart, not silent: the app never replaces
    # itself while running unattended. It downloads the new build in the
    # background, then only swaps it in when the client clicks "Restart to
    # Update", closing and relaunching itself at that moment.
    def _build_update_bar(self):
        self._update_bar = tk.Frame(self, bg=COLORS["accent2"])
        self._update_lbl = tk.Label(self._update_bar, text="", bg=COLORS["accent2"], fg="#ffffff")
        self._update_lbl.pack(side="left", padx=12, pady=6)
        self._update_btn = ttk.Button(self._update_bar, text="", command=lambda: None)
        # Not packed yet - _show_update_bar() packs both bar and button on demand.

    def _show_update_bar(self, text, button_text=None, button_cmd=None):
        self._update_lbl.config(text=text)
        if button_text:
            self._update_btn.config(text=button_text, command=button_cmd)
            self._update_btn.pack(side="right", padx=12, pady=4)
        else:
            self._update_btn.pack_forget()
        self._update_bar.pack(fill="x", before=self.body)

    def _check_for_update(self):
        def fn():
            return api_call(self.cfg["server_url"], self.cfg.get("client_id", ""),
                             self.cfg.get("client_key", ""), "/api/client-version")

        def done(info):
            if _parse_version(info.get("version", "0")) <= _parse_version(CLIENT_VERSION):
                return  # already up to date
            if not getattr(sys, "frozen", False):
                # Running from source (python client_app.py) - nothing to
                # replace. Just say so; a dev should rebuild the exe instead.
                self._show_update_bar(f"Update v{info['version']} available (rebuild the .exe to get it).")
                return
            self._show_update_bar(f"Downloading update v{info['version']}…")
            self._start_update_download(info["download_url"])

        def err(_e):
            pass  # update checks are silent on failure - never nag about connectivity

        self._run_async(fn, done, err)

    def _start_update_download(self, download_url):
        def fn():
            url = download_url if download_url.startswith("http") else self.cfg["server_url"].rstrip("/") + download_url
            dest = os.path.join(tempfile.gettempdir(), f"LumoraScalpingClient-update-{int(time.time())}.exe")
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
                shutil.copyfileobj(resp, f)
            return dest

        def done(dest):
            self._pending_update_path = dest
            self._show_update_bar("Update ready.", "Restart to Update", self._apply_update_and_restart)

        def err(e):
            self._show_update_bar(f"Update download failed: {e}")

        self._run_async(fn, done, err)

    def _apply_update_and_restart(self):
        # Hard safety gate: this must be unreachable unless (a) we are
        # actually the packaged .exe - never the raw python.exe interpreter -
        # and (b) the client explicitly confirms, so a stray/duplicate click
        # (or a bug) can never silently rename a running interpreter/app.
        if not getattr(sys, "frozen", False):
            messagebox.showerror("Update not available",
                                  "Auto-update only works in the packaged .exe, not when running from source.")
            return
        if not self._pending_update_path or not os.path.exists(self._pending_update_path):
            return
        if not messagebox.askyesno("Restart to update?",
                                    "Lumora Scalping will close and reopen on the new version. Continue?"):
            return

        current_exe = sys.executable
        try:
            # Launch the downloaded build from its own staged path rather than
            # overwriting current_exe first - see _finish_pending_update for why.
            subprocess.Popen(
                [self._pending_update_path, "--finish-update", current_exe],
                env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
            )
        except OSError as e:
            messagebox.showerror(
                "Update failed",
                f"Couldn't launch the new version: {e}\n\n"
                "This usually means the app is installed somewhere that needs "
                "admin rights (e.g. Program Files) - try running it from a "
                "regular folder like Desktop instead.",
            )
            return

        self.destroy()
        sys.exit(0)

    # ---------------------------------------------------------- layout helpers
    def _clear_body(self):
        self._screen_token += 1
        for w in self.body.winfo_children():
            w.destroy()

    def _card(self):
        outer = tk.Frame(self.body, bg=COLORS["bg"])
        outer.pack(fill="both", expand=True, padx=20, pady=16)
        card = tk.Frame(outer, bg=COLORS["panel"], padx=24, pady=24)
        card.pack(fill="x")
        return card

    def _field(self, parent, label, var, show=None, bg=None):
        bg = bg or COLORS["panel"]
        row = tk.Frame(parent, bg=bg)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, bg=bg, fg=COLORS["muted"], width=28, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=var, show=show or "", bg=COLORS["bg2"], fg=COLORS["text"],
                 insertbackground=COLORS["text"], relief="flat").pack(side="left", fill="x", expand=True, ipady=4)

    def _run_async(self, fn, on_done=None, on_error=None):
        def worker():
            try:
                result = fn()
            except Exception as e:
                # Python deletes the "as e" name at the end of this except
                # block, but the lambda below only runs later (via after()),
                # so it must close over a plain variable, not the except name,
                # or it raises NameError when the mainloop finally calls it.
                error = e
                if on_error:
                    self.after(0, lambda: on_error(error))
                return
            if on_done:
                self.after(0, lambda: on_done(result))
        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------- routing
    def _route_initial(self):
        if not self.cfg.get("server_url") or not self.cfg.get("client_id"):
            self._show_onboarding()
            return
        self._refresh_me(self._route_from_status)

    def _refresh_me(self, on_done):
        def fn():
            return api_call(self.cfg["server_url"], self.cfg["client_id"], self.cfg["client_key"], "/api/client/me")

        def done(result):
            self.me = result
            on_done()

        def err(e):
            # Only a genuine credential rejection means "this account doesn't
            # exist" - anything else (server down, ngrok tunnel restarting,
            # no internet) must NOT bounce an existing client into Register,
            # which would create a duplicate account instead of just retrying.
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                self.me = None
                on_done()
            else:
                self._show_connection_error(str(e))

        self._run_async(fn, done, err)

    def _show_connection_error(self, message):
        self._clear_body()
        card = self._card()
        tk.Label(card, text="Couldn't reach the server", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 10))
        tk.Label(card, text="Your account is fine - this is just a connection problem. "
                             f"Details: {message}",
                 bg=COLORS["panel"], fg=COLORS["muted"], wraplength=460, justify="left").pack(anchor="w")
        ttk.Button(card, text="Retry", style="Accent.TButton",
                   command=self._route_initial).pack(anchor="e", pady=(14, 0))

    def _route_from_status(self):
        if self.me is None:
            self._show_onboarding()
            return
        status = self.me.get("status", "ACTIVE")
        if status in NEEDS_PAYMENT_STATUSES:
            self._show_package_payment()
        elif status == "PENDING_APPROVAL":
            self._show_waiting()
        else:
            self._show_dashboard()

    # ---------------------------------------------------------- onboarding
    def _show_onboarding(self):
        self._clear_body()
        card = self._card()
        tk.Label(card, text="Create your account", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 10))

        self._name_var = tk.StringVar()
        self._email_var = tk.StringVar()
        self._password_var = tk.StringVar()
        self._password_confirm_var = tk.StringVar()
        self._login_var = tk.StringVar()

        self._field(card, "Full name", self._name_var)
        self._field(card, "Email", self._email_var)
        self._field(card, "Password (min 6 characters)", self._password_var, show="*")
        self._field(card, "Confirm password", self._password_confirm_var, show="*")
        self._field(card, "Your MT5 login (for reference)", self._login_var)

        self._error_lbl = tk.Label(card, text="", bg=COLORS["panel"], fg=COLORS["bad"], wraplength=460, justify="left")
        self._error_lbl.pack(anchor="w", pady=(6, 0))

        btn_row = tk.Frame(card, bg=COLORS["panel"])
        btn_row.pack(fill="x", pady=(14, 0))
        ttk.Button(btn_row, text="Already have an account? Log In",
                   command=self._show_login).pack(side="left")
        ttk.Button(btn_row, text="Register", style="Accent.TButton",
                   command=self._submit_registration).pack(side="right")

    def _submit_registration(self):
        name = self._name_var.get().strip()
        email = self._email_var.get().strip()
        password = self._password_var.get()
        password_confirm = self._password_confirm_var.get()
        login = self._login_var.get().strip()
        if not name or not email:
            self._error_lbl.config(text="Name and email are required.")
            return
        if len(password) < 6:
            self._error_lbl.config(text="Password must be at least 6 characters.")
            return
        if password != password_confirm:
            self._error_lbl.config(text="Passwords don't match.")
            return

        def fn():
            return api_call(SERVER_URL, "", "", "/api/customer/register", method="POST",
                             body={"name": name, "email": email, "password": password, "mt5_login": login})

        def done(result):
            self.cfg["client_id"] = result["id"]
            self.cfg["client_key"] = result["api_key"]
            save_json(CONFIG_PATH, self.cfg)
            self._show_package_payment()

        def err(e):
            self._error_lbl.config(text=f"Registration failed: {e}")

        self._run_async(fn, done, err)

    # ---------------------------------------------------------- login (existing account)
    def _show_login(self):
        self._clear_body()
        card = self._card()
        tk.Label(card, text="Log in to your account", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 10))

        self._login_email_var = tk.StringVar()
        self._login_password_var = tk.StringVar()
        self._field(card, "Email", self._login_email_var)
        self._field(card, "Password", self._login_password_var, show="*")

        self._error_lbl = tk.Label(card, text="", bg=COLORS["panel"], fg=COLORS["bad"], wraplength=460, justify="left")
        self._error_lbl.pack(anchor="w", pady=(6, 0))

        btn_row = tk.Frame(card, bg=COLORS["panel"])
        btn_row.pack(fill="x", pady=(14, 0))
        ttk.Button(btn_row, text="New here? Register instead",
                   command=self._show_onboarding).pack(side="left")
        ttk.Button(btn_row, text="Log In", style="Accent.TButton",
                   command=self._submit_login).pack(side="right")

        tk.Label(card, text="Admin created your account without a password?",
                 bg=COLORS["panel"], fg=COLORS["muted"]).pack(anchor="w", pady=(10, 0))
        link = tk.Label(card, text="Log in with Client ID / Key instead →", bg=COLORS["panel"],
                         fg=COLORS["accent"], cursor="hand2")
        link.pack(anchor="w")
        link.bind("<Button-1>", lambda _e: self._show_login_with_id_key())

    def _submit_login(self):
        email = self._login_email_var.get().strip()
        password = self._login_password_var.get()
        if not email or not password:
            self._error_lbl.config(text="Email and password are both required.")
            return

        def fn():
            return api_call(SERVER_URL, "", "", "/api/customer/login", method="POST",
                             body={"email": email, "password": password})

        def done(result):
            self.cfg["client_id"] = result["id"]
            self.cfg["client_key"] = result["api_key"]
            save_json(CONFIG_PATH, self.cfg)
            self.me = result
            self._route_from_status()

        def err(e):
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                self._error_lbl.config(text="Email or password is incorrect.")
            else:
                self._error_lbl.config(text=f"Could not log in: {e}")

        self._run_async(fn, done, err)

    # Fallback for admin-created accounts that were never given a password.
    def _show_login_with_id_key(self):
        self._clear_body()
        card = self._card()
        tk.Label(card, text="Log in with Client ID / Key", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 10))
        tk.Label(card, text="Enter the Client ID and Client Key the admin gave you when your "
                             "account was approved.",
                 bg=COLORS["panel"], fg=COLORS["muted"], wraplength=460, justify="left").pack(anchor="w", pady=(0, 10))

        self._login_id_var = tk.StringVar()
        self._login_key_var = tk.StringVar()
        self._field(card, "Client ID", self._login_id_var)
        self._field(card, "Client Key", self._login_key_var, show="*")

        self._error_lbl = tk.Label(card, text="", bg=COLORS["panel"], fg=COLORS["bad"], wraplength=460, justify="left")
        self._error_lbl.pack(anchor="w", pady=(6, 0))

        btn_row = tk.Frame(card, bg=COLORS["panel"])
        btn_row.pack(fill="x", pady=(14, 0))
        ttk.Button(btn_row, text="← Back to email login",
                   command=self._show_login).pack(side="left")
        ttk.Button(btn_row, text="Log In", style="Accent.TButton",
                   command=self._submit_login_with_id_key).pack(side="right")

    def _submit_login_with_id_key(self):
        client_id = self._login_id_var.get().strip()
        client_key = self._login_key_var.get().strip()
        if not client_id or not client_key:
            self._error_lbl.config(text="Client ID and Client Key are both required.")
            return

        def fn():
            return api_call(SERVER_URL, client_id, client_key, "/api/client/me")

        def done(result):
            self.cfg["client_id"] = client_id
            self.cfg["client_key"] = client_key
            save_json(CONFIG_PATH, self.cfg)
            self.me = result
            self._route_from_status()

        def err(e):
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                self._error_lbl.config(text="Client ID or Client Key is incorrect.")
            else:
                self._error_lbl.config(text=f"Could not log in: {e}")

        self._run_async(fn, done, err)

    # ---------------------------------------------------------- package + payment
    def _show_package_payment(self, back_to=None):
        self._clear_body()
        self._payment_back_to = back_to
        card = self._card()
        if back_to:
            ttk.Button(card, text="← Back", command=back_to).pack(anchor="w", pady=(0, 10))
        tk.Label(card, text="Choose a package & submit payment", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 10))

        status = (self.me or {}).get("status")
        if status in ("EXPIRED", "HALTED"):
            tk.Label(card, text=f"Your subscription is {status.lower()}. Submit a new payment to reactivate.",
                     bg=COLORS["panel"], fg=COLORS["warn"], wraplength=460, justify="left").pack(anchor="w", pady=(0, 10))

        self._package_var = tk.StringVar()
        self._packages_frame = tk.Frame(card, bg=COLORS["panel"])
        self._packages_frame.pack(fill="x", pady=6)
        tk.Label(self._packages_frame, text="Loading packages…", bg=COLORS["panel"],
                 fg=COLORS["muted"]).pack(anchor="w")

        tk.Label(card, text="How to pay", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(12, 2))
        self._payment_methods_frame = tk.Frame(card, bg=COLORS["panel"])
        self._payment_methods_frame.pack(fill="x", pady=(0, 6))
        tk.Label(self._payment_methods_frame, text="Loading payment methods…", bg=COLORS["panel"],
                 fg=COLORS["muted"]).pack(anchor="w")
        self._load_payment_methods()

        tk.Label(card, text="Once you've paid using one of the methods above, enter your reference below:",
                 bg=COLORS["panel"], fg=COLORS["muted"], wraplength=460, justify="left").pack(anchor="w", pady=(6, 0))
        self._reference_var = tk.StringVar()
        self._note_var = tk.StringVar()
        self._field(card, "Payment reference / transaction ID", self._reference_var)
        self._field(card, "Note (optional)", self._note_var)

        self._error_lbl = tk.Label(card, text="", bg=COLORS["panel"], fg=COLORS["bad"], wraplength=460, justify="left")
        self._error_lbl.pack(anchor="w", pady=(6, 0))

        ttk.Button(card, text="Submit Payment", style="Accent.TButton",
                   command=self._submit_payment).pack(anchor="e", pady=(14, 0))

        self._load_packages()

    def _load_packages(self):
        def fn():
            return api_call(self.cfg["server_url"], self.cfg.get("client_id", ""),
                             self.cfg.get("client_key", ""), "/api/packages")

        def done(packages):
            self.packages = packages
            for w in self._packages_frame.winfo_children():
                w.destroy()
            for pkg in packages:
                tk.Radiobutton(
                    self._packages_frame,
                    text=f"{pkg['name']} — {pkg['currency']} {pkg['price']}/mo — {pkg['description']}",
                    variable=self._package_var, value=pkg["id"],
                    bg=COLORS["panel"], fg=COLORS["text"], selectcolor=COLORS["bg2"],
                    activebackground=COLORS["panel"], activeforeground=COLORS["accent"],
                    anchor="w", justify="left", wraplength=460,
                ).pack(anchor="w", pady=2)
            if packages:
                self._package_var.set(packages[0]["id"])

        def err(e):
            for w in self._packages_frame.winfo_children():
                w.destroy()
            tk.Label(self._packages_frame, text=f"Could not load packages: {e}",
                     bg=COLORS["panel"], fg=COLORS["bad"]).pack(anchor="w")

        self._run_async(fn, done, err)

    def _load_payment_methods(self):
        def fn():
            return api_call(self.cfg["server_url"], self.cfg.get("client_id", ""),
                             self.cfg.get("client_key", ""), "/api/payment-methods")

        def done(methods):
            for w in self._payment_methods_frame.winfo_children():
                w.destroy()
            if not methods:
                tk.Label(self._payment_methods_frame,
                         text="No payment methods are configured yet — contact the admin for instructions.",
                         bg=COLORS["panel"], fg=COLORS["muted"], wraplength=460, justify="left").pack(anchor="w")
                return
            for m in methods:
                box = tk.Frame(self._payment_methods_frame, bg=COLORS["bg2"], padx=10, pady=8)
                box.pack(fill="x", pady=4)
                tk.Label(box, text=m["label"], bg=COLORS["bg2"], fg=COLORS["accent"],
                         font=("Segoe UI", 10, "bold")).pack(anchor="w")
                details = m.get("details", "")
                # A disabled Text widget still lets the client select/copy the
                # wallet address or bank details - a Label wouldn't.
                text_box = tk.Text(box, height=max(1, details.count("\n") + 1), bg=COLORS["bg2"],
                                    fg=COLORS["text"], relief="flat", wrap="word", bd=0,
                                    highlightthickness=0)
                text_box.insert("1.0", details)
                text_box.config(state="disabled")
                text_box.pack(fill="x", pady=(4, 0))

        def err(e):
            for w in self._payment_methods_frame.winfo_children():
                w.destroy()
            tk.Label(self._payment_methods_frame, text=f"Could not load payment methods: {e}",
                     bg=COLORS["panel"], fg=COLORS["bad"]).pack(anchor="w")

        self._run_async(fn, done, err)

        self._run_async(fn, done, err)

    def _submit_payment(self):
        package_id = self._package_var.get()
        reference = self._reference_var.get().strip()
        if not package_id or not reference:
            self._error_lbl.config(text="Pick a package and enter your payment reference.")
            return

        def fn():
            return api_call(self.cfg["server_url"], self.cfg["client_id"], self.cfg["client_key"],
                             "/api/customer/payment", method="POST",
                             body={"package_id": package_id, "reference": reference,
                                   "note": self._note_var.get().strip()})

        def done(_result):
            self._show_waiting(back_to=self._payment_back_to)

        def err(e):
            self._error_lbl.config(text=f"Could not submit payment: {e}")

        self._run_async(fn, done, err)

    # ---------------------------------------------------------- waiting for approval
    def _show_waiting(self, back_to=None):
        self._clear_body()
        self._waiting_back_to = back_to
        token = self._screen_token
        card = self._card()
        ttk.Button(card, text="← Back", command=lambda: self._show_package_payment(back_to=back_to)
                   ).pack(anchor="w", pady=(0, 10))
        tk.Label(card, text="Payment submitted", bg=COLORS["panel"], fg=COLORS["text"],
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(0, 10))
        tk.Label(card, text="Your payment is under review. This screen updates automatically once "
                            "an admin approves it — no need to reopen the app.",
                 bg=COLORS["panel"], fg=COLORS["muted"], wraplength=460, justify="left").pack(anchor="w")
        self._waiting_status_lbl = tk.Label(card, text="Checking status…", bg=COLORS["panel"], fg=COLORS["accent"])
        self._waiting_status_lbl.pack(anchor="w", pady=(14, 0))
        self._poll_status(token)

    def _poll_status(self, token):
        if token != self._screen_token:
            return

        def fn():
            return api_call(self.cfg["server_url"], self.cfg["client_id"], self.cfg["client_key"], "/api/client/me")

        def done(result):
            if token != self._screen_token:
                return
            self.me = result
            status = result.get("status")
            if status == "ACTIVE":
                self._show_dashboard()
                return
            if status in NEEDS_PAYMENT_STATUSES:
                self._show_package_payment(back_to=self._waiting_back_to)
                return
            self._waiting_status_lbl.config(text=f"Status: {status}")
            self.after(5000, lambda: self._poll_status(token))

        def err(e):
            if token != self._screen_token:
                return
            self._waiting_status_lbl.config(text=f"Could not check status: {e}")
            self.after(5000, lambda: self._poll_status(token))

        self._run_async(fn, done, err)

    # ---------------------------------------------------------- dashboard
    def _show_dashboard(self):
        self._clear_body()
        token = self._screen_token
        outer = tk.Frame(self.body, bg=COLORS["bg"])
        outer.pack(fill="both", expand=True, padx=16, pady=(6, 16))

        self._build_subscription_banner(outer)
        self._build_symbol_form(outer)
        self._build_controls(outer)
        self._build_status(outer)
        self._build_log(outer)

        if mt5 is None:
            self.log(f"MetaTrader5 package failed to load: {MT5_IMPORT_ERROR}")

        self._start_subscription_watch(token)

    def _build_subscription_banner(self, parent):
        me = self.me or {}
        pkg = me.get("package") or {}
        days_left = me.get("days_left")
        banner = tk.Frame(parent, bg=COLORS["panel"], padx=12, pady=8)
        banner.pack(fill="x", pady=(0, 10))
        text = f"Plan: {pkg.get('name', '—')}"
        color = COLORS["good"]
        if days_left is not None:
            text += f"   •   {days_left} day{'s' if days_left != 1 else ''} left"
            if days_left <= 0:
                color = COLORS["bad"]
            elif days_left <= 7:
                color = COLORS["warn"]
        tk.Label(banner, text=text, bg=COLORS["panel"], fg=color).pack(side="left")
        ttk.Button(banner, text="Renew / Manage Plan",
                   command=lambda: self._show_package_payment(back_to=self._show_dashboard)).pack(side="right")

    def _build_symbol_form(self, parent):
        # No lot-size or risk controls here on purpose: trades always mirror
        # the admin's exact volume (see CopierEngine.compute_lot) - the
        # symbol is the only thing that needs to be told apart per-broker.
        frm = tk.LabelFrame(parent, text="Trading", bg=COLORS["bg"], fg=COLORS["accent"],
                             padx=10, pady=8, bd=1, highlightbackground=COLORS["panel"])
        frm.pack(fill="x", pady=4)

        self.symbol_var = tk.StringVar(value=self.cfg["symbol"])
        row = tk.Frame(frm, bg=COLORS["bg"])
        row.pack(fill="x", pady=4)
        tk.Label(row, text="Your broker's Gold symbol", bg=COLORS["bg"], fg=COLORS["muted"],
                 width=28, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=self.symbol_var, bg=COLORS["bg2"], fg=COLORS["text"],
                 insertbackground=COLORS["text"], relief="flat").pack(side="left", fill="x", expand=True, ipady=4)
        ttk.Button(row, text="Detect", command=self._detect_gold_symbol).pack(side="left", padx=(6, 0))

        tk.Label(frm, text="Whatever YOUR broker calls gold (e.g. XAUUSD, XAUUSDm, GOLD) - it does not "
                            "need to match the admin's broker's name for it. Trade size always mirrors "
                            "the admin's exact volume.",
                 bg=COLORS["bg"], fg=COLORS["muted"], wraplength=460, justify="left").pack(anchor="w", pady=(4, 0))

    def _detect_gold_symbol(self):
        if mt5 is None:
            messagebox.showerror("MetaTrader5 not available", str(MT5_IMPORT_ERROR))
            return
        if not mt5.initialize():
            messagebox.showerror("MT5 connection failed",
                                  f"mt5.initialize() failed: {mt5.last_error()}. "
                                  "Make sure MT5 is open and logged in.")
            return

        symbols = mt5.symbols_get()
        if not symbols:
            messagebox.showwarning("No symbols found",
                                    "Could not read any symbols from MT5. Make sure MT5 is open and logged in.")
            return

        matches = sorted({s.name for s in symbols if "XAU" in s.name.upper() or "GOLD" in s.name.upper()})
        if not matches:
            messagebox.showwarning("No gold symbol found",
                                    "Could not find a Gold symbol in your broker's symbol list. "
                                    "Check Market Watch in MT5, or type it in manually.")
            return

        if len(matches) == 1:
            self.symbol_var.set(matches[0])
            messagebox.showinfo("Symbol detected", f"Found: {matches[0]}")
            return

        choice = self._ask_choice(
            "Multiple gold symbols found",
            "Your broker has more than one - pick the one matching what the admin trades:",
            matches,
        )
        if choice:
            self.symbol_var.set(choice)

    def _ask_choice(self, title, prompt, options):
        dlg = tk.Toplevel(self, bg=COLORS["panel"])
        dlg.title(title)
        dlg.configure(bg=COLORS["panel"])
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text=prompt, bg=COLORS["panel"], fg=COLORS["text"],
                 wraplength=320, justify="left").pack(padx=12, pady=(12, 6), anchor="w")
        listbox = tk.Listbox(dlg, bg=COLORS["bg2"], fg=COLORS["text"],
                              selectbackground=COLORS["accent2"], height=min(8, len(options)))
        for opt in options:
            listbox.insert("end", opt)
        listbox.selection_set(0)
        listbox.pack(padx=12, fill="both", expand=True)

        result = {}

        def submit():
            sel = listbox.curselection()
            if sel:
                result["value"] = listbox.get(sel[0])
            dlg.destroy()

        btn_bar = tk.Frame(dlg, bg=COLORS["panel"])
        btn_bar.pack(fill="x", padx=12, pady=(8, 12))
        ttk.Button(btn_bar, text="Select", style="Accent.TButton", command=submit).pack(side="right")
        ttk.Button(btn_bar, text="Cancel", command=dlg.destroy).pack(side="right", padx=6)

        dlg.wait_window()
        return result.get("value")

    def _build_controls(self, parent):
        frm = tk.Frame(parent, bg=COLORS["bg"])
        frm.pack(fill="x", pady=4)
        ttk.Button(frm, text="Save Settings", command=self.save_settings).pack(side="left")
        self.start_btn = ttk.Button(frm, text="Start Copying", style="Accent.TButton", command=self.toggle_engine)
        self.start_btn.pack(side="left", padx=6)

    def _build_status(self, parent):
        box = tk.LabelFrame(parent, text="Live Status", bg=COLORS["bg"], fg=COLORS["accent"],
                             padx=10, pady=8, bd=1, highlightbackground=COLORS["panel"])
        box.pack(fill="x", pady=4)
        self.status_lbl = tk.Label(box, text="Not connected", bg=COLORS["bg"], fg=COLORS["muted"])
        self.status_lbl.pack(anchor="w")
        self.vals = {}
        for key, label in [("balance", "Balance"), ("equity", "Equity"),
                            ("today", "Today's Realized P/L"), ("total", "Total Realized P/L"),
                            ("trades", "Closed Trades Copied")]:
            row = tk.Frame(box, bg=COLORS["bg"])
            row.pack(fill="x")
            tk.Label(row, text=label + ":", bg=COLORS["bg"], fg=COLORS["muted"], width=22, anchor="w").pack(side="left")
            v = tk.Label(row, text="-", bg=COLORS["bg"], fg=COLORS["text"])
            v.pack(side="left")
            self.vals[key] = v

        ttk.Button(box, text="📥 Download Profit Card",
                   command=self._download_profit_card).pack(anchor="w", pady=(8, 0))

    def _build_log(self, parent):
        box = tk.LabelFrame(parent, text="Activity Log", bg=COLORS["bg"], fg=COLORS["accent"],
                             padx=10, pady=8, bd=1, highlightbackground=COLORS["panel"])
        box.pack(fill="both", expand=True, pady=(4, 0))
        self.log_box = scrolledtext.ScrolledText(box, height=10, state="disabled", wrap="word",
                                                  bg=COLORS["bg2"], fg=COLORS["text"],
                                                  insertbackground=COLORS["text"], relief="flat")
        self.log_box.pack(fill="both", expand=True)

    # ---------------------------------------------------------- profit card
    def _download_profit_card(self):
        if Image is None:
            messagebox.showerror("Not available", f"Profit card feature failed to load: {CARD_IMPORT_ERROR}")
            return

        def fn():
            return api_call(self.cfg["server_url"], self.cfg["client_id"], self.cfg["client_key"],
                             "/api/client/last-closed-trade")

        def done(trade):
            if not trade:
                messagebox.showinfo("No closed trades yet", "You don't have any closed trades to share yet.")
                return
            try:
                img = self._build_profit_card_image(trade)
            except Exception as e:
                messagebox.showerror("Could not build card", str(e))
                return
            path = filedialog.asksaveasfilename(
                defaultextension=".png", filetypes=[("PNG image", "*.png")],
                initialfile=f"lumora-profit-{trade.get('client_ticket', 'trade')}.png",
            )
            if path:
                img.save(path)
                self.log(f"Profit card saved to {path}")

        def err(e):
            messagebox.showerror("Could not fetch trade", str(e))

        self._run_async(fn, done, err)

    def _build_profit_card_image(self, trade):
        W, H = 1024, 1360
        BG = (11, 15, 26)
        BORDER = (58, 90, 200)
        TEXT = (230, 236, 255)
        MUTED = (130, 140, 165)
        GOOD = (40, 220, 150)
        BAD = (255, 80, 100)
        ACCENT = (0, 200, 255)

        def font(name, size):
            try:
                return ImageFont.truetype(rf"C:\Windows\Fonts\{name}", size)
            except Exception:
                return ImageFont.load_default()

        f_logo = font("segoeuib.ttf", 64)
        f_tagline = font("segoeuil.ttf", 18)
        f_corner = font("segoeuil.ttf", 20)
        f_name = font("segoeuib.ttf", 30)
        f_date = font("segoeui.ttf", 20)
        f_symbol = font("segoeuib.ttf", 56)
        f_badge = font("segoeuib.ttf", 30)
        f_profit = font("segoeuib.ttf", 84)
        f_profit_unit = font("segoeuib.ttf", 34)
        f_pct = font("segoeuib.ttf", 30)
        f_label = font("segoeui.ttf", 20)
        f_value = font("segoeuib.ttf", 30)
        f_footer_title = font("segoeuib.ttf", 26)
        f_footer_sub = font("segoeui.ttf", 18)
        f_footer_caption = font("segoeui.ttf", 16)
        f_url = font("segoeui.ttf", 18)

        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img)
        pad = 60
        draw.rounded_rectangle([16, 16, W - 16, H - 16], radius=28, outline=BORDER, width=3)

        bull = _geometric_bull_silhouette()
        img.paste(bull, (W - bull.width + 60, 140), bull)

        if LUMORA_LOGO_PATH and os.path.exists(LUMORA_LOGO_PATH):
            try:
                logo = _load_logo_with_transparent_bg(LUMORA_LOGO_PATH).copy()
                logo.thumbnail((360, 100))
                img.paste(logo, (pad, 55), logo)
            except Exception:
                draw.text((pad, 60), "LUMORA", font=f_logo, fill=TEXT)
        else:
            draw.text((pad, 60), "LUMORA", font=f_logo, fill=TEXT)
        draw.text((pad + 2, 130), "T R A D E   S M A R T E R   T O G E T H E R", font=f_tagline, fill=MUTED)

        def right_text(x_right, y, text, fnt, fill):
            bbox = draw.textbbox((0, 0), text, font=fnt)
            draw.text((x_right - (bbox[2] - bbox[0]), y), text, font=fnt, fill=fill)

        right_text(W - pad, 55, "DISCIPLINE", f_corner, MUTED)
        right_text(W - pad, 82, "BUILDS", f_corner, MUTED)
        right_text(W - pad, 109, "WEALTH", f_corner, MUTED)

        client_name = (self.me or {}).get("name") or "Client"
        initials = "".join(w[0] for w in client_name.split()[:2]).upper() or "?"
        opened = _parse_iso(trade["opened_at"]) if trade.get("opened_at") else None
        closed = _parse_iso(trade["at"])
        date_str = closed.strftime("%Y-%m-%d %H:%M:%S")

        ay = 260
        draw.ellipse([pad, ay, pad + 90, ay + 90], outline=BORDER, width=3)
        ib = draw.textbbox((0, 0), initials, font=f_name)
        draw.text((pad + 45 - (ib[2] - ib[0]) / 2, ay + 45 - (ib[3] - ib[1]) / 2 - ib[1]),
                   initials, font=f_name, fill=TEXT)
        draw.text((pad + 110, ay + 8), client_name, font=f_name, fill=TEXT)
        draw.text((pad + 110, ay + 48), date_str, font=f_date, fill=MUTED)

        y = ay + 150
        draw.text((pad, y), trade.get("symbol") or self.cfg["symbol"], font=f_symbol, fill=TEXT)

        y += 80
        trade_type = trade.get("type") or "BUY"
        is_buy = trade_type == "BUY"
        badge_color = GOOD if is_buy else BAD
        draw.text((pad, y), trade_type, font=f_badge, fill=badge_color)
        tb = draw.textbbox((0, 0), trade_type, font=f_badge)
        tw = tb[2] - tb[0]
        draw.text((pad + tw + 20, y + 2), "|", font=f_badge, fill=MUTED)
        volume = trade.get("volume") or 0
        draw.text((pad + tw + 45, y + 2), f"{volume:.2f} Lots", font=f_date, fill=TEXT)

        y += 70
        profit = trade.get("profit", 0)
        color = GOOD if profit >= 0 else BAD
        sign = "+" if profit >= 0 else ""
        profit_text = f"{sign}{profit:.2f}"
        draw.text((pad, y), profit_text, font=f_profit, fill=color)
        pb = draw.textbbox((0, 0), profit_text, font=f_profit)
        draw.text((pad + (pb[2] - pb[0]) + 16, y + 34), "USD", font=f_profit_unit, fill=color)

        y += 100
        margin = trade.get("margin")
        if margin:
            pct = profit / margin * 100
            draw.text((pad, y), f"{sign}{pct:.2f}%", font=f_pct, fill=color)

        y += 70
        col_w = (W - 2 * pad) / 2
        draw.text((pad, y), "Entry Price", font=f_label, fill=MUTED)
        draw.text((pad + col_w, y), "Exit Price", font=f_label, fill=MUTED)
        y += 32
        entry_price = trade.get("entry_price") or 0
        exit_price = trade.get("price") or 0
        draw.text((pad, y), f"{entry_price:.2f}", font=f_value, fill=TEXT)
        draw.text((pad + col_w, y), f"{exit_price:.2f}", font=f_value, fill=TEXT)

        y += 80
        col_w3 = (W - 2 * pad) / 3
        draw.text((pad, y), "Trade Type", font=f_label, fill=MUTED)
        draw.text((pad + col_w3, y), "Duration", font=f_label, fill=MUTED)
        draw.text((pad + col_w3 * 2, y), "Status", font=f_label, fill=MUTED)
        y += 32
        duration_str = _format_duration((closed - opened).total_seconds()) if opened else "-"
        draw.text((pad, y), "Market", font=f_value, fill=TEXT)
        draw.text((pad + col_w3, y), duration_str, font=f_value, fill=TEXT)
        draw.text((pad + col_w3 * 2, y), "Closed", font=f_value, fill=GOOD)

        y += 90
        draw.line([(pad, y), (W - pad, y)], fill=(40, 48, 70), width=2)

        y += 40
        draw.text((pad, y), "Trade with LUMORA", font=f_footer_title, fill=ACCENT)
        draw.text((pad, y + 36), "Trade Smarter Together", font=f_footer_sub, fill=MUTED)

        qr_img = qrcode.make(PROFIT_CARD_QR_URL).convert("RGB").resize((150, 150))
        qr_x = W - pad - 150
        img.paste(qr_img, (qr_x, y - 10))
        cap = "Scan & Join Lumora"
        cb = draw.textbbox((0, 0), cap, font=f_footer_caption)
        draw.text((qr_x + 75 - (cb[2] - cb[0]) / 2, y + 145), cap, font=f_footer_caption, fill=MUTED)

        y += 220
        draw.ellipse([pad, y, pad + 22, y + 22], outline=MUTED, width=2)
        draw.line([(pad, y + 11), (pad + 22, y + 11)], fill=MUTED, width=1)
        url_display = PROFIT_CARD_QR_URL.replace("https://", "").replace("http://", "").rstrip("/")
        draw.text((pad + 32, y), url_display, font=f_url, fill=MUTED)

        return img

    # ---------------------------------------------------------- actions
    def _collect_cfg(self):
        cfg = dict(self.cfg)
        cfg.update({
            "symbol": self.symbol_var.get().strip(),
            "copy_sl": True,
            "copy_tp": True,
            "copy_modifications": True,
            "max_slippage_points": 30,
            "magic_number": 7788990,
            "poll_seconds": 2,
        })
        return cfg

    def save_settings(self):
        self.cfg = self._collect_cfg()
        save_json(CONFIG_PATH, self.cfg)
        self.log("Settings saved.")

    def toggle_engine(self):
        if self.engine and self.engine.running:
            self.engine.stop()
            self.start_btn.config(text="Start Copying")
            return

        self.save_settings()
        self.engine = CopierEngine(self.cfg, self.log, lambda: None)
        try:
            acc = self.engine.connect_mt5()
        except Exception as e:
            messagebox.showerror("MT5 connection failed", str(e))
            return

        self.engine.start()
        self.start_btn.config(text="Stop Copying")
        self.status_lbl.config(text=f"🟢 Connected — MT5 login {acc.login} on {acc.server}", fg=COLORS["good"])
        self._start_status_loop()

    def log(self, msg):
        def _append():
            self.log_box.config(state="normal")
            self.log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.after(0, _append)

    def _start_status_loop(self):
        def loop():
            while self.engine and self.engine.running:
                try:
                    me = api_call(self.cfg["server_url"], self.cfg["client_id"], self.cfg["client_key"], "/api/client/me")
                    pnl = me.get("pnl", {})
                    self.after(0, lambda: (
                        self.vals["balance"].config(text=me.get("balance")),
                        self.vals["equity"].config(text=me.get("equity")),
                        self.vals["today"].config(text=pnl.get("todayProfit")),
                        self.vals["total"].config(text=pnl.get("totalProfit")),
                        self.vals["trades"].config(text=pnl.get("closedTrades")),
                    ))
                except Exception:
                    pass
                time.sleep(3)
        threading.Thread(target=loop, daemon=True).start()

    def _start_subscription_watch(self, token):
        # Periodically re-checks subscription status while the dashboard is
        # open, so an expiry (or an admin halt) bounces the client back to
        # the renewal screen without needing to restart the app.
        def loop():
            while token == self._screen_token:
                time.sleep(60)
                if token != self._screen_token:
                    return
                try:
                    me = api_call(self.cfg["server_url"], self.cfg["client_id"], self.cfg["client_key"], "/api/client/me")
                except Exception:
                    continue
                if token != self._screen_token:
                    return
                self.me = me
                if me.get("status") != "ACTIVE":
                    self.after(0, lambda: self._handle_subscription_lapsed(token))
                    return
        threading.Thread(target=loop, daemon=True).start()

    def _handle_subscription_lapsed(self, token):
        if token != self._screen_token:
            return
        if self.engine and self.engine.running:
            self.engine.stop()
        self.log("Subscription is no longer active — redirecting to renewal.")
        self._show_package_payment()

    def destroy(self):
        self._screen_token += 1  # stop any in-flight polling/watch loops
        if self.engine:
            self.engine.stop()
        super().destroy()


if __name__ == "__main__":
    _finish_pending_update()
    app = ClientApp()
    app.mainloop()
