"""
Hyperliquid Wallet Monitor - Backend Server
Monitors wallets on Hyperliquid DEX for new trades and position changes.
Uses WebSocket for real-time updates and REST API for position snapshots.
"""

import asyncio
import json
import time
import threading
import logging
import os
import sys
from datetime import datetime, timezone
from collections import defaultdict

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


import requests
import websockets
from flask import Flask, render_template, jsonify, request, send_from_directory

# ─── Configuration ────────────────────────────────────────────────────────
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"
POLL_INTERVAL = 5  # seconds between position polling
MAX_TRADE_HISTORY = 200  # max trades to keep per wallet

# ─── Logging Setup ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("HyperMonitor")

# ─── Flask App ────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", template_folder="templates")

# ─── Global State ─────────────────────────────────────────────────────────
monitored_wallets = {}       # address -> { label, added_at }
wallet_positions = {}        # address -> [positions]
wallet_trades = defaultdict(list)  # address -> [trades]
wallet_account_info = {}     # address -> { accountValue, margin, etc }
notifications = []           # global notification log
ws_clients = set()           # connected browser websocket clients
monitor_tasks = {}           # address -> asyncio task
lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════
# HYPERLIQUID API HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def fetch_clearinghouse_state(address: str) -> dict:
    """Fetch the full clearinghouse state for a wallet."""
    try:
        resp = requests.post(
            HYPERLIQUID_INFO_URL,
            json={"type": "clearinghouseState", "user": address},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Error fetching clearinghouse state for {address[:10]}...: {e}")
        return {}


def fetch_user_fills(address: str, limit: int = 50) -> list:
    """Fetch recent fills/trades for a wallet."""
    try:
        resp = requests.post(
            HYPERLIQUID_INFO_URL,
            json={"type": "userFills", "user": address},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        resp.raise_for_status()
        fills = resp.json()
        return fills[:limit] if isinstance(fills, list) else []
    except Exception as e:
        logger.error(f"Error fetching fills for {address[:10]}...: {e}")
        return []


def fetch_open_orders(address: str) -> list:
    """Fetch open orders for a wallet."""
    try:
        resp = requests.post(
            HYPERLIQUID_INFO_URL,
            json={"type": "openOrders", "user": address},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        resp.raise_for_status()
        return resp.json() if isinstance(resp.json(), list) else []
    except Exception as e:
        logger.error(f"Error fetching open orders for {address[:10]}...: {e}")
        return []


def parse_positions(state: dict) -> list:
    """Extract and format positions from clearinghouse state."""
    positions = []
    asset_positions = state.get("assetPositions", [])
    for ap in asset_positions:
        pos = ap.get("position", {})
        szi = float(pos.get("szi", 0))
        if szi == 0:
            continue

        entry_px = float(pos.get("entryPx", 0))
        position_value = abs(szi) * entry_px
        unrealized_pnl = float(pos.get("unrealizedPnl", 0))
        return_on_position = float(pos.get("returnOnEquity", 0))
        liquidation_px = pos.get("liquidationPx", None)
        leverage_val = pos.get("leverage", {})
        if isinstance(leverage_val, dict):
            leverage = leverage_val.get("value", "N/A")
            leverage_type = leverage_val.get("type", "cross")
        else:
            leverage = leverage_val
            leverage_type = "cross"

        positions.append({
            "coin": pos.get("coin", "UNKNOWN"),
            "side": "LONG" if szi > 0 else "SHORT",
            "size": abs(szi),
            "szi": szi,
            "entryPx": entry_px,
            "positionValue": round(position_value, 2),
            "unrealizedPnl": round(unrealized_pnl, 2),
            "returnOnEquity": round(return_on_position * 100, 2),
            "liquidationPx": liquidation_px,
            "leverage": leverage,
            "leverageType": leverage_type,
            "marginUsed": float(pos.get("marginUsed", 0)),
            "maxLeverage": pos.get("maxLeverage", None),
        })
    return positions


def parse_account_info(state: dict) -> dict:
    """Extract account-level info from clearinghouse state."""
    margin = state.get("marginSummary", {})
    cross = state.get("crossMarginSummary", {})
    return {
        "accountValue": float(margin.get("accountValue", 0)),
        "totalNtlPos": float(margin.get("totalNtlPos", 0)),
        "totalMarginUsed": float(margin.get("totalMarginUsed", 0)),
        "withdrawable": float(margin.get("withdrawable", 0)),
        "crossAccountValue": float(cross.get("accountValue", 0)),
        "crossTotalNtlPos": float(cross.get("totalNtlPos", 0)),
    }


def format_fill(fill: dict) -> dict:
    """Format a fill object for the frontend."""
    ts = fill.get("time", 0)
    if isinstance(ts, (int, float)):
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        time_str = str(ts)

    px = float(fill.get("px", 0))
    sz = float(fill.get("sz", 0))

    return {
        "coin": fill.get("coin", "UNKNOWN"),
        "side": "BUY" if fill.get("side") == "B" else "SELL",
        "price": px,
        "size": sz,
        "value": round(px * sz, 2),
        "time": time_str,
        "timestamp": ts,
        "dir": fill.get("dir", ""),
        "closedPnl": float(fill.get("closedPnl", 0)),
        "fee": float(fill.get("fee", 0)),
        "feeToken": fill.get("feeToken", "USDC"),
        "hash": fill.get("hash", ""),
        "oid": fill.get("oid", ""),
        "tid": fill.get("tid", ""),
        "startPosition": fill.get("startPosition", ""),
        "crossed": fill.get("crossed", False),
    }


# ═══════════════════════════════════════════════════════════════════════════
# WALLET MONITOR (POLLING + WEBSOCKET)
# ═══════════════════════════════════════════════════════════════════════════

class WalletMonitor:
    """Monitors a single wallet for position changes and new trades."""

    def __init__(self, address: str, label: str = ""):
        self.address = address.lower().strip()
        self.label = label or f"Wallet {address[:8]}"
        self.last_fill_ids = set()
        self.running = True
        self._ws = None

    async def start(self):
        """Start monitoring this wallet."""
        logger.info(f"[START] Starting monitor for {self.label} ({self.address[:10]}...)")

        # Initial snapshot
        await self._snapshot()

        # Run polling + WebSocket in parallel
        await asyncio.gather(
            self._poll_loop(),
            self._ws_loop(),
            return_exceptions=True
        )

    async def _snapshot(self):
        """Take initial snapshot of positions and recent trades."""
        loop = asyncio.get_event_loop()

        state = await loop.run_in_executor(None, fetch_clearinghouse_state, self.address)
        fills = await loop.run_in_executor(None, fetch_user_fills, self.address)

        if state:
            positions = parse_positions(state)
            account_info = parse_account_info(state)
            with lock:
                wallet_positions[self.address] = positions
                wallet_account_info[self.address] = account_info

            if positions:
                logger.info(f"[POS] {self.label}: {len(positions)} open position(s)")

        if fills:
            formatted = [format_fill(f) for f in fills]
            with lock:
                wallet_trades[self.address] = formatted[:MAX_TRADE_HISTORY]
                self.last_fill_ids = {f.get("tid", f.get("hash", "")) for f in fills}

            logger.info(f"[TRADES] {self.label}: Loaded {len(formatted)} recent trades")

    async def _poll_loop(self):
        """Poll for position changes every POLL_INTERVAL seconds."""
        while self.running:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                await self._check_updates()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll error for {self.label}: {e}")
                await asyncio.sleep(POLL_INTERVAL)

    async def _check_updates(self):
        """Check for new fills and position changes."""
        loop = asyncio.get_event_loop()

        # Fetch current state
        state = await loop.run_in_executor(None, fetch_clearinghouse_state, self.address)
        fills = await loop.run_in_executor(None, fetch_user_fills, self.address)

        if state:
            new_positions = parse_positions(state)
            account_info = parse_account_info(state)

            with lock:
                old_positions = wallet_positions.get(self.address, [])
                wallet_positions[self.address] = new_positions
                wallet_account_info[self.address] = account_info

            # Detect position changes
            self._detect_position_changes(old_positions, new_positions)

        if fills:
            new_fills = []
            for f in fills:
                fid = f.get("tid", f.get("hash", ""))
                if fid and fid not in self.last_fill_ids:
                    new_fills.append(f)
                    self.last_fill_ids.add(fid)

            if new_fills:
                formatted = [format_fill(f) for f in new_fills]
                with lock:
                    wallet_trades[self.address] = (formatted + wallet_trades[self.address])[:MAX_TRADE_HISTORY]

                for trade in formatted:
                    self._emit_notification(trade)

    def _detect_position_changes(self, old_pos: list, new_pos: list):
        """Detect and notify about position changes."""
        old_map = {p["coin"]: p for p in old_pos}
        new_map = {p["coin"]: p for p in new_pos}

        # New positions opened
        for coin, pos in new_map.items():
            if coin not in old_map:
                notif = {
                    "type": "NEW_POSITION",
                    "wallet": self.address,
                    "label": self.label,
                    "coin": coin,
                    "side": pos["side"],
                    "size": pos["size"],
                    "entryPx": pos["entryPx"],
                    "positionValue": pos["positionValue"],
                    "leverage": pos["leverage"],
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "message": f"[NEW] {self.label} opened {pos['side']} {pos['size']} {coin} @ ${pos['entryPx']:,.2f} (${pos['positionValue']:,.2f})"
                }
                self._push_notification(notif)

            elif coin in old_map:
                old = old_map[coin]
                # Size changed
                if abs(pos["size"] - old["size"]) > 0.0001:
                    action = "INCREASED" if pos["size"] > old["size"] else "DECREASED"
                    notif = {
                        "type": "POSITION_CHANGED",
                        "wallet": self.address,
                        "label": self.label,
                        "coin": coin,
                        "side": pos["side"],
                        "oldSize": old["size"],
                        "newSize": pos["size"],
                        "entryPx": pos["entryPx"],
                        "positionValue": pos["positionValue"],
                        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "message": f"[UPD] {self.label} {action} {coin} {pos['side']}: {old['size']} -> {pos['size']} @ ${pos['entryPx']:,.2f}"
                    }
                    self._push_notification(notif)

        # Positions closed
        for coin in old_map:
            if coin not in new_map:
                old = old_map[coin]
                notif = {
                    "type": "POSITION_CLOSED",
                    "wallet": self.address,
                    "label": self.label,
                    "coin": coin,
                    "side": old["side"],
                    "size": old["size"],
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "message": f"[CLOSE] {self.label} closed {old['side']} {old['size']} {coin}"
                }
                self._push_notification(notif)

    def _emit_notification(self, trade: dict):
        """Emit a trade notification."""
        notif = {
            "type": "NEW_TRADE",
            "wallet": self.address,
            "label": self.label,
            "coin": trade["coin"],
            "side": trade["side"],
            "price": trade["price"],
            "size": trade["size"],
            "value": trade["value"],
            "dir": trade["dir"],
            "closedPnl": trade["closedPnl"],
            "time": trade["time"],
            "message": f"[TRADE] {self.label} {trade['side']} {trade['size']} {trade['coin']} @ ${trade['price']:,.2f} ({trade['dir']}) | Value: ${trade['value']:,.2f}"
        }
        self._push_notification(notif)

    def _push_notification(self, notif: dict):
        """Push notification to global list and log."""
        with lock:
            notifications.insert(0, notif)
            if len(notifications) > 500:
                notifications[:] = notifications[:500]

        logger.info(notif["message"])

        # Broadcast to connected browser clients
        broadcast_to_clients(notif)

    async def _ws_loop(self):
        """Listen to Hyperliquid WebSocket for real-time fill updates."""
        while self.running:
            try:
                async with websockets.connect(HYPERLIQUID_WS_URL) as ws:
                    self._ws = ws
                    # Subscribe to user fills
                    sub_msg = json.dumps({
                        "method": "subscribe",
                        "subscription": {
                            "type": "userFills",
                            "user": self.address
                        }
                    })
                    await ws.send(sub_msg)
                    logger.info(f"[WS] WebSocket connected for {self.label}")

                    async for message in ws:
                        if not self.running:
                            break
                        try:
                            data = json.loads(message)
                            await self._handle_ws_message(data)
                        except json.JSONDecodeError:
                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"WebSocket error for {self.label}: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _handle_ws_message(self, data: dict):
        """Handle incoming WebSocket message."""
        channel = data.get("channel", "")
        msg_data = data.get("data", {})

        if channel == "userFills":
            is_snapshot = msg_data.get("isSnapshot", False)
            fills = msg_data.get("fills", [])

            if is_snapshot:
                # Update our known fill IDs from snapshot
                for f in fills:
                    fid = f.get("tid", f.get("hash", ""))
                    if fid:
                        self.last_fill_ids.add(fid)
                return

            # Process new fills
            for f in fills:
                fid = f.get("tid", f.get("hash", ""))
                if fid and fid not in self.last_fill_ids:
                    self.last_fill_ids.add(fid)
                    formatted = format_fill(f)
                    with lock:
                        wallet_trades[self.address].insert(0, formatted)
                        wallet_trades[self.address] = wallet_trades[self.address][:MAX_TRADE_HISTORY]
                    self._emit_notification(formatted)

                    # Also refresh positions
                    await self._check_updates()

    def stop(self):
        """Stop monitoring."""
        self.running = False
        logger.info(f"[STOP] Stopped monitor for {self.label}")


# ═══════════════════════════════════════════════════════════════════════════
# BROWSER WEBSOCKET BROADCAST
# ═══════════════════════════════════════════════════════════════════════════

browser_ws_clients = []

def broadcast_to_clients(data: dict):
    """Broadcast data to all connected browser WebSocket clients."""
    # We use Server-Sent Events (SSE) instead, simpler for the browser
    pass


# ═══════════════════════════════════════════════════════════════════════════
# ASYNC MONITOR MANAGER
# ═══════════════════════════════════════════════════════════════════════════

monitors = {}  # address -> WalletMonitor
event_loop = None

def start_monitor_for_wallet(address: str, label: str = ""):
    """Start a monitor for the given wallet address."""
    address = address.lower().strip()
    if address in monitors:
        return False, "Wallet already being monitored"

    monitor = WalletMonitor(address, label)
    monitors[address] = monitor

    with lock:
        monitored_wallets[address] = {
            "label": label or f"Wallet {address[:8]}",
            "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "address": address,
        }

    if event_loop:
        future = asyncio.run_coroutine_threadsafe(monitor.start(), event_loop)
        monitor_tasks[address] = future

    return True, "Monitor started"


def stop_monitor_for_wallet(address: str):
    """Stop monitoring a wallet."""
    address = address.lower().strip()
    if address not in monitors:
        return False, "Wallet not being monitored"

    monitors[address].stop()
    del monitors[address]

    if address in monitor_tasks:
        monitor_tasks[address].cancel()
        del monitor_tasks[address]

    with lock:
        monitored_wallets.pop(address, None)
        wallet_positions.pop(address, None)
        wallet_trades.pop(address, None)
        wallet_account_info.pop(address, None)

    return True, "Monitor stopped"


def run_async_loop():
    """Run the async event loop in a separate thread."""
    global event_loop
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    event_loop.run_forever()


# ═══════════════════════════════════════════════════════════════════════════
# SSE (Server-Sent Events) for real-time browser updates
# ═══════════════════════════════════════════════════════════════════════════

sse_queues = []

def broadcast_sse(data):
    """Push data to all SSE listeners."""
    msg = f"data: {json.dumps(data)}\n\n"
    dead = []
    for q in sse_queues:
        try:
            q.append(msg)
        except:
            dead.append(q)
    for d in dead:
        sse_queues.remove(d)

# Override the broadcast function
def broadcast_to_clients(data: dict):
    broadcast_sse(data)


# ═══════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory("static", path)

@app.route("/api/wallets", methods=["GET"])
def get_wallets():
    with lock:
        return jsonify(list(monitored_wallets.values()))

@app.route("/api/wallet/add", methods=["POST"])
def add_wallet():
    data = request.json
    address = data.get("address", "").strip()
    label = data.get("label", "").strip()

    if not address:
        return jsonify({"error": "Address required"}), 400

    if not address.startswith("0x") or len(address) != 42:
        return jsonify({"error": "Invalid Ethereum address format"}), 400

    success, msg = start_monitor_for_wallet(address, label)
    return jsonify({"success": success, "message": msg})

@app.route("/api/wallet/remove", methods=["POST"])
def remove_wallet():
    data = request.json
    address = data.get("address", "").strip()

    if not address:
        return jsonify({"error": "Address required"}), 400

    success, msg = stop_monitor_for_wallet(address)
    return jsonify({"success": success, "message": msg})

@app.route("/api/positions/<address>")
def get_positions(address):
    address = address.lower().strip()
    with lock:
        positions = wallet_positions.get(address, [])
        account = wallet_account_info.get(address, {})
    return jsonify({"positions": positions, "account": account})

@app.route("/api/trades/<address>")
def get_trades(address):
    address = address.lower().strip()
    with lock:
        trades = wallet_trades.get(address, [])
    return jsonify({"trades": trades})

@app.route("/api/notifications")
def get_notifications():
    with lock:
        return jsonify(notifications[:100])

@app.route("/api/dashboard")
def get_dashboard():
    """Get full dashboard data."""
    with lock:
        wallets = list(monitored_wallets.values())
        all_positions = {}
        all_accounts = {}
        all_trades = {}

        for addr in monitored_wallets:
            all_positions[addr] = wallet_positions.get(addr, [])
            all_accounts[addr] = wallet_account_info.get(addr, {})
            all_trades[addr] = wallet_trades.get(addr, [])[:20]

        return jsonify({
            "wallets": wallets,
            "positions": all_positions,
            "accounts": all_accounts,
            "trades": all_trades,
            "notifications": notifications[:50],
        })

@app.route("/api/stream")
def sse_stream():
    """Server-Sent Events endpoint for real-time updates."""
    def generate():
        q = []
        sse_queues.append(q)
        try:
            while True:
                if q:
                    msg = q.pop(0)
                    yield msg
                else:
                    # Send heartbeat
                    yield ": heartbeat\n\n"
                    time.sleep(1)
        except GeneratorExit:
            if q in sse_queues:
                sse_queues.remove(q)

    return app.response_class(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("")
    print("=" * 62)
    print("  HYPERLIQUID WALLET MONITOR v1.0")
    print("  Real-time position & trade tracking")
    print("-" * 62)
    print("  Dashboard: http://localhost:5000")
    print("  API Docs:  http://localhost:5000/api/dashboard")
    print("=" * 62)
    print("")

    # Start async event loop in background thread
    async_thread = threading.Thread(target=run_async_loop, daemon=True)
    async_thread.start()

    # Give the event loop a moment to start
    time.sleep(0.5)

    # Get port from environment variable (required by Render)
    port = int(os.environ.get("PORT", 5000))
    
    # Start Flask server
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
