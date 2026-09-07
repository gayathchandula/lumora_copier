// Lumora Scalping Server
// -----------------------
// Relay between the Admin EA (running the Nyao Scalper strategy) and any
// number of Client EAs (running on each client's own MT5 terminal).
//
// Flow:
//   Admin EA  --POST /api/admin/signal-->  Server  --GET /api/client/signals-->  Client EA
//   Client EA --POST /api/client/report--> Server  <--GET /api/admin/clients--   Admin GUI
//
// Auth:
//   Admin endpoints: header  X-Admin-Key: <ADMIN_KEY>
//   Client endpoints: headers X-Client-Id: <id>  X-Client-Key: <per-client secret>
//
// Run:  ADMIN_KEY=changeme node server.js
'use strict';

const path = require('path');
const express = require('express');
const cors = require('cors');
const crypto = require('crypto');
const { v4: uuidv4 } = require('uuid');
const { init: initDb, state, withState } = require('./db');
const PACKAGES = require('./packages');
const CLIENT_VERSION_INFO = require('./client_version');

const PORT = process.env.PORT || 4000;
const ADMIN_KEY = process.env.ADMIN_KEY || 'admin123';

const app = express();
app.use(cors());
app.use(express.json());
// Serves the client .exe for the "Restart to Update" download - drop new
// builds in server/public/downloads/ and bump client_version.js to match.
app.use('/downloads', express.static(path.join(__dirname, 'public', 'downloads')));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function newApiKey() {
  return crypto.randomBytes(24).toString('hex');
}

function findClient(id) {
  return state().clients.find(c => c.id === id);
}

function findClientByEmail(email) {
  const needle = (email || '').trim().toLowerCase();
  return state().clients.find(c => (c.email || '').trim().toLowerCase() === needle);
}

// scrypt is built into Node - no bcrypt dependency needed. Salt is unique per
// client; hash/salt are never exposed in any API response.
function hashPassword(password) {
  const salt = crypto.randomBytes(16).toString('hex');
  const hash = crypto.scryptSync(password, salt, 64).toString('hex');
  return { password_salt: salt, password_hash: hash };
}

