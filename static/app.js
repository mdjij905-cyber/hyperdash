/**
 * HyperDash — Hyperliquid Wallet Monitor
 * Frontend Application Logic
 */

// ═══════════════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════════════

const state = {
    wallets: [],
    positions: {},
    accounts: {},
    trades: {},
    notifications: [],
    selectedWallet: null,
    sseSource: null,
    refreshInterval: null,
};

// ═══════════════════════════════════════════════════════════════════
// API HELPERS
// ═══════════════════════════════════════════════════════════════════

async function api(path, options = {}) {
    try {
        const resp = await fetch(path, {
            headers: { 'Content-Type': 'application/json' },
            ...options,
        });
        return await resp.json();
    } catch (err) {
        console.error(`API error: ${path}`, err);
        return null;
    }
}

// ═══════════════════════════════════════════════════════════════════
// WALLET MANAGEMENT
// ═══════════════════════════════════════════════════════════════════

async function addWallet() {
    const addressInput = document.getElementById('wallet-address');
    const labelInput = document.getElementById('wallet-label');
    const btn = document.getElementById('btn-add-wallet');

    const address = addressInput.value.trim();
    const label = labelInput.value.trim();

    if (!address) {
        shakeElement(addressInput);
        return;
    }

    if (!address.startsWith('0x') || address.length !== 42) {
        showToast({ type: 'error', message: 'Invalid wallet address. Must be 42-character hex starting with 0x.' });
        shakeElement(addressInput);
        return;
    }

    // Disable button
    btn.disabled = true;
    btn.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-width:2px;"></div> Adding...';

    const result = await api('/api/wallet/add', {
        method: 'POST',
        body: JSON.stringify({ address, label }),
    });

    if (result && result.success) {
        addressInput.value = '';
        labelInput.value = '';
        showToast({
            type: 'new',
            message: `Now monitoring ${label || address.slice(0, 10)}...`,
        });
        // Wait a moment for the initial snapshot
        setTimeout(() => refreshDashboard(), 2000);
    } else {
        showToast({
            type: 'error',
            message: result?.message || 'Failed to add wallet',
        });
    }

    btn.disabled = false;
    btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 3V13M3 8H13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg> Start Monitoring`;
}

async function removeWallet(address) {
    const result = await api('/api/wallet/remove', {
        method: 'POST',
        body: JSON.stringify({ address }),
    });

    if (result && result.success) {
        showToast({ type: 'closed', message: `Stopped monitoring ${address.slice(0, 10)}...` });
        refreshDashboard();
    }
}

// ═══════════════════════════════════════════════════════════════════
// DASHBOARD REFRESH
// ═══════════════════════════════════════════════════════════════════

async function refreshDashboard() {
    const data = await api('/api/dashboard');
    if (!data) return;

    state.wallets = data.wallets || [];
    state.positions = data.positions || {};
    state.accounts = data.accounts || {};
    state.trades = data.trades || {};
    state.notifications = data.notifications || [];

    renderWalletList();
    renderStats();
    renderPositions();
    renderTrades();
    renderNotifications();
    updateVisibility();
}

function updateVisibility() {
    const hasWallets = state.wallets.length > 0;
    document.getElementById('empty-state').style.display = hasWallets ? 'none' : 'flex';
    document.getElementById('positions-section').style.display = hasWallets ? 'block' : 'none';
    document.getElementById('opened-trades-section').style.display = hasWallets ? 'block' : 'none';
    document.getElementById('trades-section').style.display = hasWallets ? 'block' : 'none';
    document.getElementById('notifications-section').style.display = hasWallets ? 'block' : 'none';
}

// ═══════════════════════════════════════════════════════════════════
// RENDERERS
// ═══════════════════════════════════════════════════════════════════

function renderWalletList() {
    const container = document.getElementById('wallet-list');

    if (state.wallets.length === 0) {
        container.innerHTML = '<div class="empty-state-small"><p>No wallets tracked yet</p></div>';
        return;
    }

    container.innerHTML = state.wallets.map(w => {
        const isActive = state.selectedWallet === w.address;
        return `
            <div class="wallet-item ${isActive ? 'active' : ''}" onclick="selectWallet('${w.address}')">
                <div class="wallet-item-info">
                    <div class="wallet-item-label">${escapeHtml(w.label)}</div>
                    <div class="wallet-item-address">${w.address}</div>
                </div>
                <button class="wallet-item-remove" onclick="event.stopPropagation(); removeWallet('${w.address}')" title="Stop monitoring">
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3 3L11 11M3 11L11 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
                </button>
            </div>
        `;
    }).join('');
}

function selectWallet(address) {
    state.selectedWallet = state.selectedWallet === address ? null : address;
    renderWalletList();
    renderPositions();
    renderTrades();
}

function renderStats() {
    let totalPositions = 0;
    let totalValue = 0;
    let totalTrades = 0;

    for (const addr of Object.keys(state.positions)) {
        const positions = state.positions[addr] || [];
        totalPositions += positions.length;
        positions.forEach(p => { totalValue += p.positionValue || 0; });
    }

    for (const addr of Object.keys(state.trades)) {
        totalTrades += (state.trades[addr] || []).length;
    }

    document.getElementById('stat-wallets').textContent = state.wallets.length;
    document.getElementById('stat-positions').textContent = totalPositions;
    document.getElementById('stat-value').textContent = formatUSD(totalValue);
    document.getElementById('stat-trades').textContent = totalTrades;
}

function renderPositions() {
    const grid = document.getElementById('positions-grid');
    let html = '';

    const addresses = state.selectedWallet
        ? [state.selectedWallet]
        : Object.keys(state.positions);

    let allPositions = [];
    for (const addr of addresses) {
        const positions = state.positions[addr] || [];
        const wallet = state.wallets.find(w => w.address === addr);
        const walletLabel = wallet ? wallet.label : addr.slice(0, 10) + '...';

        for (const pos of positions) {
            // Find the most recent trade timestamp for this coin to determine age
            const coinTrades = (state.trades[addr] || []).filter(t => t.coin === pos.coin);
            coinTrades.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
            const lastTradeTime = coinTrades.length > 0 ? coinTrades[0].timestamp : 0;
            allPositions.push({ ...pos, walletLabel, lastTradeTime });
        }
    }

    // Sort positions: Newest trades to Oldest trades
    allPositions.sort((a, b) => b.lastTradeTime - a.lastTradeTime);

    for (const pos of allPositions) {
        const sideClass = pos.side === 'LONG' ? 'long' : 'short';
        const pnlClass = pos.unrealizedPnl >= 0 ? 'positive' : 'negative';
            const pnlSign = pos.unrealizedPnl >= 0 ? '+' : '';
            const roeSign = pos.returnOnEquity >= 0 ? '+' : '';

            html += `
                <div class="position-card ${sideClass} scale-in">
                    <div class="position-wallet-label">${escapeHtml(pos.walletLabel)}</div>
                    <div class="position-header">
                        <div class="position-coin">
                            <span class="position-coin-name">${escapeHtml(pos.coin)}</span>
                            <span class="position-side-badge ${sideClass}">${pos.side}</span>
                        </div>
                        <span class="position-leverage">${pos.leverage}x</span>
                    </div>
                    <div class="position-details">
                        <div class="position-detail">
                            <span class="position-detail-label">Size</span>
                            <span class="position-detail-value">${formatNumber(pos.size)}</span>
                        </div>
                        <div class="position-detail">
                            <span class="position-detail-label">Entry Price</span>
                            <span class="position-detail-value">${formatPrice(pos.entryPx)}</span>
                        </div>
                        <div class="position-detail">
                            <span class="position-detail-label">Position Value</span>
                            <span class="position-detail-value">${formatUSD(pos.positionValue)}</span>
                        </div>
                        <div class="position-detail">
                            <span class="position-detail-label">Liq. Price</span>
                            <span class="position-detail-value ${pos.liquidationPx ? '' : 'zero'}">${pos.liquidationPx ? formatPrice(parseFloat(pos.liquidationPx)) : 'N/A'}</span>
                        </div>
                        <div class="position-detail">
                            <span class="position-detail-label">Margin Used</span>
                            <span class="position-detail-value">${formatUSD(pos.marginUsed)}</span>
                        </div>
                        <div class="position-detail">
                            <span class="position-detail-label">Margin Mode</span>
                            <span class="position-detail-value" style="font-family: var(--font-primary); text-transform: capitalize;">${pos.leverageType || 'cross'}</span>
                        </div>
                    </div>
                    <div class="pnl-bar">
                        <span class="pnl-label">Unrealized PnL</span>
                        <div>
                            <span class="pnl-value ${pnlClass}">${pnlSign}${formatUSD(pos.unrealizedPnl)}</span>
                            <span class="pnl-percent ${pnlClass}">${roeSign}${pos.returnOnEquity.toFixed(2)}%</span>
                        </div>
                    </div>
                </div>
            `;
    }

    if (!html) {
        html = '<div class="empty-state-small"><p>No open positions detected</p></div>';
    }

    grid.innerHTML = html;
}

function renderTrades() {
    const openedTbody = document.getElementById('opened-trades-tbody');
    const tbody = document.getElementById('trades-tbody');
    let allTrades = [];

    const addresses = state.selectedWallet
        ? [state.selectedWallet]
        : Object.keys(state.trades);

    for (const addr of addresses) {
        const trades = state.trades[addr] || [];
        const wallet = state.wallets.find(w => w.address === addr);
        const walletLabel = wallet ? wallet.label : addr.slice(0, 10) + '...';

        for (const trade of trades) {
            allTrades.push({ ...trade, walletLabel, walletAddress: addr });
        }
    }

    // Sort by timestamp descending
    allTrades.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));

    // Filter into opening trades and other trades
    let openingTrades = allTrades.filter(t => (t.dir || '').toLowerCase().includes('open'));
    let otherTrades = allTrades.filter(t => !(t.dir || '').toLowerCase().includes('open'));

    // Limit to most recent
    openingTrades = openingTrades.slice(0, 50);
    otherTrades = otherTrades.slice(0, 50);

    const generateRows = (tradesArr) => {
        if (tradesArr.length === 0) {
            return '<tr><td colspan="9" style="text-align:center;color:var(--text-tertiary);padding:30px;">No trades recorded yet</td></tr>';
        }

        return tradesArr.map(trade => {
            const sideClass = trade.side === 'BUY' ? 'buy' : 'sell';
            const pnl = parseFloat(trade.closedPnl || 0);
            const pnlClass = pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'zero';
            const pnlSign = pnl > 0 ? '+' : '';

            return `
                <tr>
                    <td class="trade-time">${trade.time || '—'}</td>
                    <td class="trade-wallet">${escapeHtml(trade.walletLabel)}</td>
                    <td class="trade-coin">${escapeHtml(trade.coin)}</td>
                    <td><span class="trade-side ${sideClass}">${trade.side}</span></td>
                    <td class="trade-dir">${escapeHtml(trade.dir || '—')}</td>
                    <td class="trade-value">${formatNumber(trade.size)}</td>
                    <td class="trade-value">${formatPrice(trade.price)}</td>
                    <td class="trade-value">${formatUSD(trade.value)}</td>
                    <td class="trade-pnl ${pnlClass}">${pnlSign}${formatUSD(pnl)}</td>
                </tr>
            `;
        }).join('');
    };

    openedTbody.innerHTML = generateRows(openingTrades);
    tbody.innerHTML = generateRows(otherTrades);
}

function renderNotifications() {
    const feed = document.getElementById('notification-feed');

    if (state.notifications.length === 0) {
        feed.innerHTML = '<div class="empty-state-small"><p>No activity yet. Notifications will appear here when monitored wallets make trades.</p></div>';
        return;
    }

    feed.innerHTML = state.notifications.slice(0, 50).map(notif => {
        const typeClass = (notif.type || '').toLowerCase().replace('_', '-');
        const icon = getNotificationIcon(notif.type);

        return `
            <div class="notification-item ${typeClass}">
                <span class="notification-icon">${icon}</span>
                <div class="notification-content">
                    <div class="notification-message">${escapeHtml(notif.message || '')}</div>
                    <div class="notification-time">${notif.time || ''}</div>
                </div>
            </div>
        `;
    }).join('');
}

function getNotificationIcon(type) {
    switch (type) {
        case 'NEW_POSITION': return '🟢';
        case 'POSITION_CLOSED': return '🔴';
        case 'POSITION_CHANGED': return '🔄';
        case 'NEW_TRADE': return '⚡';
        default: return '📋';
    }
}

// ═══════════════════════════════════════════════════════════════════
// TOAST NOTIFICATIONS
// ═══════════════════════════════════════════════════════════════════

function showToast(notif) {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = 'toast';

    const typeLabel = notif.type || 'info';
    const typeClass = typeLabel === 'error' ? 'closed' : typeLabel;

    toast.innerHTML = `
        <div class="toast-header">
            <span class="toast-type ${typeClass}">${typeLabel.toUpperCase()}</span>
        </div>
        <div class="toast-message">${escapeHtml(notif.message || '')}</div>
        <div class="toast-time">${new Date().toLocaleTimeString()}</div>
    `;

    container.appendChild(toast);

    // Auto-remove after 5 seconds
    setTimeout(() => {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 5000);
}

// ═══════════════════════════════════════════════════════════════════
// SSE (Server-Sent Events) REAL-TIME UPDATES
// ═══════════════════════════════════════════════════════════════════

function connectSSE() {
    if (state.sseSource) {
        state.sseSource.close();
    }

    const source = new EventSource('/api/stream');
    state.sseSource = source;

    source.onopen = () => {
        updateConnectionStatus('connected');
    };

    source.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleRealtimeUpdate(data);
        } catch (e) {
            // Heartbeat or malformed message
        }
    };

    source.onerror = () => {
        updateConnectionStatus('error');
        // Auto-reconnect is handled by EventSource
        setTimeout(() => {
            updateConnectionStatus('connecting');
        }, 1000);
    };
}

function handleRealtimeUpdate(data) {
    // Show toast notification
    const typeMap = {
        'NEW_TRADE': 'buy',
        'NEW_POSITION': 'new',
        'POSITION_CLOSED': 'closed',
        'POSITION_CHANGED': 'changed',
    };

    showToast({
        type: typeMap[data.type] || 'info',
        message: data.message || 'New activity detected',
    });

    // Add to notifications
    state.notifications.unshift(data);
    if (state.notifications.length > 100) {
        state.notifications = state.notifications.slice(0, 100);
    }

    // Refresh full dashboard to pick up new positions
    refreshDashboard();

    // Play sound
    try {
        // Use Web Audio API for notification sound
        playNotificationSound();
    } catch (e) {}
}

function playNotificationSound() {
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const oscillator = ctx.createOscillator();
        const gain = ctx.createGain();
        oscillator.connect(gain);
        gain.connect(ctx.destination);
        oscillator.frequency.setValueAtTime(800, ctx.currentTime);
        oscillator.frequency.setValueAtTime(600, ctx.currentTime + 0.1);
        gain.gain.setValueAtTime(0.1, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
        oscillator.start(ctx.currentTime);
        oscillator.stop(ctx.currentTime + 0.3);
    } catch (e) {}
}

function updateConnectionStatus(status) {
    const statusEl = document.getElementById('connection-status');
    const dot = statusEl.querySelector('.status-dot');
    const text = statusEl.querySelector('.status-text');

    dot.className = 'status-dot';

    switch (status) {
        case 'connected':
            dot.classList.add('connected');
            text.textContent = 'Connected';
            break;
        case 'error':
            dot.classList.add('error');
            text.textContent = 'Disconnected';
            break;
        default:
            text.textContent = 'Connecting...';
    }
}

// ═══════════════════════════════════════════════════════════════════
// UI UTILITIES
// ═══════════════════════════════════════════════════════════════════

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    sidebar.classList.toggle('collapsed');
}

function clearNotifications() {
    state.notifications = [];
    renderNotifications();
}

function shakeElement(el) {
    el.style.animation = 'none';
    el.offsetHeight; // trigger reflow
    el.style.animation = 'shake 0.5s ease';
    setTimeout(() => { el.style.animation = ''; }, 500);
}

// Add shake animation
const shakeStyle = document.createElement('style');
shakeStyle.textContent = `
@keyframes shake {
    0%, 100% { transform: translateX(0); }
    20% { transform: translateX(-6px); }
    40% { transform: translateX(6px); }
    60% { transform: translateX(-4px); }
    80% { transform: translateX(4px); }
}
`;
document.head.appendChild(shakeStyle);

// ═══════════════════════════════════════════════════════════════════
// FORMATTERS
// ═══════════════════════════════════════════════════════════════════

function formatUSD(value) {
    if (value === undefined || value === null) return '$0.00';
    const num = parseFloat(value);
    if (isNaN(num)) return '$0.00';

    if (Math.abs(num) >= 1000000) {
        return '$' + (num / 1000000).toFixed(2) + 'M';
    }
    if (Math.abs(num) >= 1000) {
        return '$' + num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return '$' + num.toFixed(2);
}

function formatPrice(value) {
    if (value === undefined || value === null) return '$0';
    const num = parseFloat(value);
    if (isNaN(num)) return '$0';

    if (num >= 1000) {
        return '$' + num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    if (num >= 1) {
        return '$' + num.toFixed(4);
    }
    return '$' + num.toPrecision(4);
}

function formatNumber(value) {
    if (value === undefined || value === null) return '0';
    const num = parseFloat(value);
    if (isNaN(num)) return '0';

    if (Math.abs(num) >= 1000000) {
        return (num / 1000000).toFixed(2) + 'M';
    }
    if (Math.abs(num) >= 1000) {
        return num.toLocaleString('en-US', { maximumFractionDigits: 4 });
    }
    if (Math.abs(num) < 0.001) {
        return num.toPrecision(4);
    }
    return num.toLocaleString('en-US', { maximumFractionDigits: 6 });
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ═══════════════════════════════════════════════════════════════════
// CLOCK
// ═══════════════════════════════════════════════════════════════════

function updateClock() {
    const now = new Date();
    const hours = now.getHours().toString().padStart(2, '0');
    const mins = now.getMinutes().toString().padStart(2, '0');
    const secs = now.getSeconds().toString().padStart(2, '0');
    document.getElementById('clock').textContent = `${hours}:${mins}:${secs}`;
}

// ═══════════════════════════════════════════════════════════════════
// KEYBOARD SHORTCUTS
// ═══════════════════════════════════════════════════════════════════

document.addEventListener('keydown', (e) => {
    // Ctrl+K to focus wallet address input
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        document.getElementById('wallet-address').focus();
    }

    // Escape to toggle sidebar
    if (e.key === 'Escape') {
        const sidebar = document.getElementById('sidebar');
        if (!sidebar.classList.contains('collapsed')) {
            sidebar.classList.add('collapsed');
        }
    }

    // Enter to add wallet when input is focused
    if (e.key === 'Enter') {
        const active = document.activeElement;
        if (active && (active.id === 'wallet-address' || active.id === 'wallet-label')) {
            addWallet();
        }
    }
});

// ═══════════════════════════════════════════════════════════════════
// INITIALIZATION
// ═══════════════════════════════════════════════════════════════════

window.addEventListener('DOMContentLoaded', () => {
    // Initial dashboard load
    refreshDashboard();

    // Connect to SSE for real-time updates
    connectSSE();

    // Start clock
    updateClock();
    setInterval(updateClock, 1000);

    // Auto-refresh every 10 seconds
    state.refreshInterval = setInterval(refreshDashboard, 10000);

    console.log('%c HyperDash ', 'background: linear-gradient(135deg, #00e5ff, #7c4dff); color: white; font-size: 14px; font-weight: bold; padding: 4px 12px; border-radius: 4px;');
    console.log('%c Hyperliquid Wallet Monitor ', 'color: #8b95b0; font-size: 11px;');
});