function verifyPassword(password, salt, hash) {
  if (!salt || !hash) return false;
  const candidate = crypto.scryptSync(password, salt, 64).toString('hex');
  const a = Buffer.from(candidate, 'hex');
  const b = Buffer.from(hash, 'hex');
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function findPackage(id) {
  return PACKAGES.find(p => p.id === id);
}

function findPaymentMethod(id) {
  return state().paymentMethods.find(m => m.id === id);
}

// Lazily flips an ACTIVE client to EXPIRED once its subscription has lapsed —
// this is the whole auto-halt-on-expiry mechanism. No cron needed: it's
// evaluated whenever a client is read for admin listing or an auth check.
// Legacy rows created before subscriptions existed default to ACTIVE so they
// keep working untouched.
function effectiveStatus(c) {
  if (!c.status) c.status = 'ACTIVE';
  if (c.status === 'ACTIVE' && c.subscription_end && new Date(c.subscription_end) < new Date()) {
    c.status = 'EXPIRED';
    c.active = false;
  }
  return c.status;
}

function daysLeft(c) {
  if (!c.subscription_end) return null;
  return Math.ceil((new Date(c.subscription_end).getTime() - Date.now()) / 86_400_000);
}

function requireAdmin(req, res, next) {
  if (req.header('X-Admin-Key') !== ADMIN_KEY) {
    return res.status(401).json({ error: 'invalid admin key' });
  }
  next();
}

// Validates client credentials only — no subscription-status check. Used for
// status polling and payment submission, which must work even before (or
// after) the account is active.
function requireClientAuth(req, res, next) {
  const id = req.header('X-Client-Id');
  const key = req.header('X-Client-Key');
  const client = findClient(id);
  if (!client || client.api_key !== key) {
    return res.status(401).json({ error: 'invalid client credentials' });
  }
  req.client = client;
  next();
}

// Same as above, plus requires an ACTIVE subscription — used for the actual
// trading endpoints.
async function requireActiveClient(req, res, next) {
  requireClientAuth(req, res, async () => {
    await withState(() => effectiveStatus(req.client));
    if (req.client.status !== 'ACTIVE') {
      return res.status(403).json({ error: `subscription not active (status: ${req.client.status})` });
    }
    next();
  });
}

// Rolls up today's / total realized profit for a client from its reports.
function clientPnlSummary(clientId) {
  const reports = state().reports.filter(r => r.client_id === clientId && r.status === 'CLOSED');
  const todayKey = new Date().toISOString().slice(0, 10);
  let today = 0, total = 0;
  for (const r of reports) {
    total += r.profit || 0;
    if ((r.at || '').slice(0, 10) === todayKey) today += r.profit || 0;
  }
  return { todayProfit: round2(today), totalProfit: round2(total), closedTrades: reports.length };
}

function round2(n) { return Math.round((n + Number.EPSILON) * 100) / 100; }

// ---------------------------------------------------------------------------
// ADMIN: client management
// ---------------------------------------------------------------------------

// Create a new client (license). Returns the client_id + client_key ONCE —
// hand these to the client to paste into their Client EA / Client GUI.
// Admin-vouched shortcut (e.g. phone/offline sales): creates the client
// already ACTIVE, bypassing the self-service register -> pay -> approve queue.
app.post('/api/admin/clients', requireAdmin, async (req, res) => {
  const { name, email, mt5_login, lot_mode, lot_value, package_id, duration_days, password } = req.body || {};
  if (!name) return res.status(400).json({ error: 'name is required' });
  if (password && password.length < 6) {
    return res.status(400).json({ error: 'password must be at least 6 characters' });
  }
  if (email && findClientByEmail(email)) {
    return res.status(409).json({ error: 'an account with this email already exists' });
  }

  const pkg = findPackage(package_id) || PACKAGES[0];
  const days = duration_days || pkg.duration_days;
  const now = new Date();

  const client = {
    id: uuidv4(),
    api_key: newApiKey(),
    name,
    email: email || '',
    ...(password ? hashPassword(password) : {}),
    mt5_login: mt5_login || '',
    lot_mode: lot_mode || 'MULTIPLIER',       // FIXED | MULTIPLIER | RISK_PERCENT
    lot_value: typeof lot_value === 'number' ? lot_value : 1.0,
    active: true,
    status: 'ACTIVE',
    package_id: pkg.id,
    subscription_start: now.toISOString(),
    subscription_end: new Date(now.getTime() + days * 86_400_000).toISOString(),
    pending_payment: null,
    payment_history: [],
    created_at: now.toISOString(),
    last_seen: null,
    balance: null,
    equity: null,
    floating_profit: null,
  };
  await withState(s => s.clients.push(client));
  res.json(client);
});

app.get('/api/admin/clients', requireAdmin, async (req, res) => {
  await withState(s => { s.clients.forEach(effectiveStatus); });
  const clients = state().clients.map(c => ({
    ...c,
    api_key: undefined,          // don't leak keys back to a generic list view
    online: c.last_seen ? (Date.now() - new Date(c.last_seen).getTime() < 60_000) : false,
    pnl: clientPnlSummary(c.id),
    package: findPackage(c.package_id) || null,
    days_left: daysLeft(c),
  }));
  res.json(clients);
});

// ---------------------------------------------------------------------------
// ADMIN: subscription lifecycle (approve / reject / extend / halt)
// ---------------------------------------------------------------------------
app.post('/api/admin/clients/:id/approve', requireAdmin, async (req, res) => {
  const c = findClient(req.params.id);
  if (!c) return res.status(404).json({ error: 'not found' });
  if (!c.pending_payment) return res.status(400).json({ error: 'no pending payment to approve' });

  const pkg = findPackage(c.pending_payment.package_id);
  const days = req.body?.duration_days || (pkg ? pkg.duration_days : 30);
  const now = new Date();

  await withState(() => {
    c.payment_history = c.payment_history || [];
    c.payment_history.push({ ...c.pending_payment, decision: 'APPROVED', decided_at: now.toISOString() });
    c.package_id = c.pending_payment.package_id;
    c.pending_payment = null;
    c.status = 'ACTIVE';
    c.active = true;
    c.subscription_start = now.toISOString();
    c.subscription_end = new Date(now.getTime() + days * 86_400_000).toISOString();
  });
  res.json(c);
});

app.post('/api/admin/clients/:id/reject', requireAdmin, async (req, res) => {
  const c = findClient(req.params.id);
  if (!c) return res.status(404).json({ error: 'not found' });
  if (!c.pending_payment) return res.status(400).json({ error: 'no pending payment to reject' });

  await withState(() => {
    c.payment_history = c.payment_history || [];
    c.payment_history.push({
      ...c.pending_payment, decision: 'REJECTED',
      reason: req.body?.reason || '', decided_at: new Date().toISOString(),
    });
    c.pending_payment = null;
    c.status = 'PENDING_PAYMENT';
  });
  res.json(c);
});

// Renewal: extends from whichever is later, now or the current expiry (so
// renewing early doesn't waste remaining days), and consumes a pending
// payment if the renewal came in through the payment-submit flow.
app.post('/api/admin/clients/:id/extend', requireAdmin, async (req, res) => {
  const c = findClient(req.params.id);
  if (!c) return res.status(404).json({ error: 'not found' });

  const pkg = findPackage(c.package_id) || findPackage(c.pending_payment?.package_id);
  const days = req.body?.duration_days || (pkg ? pkg.duration_days : 30);
  const now = new Date();
  const base = c.subscription_end && new Date(c.subscription_end) > now ? new Date(c.subscription_end) : now;

  await withState(() => {
    if (c.pending_payment) {
      c.payment_history = c.payment_history || [];
      c.payment_history.push({ ...c.pending_payment, decision: 'APPROVED', decided_at: now.toISOString() });
      c.package_id = c.pending_payment.package_id;
      c.pending_payment = null;
    }
    c.status = 'ACTIVE';
    c.active = true;
    if (!c.subscription_start) c.subscription_start = now.toISOString();
    c.subscription_end = new Date(base.getTime() + days * 86_400_000).toISOString();
  });
  res.json(c);
});

app.post('/api/admin/clients/:id/halt', requireAdmin, async (req, res) => {
  const c = findClient(req.params.id);
  if (!c) return res.status(404).json({ error: 'not found' });

  await withState(() => {
    c.status = 'HALTED';
    c.active = false;
  });
  res.json(c);
});

// Reveal a single client's key again (e.g. to re-issue to the client) — separate
// endpoint so it never appears in the bulk list above.
app.get('/api/admin/clients/:id/key', requireAdmin, (req, res) => {
  const c = findClient(req.params.id);
  if (!c) return res.status(404).json({ error: 'not found' });
  res.json({ id: c.id, api_key: c.api_key });
});

app.patch('/api/admin/clients/:id', requireAdmin, async (req, res) => {
  const c = findClient(req.params.id);
  if (!c) return res.status(404).json({ error: 'not found' });
  const { active, lot_mode, lot_value, name } = req.body || {};
  await withState(() => {
    if (typeof active === 'boolean') c.active = active;
    if (lot_mode) c.lot_mode = lot_mode;
    if (typeof lot_value === 'number') c.lot_value = lot_value;
    if (name) c.name = name;
  });
  res.json(c);
});

app.delete('/api/admin/clients/:id', requireAdmin, async (req, res) => {
  await withState(s => { s.clients = s.clients.filter(c => c.id !== req.params.id); });
  res.json({ ok: true });
});

// ---------------------------------------------------------------------------
// ADMIN EA -> SERVER: new trade signal
// ---------------------------------------------------------------------------
// action: OPEN | MODIFY | CLOSE
app.post('/api/admin/signal', requireAdmin, async (req, res) => {
  const { action, symbol, type, admin_ticket, position_id, volume, sl, tp, price, comment } = req.body || {};
  if (!action || !symbol) return res.status(400).json({ error: 'action and symbol are required' });

  const signal = await withState(s => {
    const sig = {
      id: s.nextSignalId++,
      action, symbol, type: type || '', admin_ticket: admin_ticket || 0,
      position_id: position_id || 0, volume: volume || 0,
      sl: sl || 0, tp: tp || 0, price: price || 0,
      comment: comment || '',
      created_at: new Date().toISOString(),
    };
    s.signals.push(sig);
    // Keep history from growing forever
    if (s.signals.length > 5000) s.signals.splice(0, s.signals.length - 5000);
    return sig;
  });

  res.json(signal);
});

app.get('/api/admin/signals', requireAdmin, (req, res) => {
  const limit = Math.min(parseInt(req.query.limit) || 50, 500);
  res.json(state().signals.slice(-limit).reverse());
});

// ---------------------------------------------------------------------------
// CLIENT EA: poll for new signals since last-seen id
// ---------------------------------------------------------------------------
app.get('/api/client/signals', requireActiveClient, async (req, res) => {
  const since = parseInt(req.query.since) || 0;
  const pending = state().signals.filter(s => s.id > since);
  await withState(() => { req.client.last_seen = new Date().toISOString(); });
  res.json(pending);
});

// Client EA reports the result of copying a signal (or a full trade close).
// A CLOSED report is self-contained (symbol/type/volume/entry_price/opened_at
// included alongside the usual price+profit) so a full trade card can be
// rendered straight from one report row - no joining against the OPENED row.
app.post('/api/client/report', requireActiveClient, async (req, res) => {
  const { signal_id, client_ticket, status, price, profit, symbol, type, volume, entry_price, opened_at, margin } = req.body || {};
  const report = {
    id: uuidv4(),
    client_id: req.client.id,
    signal_id: signal_id || 0,
    client_ticket: client_ticket || 0,
    status: status || 'UNKNOWN',   // OPENED | MODIFIED | CLOSED | PARTIAL_CLOSED | FAILED
    price: price || 0,
    profit: typeof profit === 'number' ? profit : 0,
    symbol: symbol || null,
    type: type || null,             // BUY | SELL
    volume: typeof volume === 'number' ? volume : null,
    entry_price: typeof entry_price === 'number' ? entry_price : null,
    opened_at: opened_at || null,
    margin: typeof margin === 'number' ? margin : null,
    at: new Date().toISOString(),
  };
  await withState(s => s.reports.push(report));
  res.json({ ok: true });
});

// The client's own most recent fully-closed trade, for the "Download Profit
// Card" feature - everything needed to render the card in one response.
app.get('/api/client/last-closed-trade', requireClientAuth, (req, res) => {
  const reports = state().reports
    .filter(r => r.client_id === req.client.id && r.status === 'CLOSED')
    .sort((a, b) => new Date(b.at) - new Date(a.at));
  res.json(reports[0] || null);
});

// Client EA heartbeat: account snapshot, used for the admin GUI's live P/L view
app.post('/api/client/heartbeat', requireActiveClient, async (req, res) => {
  const { balance, equity, floating_profit } = req.body || {};
  await withState(() => {
    req.client.last_seen = new Date().toISOString();
    if (typeof balance === 'number') req.client.balance = balance;
    if (typeof equity === 'number') req.client.equity = equity;
    if (typeof floating_profit === 'number') req.client.floating_profit = floating_profit;
  });
  res.json({ ok: true });
});

// Client-facing "my status" — lets the Client GUI show the client their own
// login, connection state, subscription status and live profit, without ever
// handing out the admin key. Uses requireClientAuth (not requireActiveClient)
// since the client needs this to work while pending/expired/halted too, to
// know which onboarding/renewal screen to show.
function clientMePayload(c) {
  return {
    id: c.id,
    name: c.name,
    mt5_login: c.mt5_login,
    active: c.active,
    status: c.status,
    lot_mode: c.lot_mode,
    lot_value: c.lot_value,
    balance: c.balance,
    equity: c.equity,
    floating_profit: c.floating_profit,
    last_seen: c.last_seen,
    package: findPackage(c.package_id) || null,
    subscription_start: c.subscription_start,
    subscription_end: c.subscription_end,
    days_left: daysLeft(c),
    pending_payment: c.pending_payment || null,
    pnl: clientPnlSummary(c.id),
  };
}

app.get('/api/client/me', requireClientAuth, async (req, res) => {
  await withState(() => effectiveStatus(req.client));
  res.json(clientMePayload(req.client));
});

// ---------------------------------------------------------------------------
// CUSTOMER-FACING: self-service registration and payment submission
// ---------------------------------------------------------------------------
app.get('/api/packages', (req, res) => res.json(PACKAGES));

// Powers the client app's "Restart to Update" banner - see client_version.js.
// download_url is returned exactly as configured there (a relative path, by
// convention); the client joins it with its own known server URL. Deriving
// an absolute URL here would need to trust X-Forwarded-Proto from ngrok,
// which Express doesn't by default - simpler to just not need it.
app.get('/api/client-version', (req, res) => res.json(CLIENT_VERSION_INFO));

// Public: only methods the admin has switched on, for the client's payment screen.
app.get('/api/payment-methods', (req, res) => {
  res.json(state().paymentMethods.filter(m => m.enabled));
});

app.post('/api/customer/register', async (req, res) => {
  const { name, email, mt5_login, password } = req.body || {};
  if (!name || !email) return res.status(400).json({ error: 'name and email are required' });
  if (!password || password.length < 6) {
    return res.status(400).json({ error: 'password is required and must be at least 6 characters' });
  }
  if (findClientByEmail(email)) {
    return res.status(409).json({ error: 'an account with this email already exists' });
  }

  const client = {
    id: uuidv4(),
    api_key: newApiKey(),
    name,
    email,
    ...hashPassword(password),
    mt5_login: mt5_login || '',
    lot_mode: 'MULTIPLIER',
    lot_value: 1.0,
    active: false,
    status: 'PENDING_PAYMENT',
    package_id: null,
    subscription_start: null,
    subscription_end: null,
    pending_payment: null,
    payment_history: [],
    created_at: new Date().toISOString(),
    last_seen: null,
    balance: null,
    equity: null,
    floating_profit: null,
  };
  await withState(s => s.clients.push(client));
  res.json({ id: client.id, api_key: client.api_key });
});

// Self-service login: exchange email+password for the id/api_key pair the
// client app needs to store locally for every other call. Returns the same
// shape as GET /api/client/me, plus api_key (which /me never exposes).
app.post('/api/customer/login', async (req, res) => {
  const { email, password } = req.body || {};
  if (!email || !password) return res.status(400).json({ error: 'email and password are required' });

  const c = findClientByEmail(email);
  if (!c || !verifyPassword(password, c.password_salt, c.password_hash)) {
    return res.status(401).json({ error: 'invalid email or password' });
  }

  await withState(() => effectiveStatus(c));
  res.json({ ...clientMePayload(c), api_key: c.api_key });
});

// Submits (or re-submits, for a renewal) proof of payment for admin review.
app.post('/api/customer/payment', requireClientAuth, async (req, res) => {
  const { package_id, reference, note } = req.body || {};
  const pkg = findPackage(package_id);
  if (!pkg) return res.status(400).json({ error: 'unknown package_id' });
  if (!reference) return res.status(400).json({ error: 'payment reference is required' });

  await withState(() => {
    req.client.pending_payment = { package_id, reference, note: note || '', submitted_at: new Date().toISOString() };
    req.client.status = 'PENDING_APPROVAL';
  });
  res.json({ ok: true, status: 'PENDING_APPROVAL' });
});

// ---------------------------------------------------------------------------
// ADMIN: payment method management (crypto wallet / bank details, etc.)
// ---------------------------------------------------------------------------
// Full list including disabled ones, for editing.
app.get('/api/admin/payment-methods', requireAdmin, (req, res) => {
  res.json(state().paymentMethods);
});

app.post('/api/admin/payment-methods', requireAdmin, async (req, res) => {
  const { label, details, enabled } = req.body || {};
  if (!label) return res.status(400).json({ error: 'label is required' });

  const method = { id: uuidv4(), label, details: details || '', enabled: enabled !== false };
  await withState(s => s.paymentMethods.push(method));
  res.json(method);
});

app.patch('/api/admin/payment-methods/:id', requireAdmin, async (req, res) => {
  const m = findPaymentMethod(req.params.id);
  if (!m) return res.status(404).json({ error: 'not found' });

  const { label, details, enabled } = req.body || {};
  await withState(() => {
    if (typeof label === 'string') m.label = label;
    if (typeof details === 'string') m.details = details;
    if (typeof enabled === 'boolean') m.enabled = enabled;
  });
  res.json(m);
});

app.delete('/api/admin/payment-methods/:id', requireAdmin, async (req, res) => {
  await withState(s => { s.paymentMethods = s.paymentMethods.filter(m => m.id !== req.params.id); });
  res.json({ ok: true });
});

app.get('/api/health', (req, res) => res.json({ ok: true, clients: state().clients.length }));

initDb()
  .then(() => {
    app.listen(PORT, () => {
      console.log(`Lumora Scalping Server listening on :${PORT}`);
      if (ADMIN_KEY === 'CHANGE_ME_ADMIN_KEY') {
        console.warn('WARNING: using default ADMIN_KEY — set the ADMIN_KEY env var before exposing this to the internet.');
      }
    });
  })
  .catch(err => {
    console.error('Failed to start: could not connect to MongoDB.', err.message);
    process.exit(1);
  });
