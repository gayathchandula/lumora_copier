# Lumora Scalping — Admin → Client Gold Trade Copier

Copies trades from your Admin MT5 (running the Nyao Scalper EA) to any number
of paying client MT5 accounts in real time, with a desktop dashboard showing
every client's subscription status and live profit.

## How it fits together

```
[Admin MT5 + LumoraScalper_AdminCopier EA]
         |  WebRequest POST (trade opened / modified / closed)
         v
[Copier Server]  <-- Node.js, runs on a VPS with a public HTTPS URL
   |        ^
   | poll   | POST report / heartbeat / payment / status
   v        |
[Client App (single .exe)]  <-- connects straight to the client's running,
                                  logged-in MT5 via the MetaTrader5 Python
                                  package, and places the trades itself
                                  (no EA needed on the client side)

[Admin GUI]  <-- desktop app, reads /api/admin/* to show all clients + live
                  P/L, and to review/approve pending subscription payments
```

(A chart-attached Client EA is also included as a fallback for clients who'd
rather not install a Python package — see "Alternative" below. Either kind of
client talks to the same server API.)

Nothing routes through anyone's exe directly — everyone talks to the central
**server**, which is the only thing that needs a real internet address. This
is far more reliable than trying to bridge two Windows processes directly,
and it's how virtually every commercial trade-copier product is built.

## 1. Deploy the server

The server stores its state (clients, signals, reports, payment methods) as a
single document in MongoDB, not a local file — that's what lets it run on
hosts with an ephemeral filesystem (like Render's free tier) without losing
data on every redeploy or restart.

**Get a free MongoDB Atlas cluster** (skip if you already have one):
1. Sign up at [mongodb.com/cloud/atlas](https://www.mongodb.com/cloud/atlas)
   and create a free **M0** cluster (512MB — plenty; this app's whole state is
   a few hundred KB even with thousands of signals, since old signals are
   trimmed).
2. **Database Access** — add a database user with a password.
3. **Network Access** — add `0.0.0.0/0` (allow access from anywhere), since
   Render's outbound IPs aren't fixed on free/starter plans.
4. **Connect → Drivers** — copy the connection string, e.g.
   `mongodb+srv://user:password@cluster0.xxxxx.mongodb.net/`.

**If you already have real data in `data/db.json`** (from earlier local
testing), migrate it in once, before switching over:
```
cd server
npm install
MONGODB_URI="<your connection string>" node scripts/migrate_to_mongo.js
```

**Run the server**:
```
cd server
npm install
MONGODB_URI="<your connection string>" ADMIN_KEY="pick-a-long-random-string" PORT=4000 npm start
```

Put it on any VPS (DigitalOcean, a Windows VPS you already rent for MT5, etc.)
or a platform like Render (see below) and put a reverse proxy with HTTPS in
front of it if it's not already provided (Caddy or nginx + certbot are the
easiest). MT5's `WebRequest` **requires HTTPS** for anything other than
localhost, so don't skip this step. For quick local testing, you can also
tunnel your local server with `ngrok http 4000` and use the public
`https://...ngrok-free.app` URL it gives you everywhere below.

Subscription packages (name/price/duration) are defined in
`server/packages.js` — edit that file to change what's on offer.

### Deploying to Render (free tier)

1. Push this repo to GitHub (Render deploys from a git repo):
   ```
   git init
   git add server admin_gui client_gui client_ea admin_ea README.md .gitignore
   git commit -m "Initial commit"
   ```
   then create a repo on GitHub and push to it. **Do not commit `server/data/`
   or the client `.exe`** — see `.gitignore` and the note on hosting the
   `.exe` below.
2. On [render.com](https://render.com): **New → Web Service**, connect the
   repo, set:
   - **Root Directory**: `server`
   - **Build Command**: `npm install`
   - **Start Command**: `npm start`
   - **Instance Type**: Free
3. Under **Environment**, add:
   - `MONGODB_URI` — your Atlas connection string
   - `ADMIN_KEY` — the same long random string you use everywhere else
   (`PORT` is set automatically by Render — the server already reads it.)
4. Deploy. Render gives you a permanent `https://your-app.onrender.com` URL —
   no ngrok needed anymore. Update `SERVER_URL` in `admin_gui/admin_gui.py`
   and `client_gui/client_app.py` to that URL, and the WebRequest allow-list
   in the Admin EA's MT5 settings, then rebuild both `.exe`s.
5. The free tier spins the service down after 15 minutes idle and takes a few
   seconds to wake back up on the next request — the client/admin apps' retry
   logic already tolerates this. If that cold-start delay becomes a problem,
   upgrade the web service's instance type (data is unaffected either way,
   since it lives in MongoDB, not on Render's disk).

**Hosting the client `.exe`**: it's ~80MB, too large to be reasonable to
commit to git. Instead, upload each build as a
[GitHub Release](https://github.com) asset on this repo and point
`server/client_version.js`'s `download_url` at that asset's direct URL
(a full `https://github.com/.../releases/download/...` URL — `client_version.js`
already supports absolute URLs, not just paths on this server) instead of
`/downloads/LumoraScalpClient.exe`.

## 2. Set up the Admin EA

1. Compile `admin_ea/LumoraScalper_AdminCopier.mq5` in MetaEditor (this is the
   original Nyao Scalper strategy — trading logic untouched, copyright/
   attribution intact — with a "🔗 Trade Copier (Admin)" input group and
   broadcast hooks for OPEN, MODIFY (including every trailing-stop update —
   all SL/TP changes funnel through one function), and CLOSE added, plus the
   recommended "best values" preset baked in as the input defaults).
2. In MT5: **Tools → Options → Expert Advisors → Allow WebRequest for listed
   URLs**, add your server's `https://your-domain.com`.
3. Attach the EA to your XAUUSD chart as usual, and additionally set:
   - `EnableTradeCopier = true`
   - `CopierServerURL = https://your-domain.com`
   - `CopierAdminKey = <the ADMIN_KEY you set on the server>`
   - `DailyProfitLimitToStopCopying` (optional, `$`, `0` = disabled) — once
     today's realized profit reaches this, the EA stops broadcasting *new*
     OPEN signals to clients. It keeps trading its own account normally, and
     still sends MODIFY/CLOSE for positions clients already copied, so
     nothing is left stranded without SL updates or a close.

Every open, SL/TP change (including trailing stop), and close now gets
POSTed to the server automatically.

## 3. Onboard a client — subscription flow

Clients are self-service. Give them `client_gui/client_app.py` (or the
packaged `.exe` — see below); the app walks them through everything:

1. **Register** — the client opens the app, enters the Server URL you gave
   them plus their name/email/MT5 login, and clicks **Register**.
2. **Select a package & submit payment** — they pick one of the packages
   from `server/packages.js`, pay you directly (bank transfer, crypto,
   whatever you use), and enter the payment reference/transaction ID into
   the app.
3. **Admin approves** — in the **Admin GUI**'s **Pending Approvals** tab,
   you'll see their name, chosen package, and payment reference. Verify the
   payment arrived and click **Approve** (or **Reject** with a reason if it
   didn't). Approving activates their subscription for that package's
   duration (30 days by default).
4. **Dashboard access** — once approved, the client's app unlocks the
   trading dashboard: they open MT5, log into their own account, enable
   **Algo Trading** (top toolbar button), set the Symbol field to whatever
   *their own broker* calls gold (e.g. `XAUUSD`, `XAUUSDm`, `GOLD` — brokers
   name it differently, and it does not need to match the admin's broker's
   name for it), and click **Start Copying**. Trade size always mirrors the
   admin's exact volume — there's no lot-sizing choice to make.

The client app shows a subscription banner with days remaining, turning to a
warning color inside 7 days. When a subscription lapses, the client is
automatically bounced back to the package/payment screen and the copier
engine stops — no manual disabling needed. From the **Clients** tab, an admin
can:
- **Extend Subscription** — renews a client (typically after they submit a
  new payment reference through the same in-app flow) from whichever is
  later, today or their current expiry, so renewing early doesn't waste
  remaining days.
- **Halt** — manually suspends a client's access regardless of expiry.
- **Add Client** — a shortcut for phone/offline sales: creates the client
  already active with a chosen package/duration, bypassing the approval
  queue.

That's it — no MetaEditor, no dragging an EA onto a chart. The app talks
straight to the running MT5 terminal via MetaQuotes' official `MetaTrader5`
Python package and places/modifies/closes trades itself. Both the Admin GUI
and the client's own app show live profit.

### Alternative: MT5-EA-based client (no Python package needed)

If a client would rather not install `pip install MetaTrader5` (e.g. running
MT5 on a VPS where you don't want extra Python tooling), `client_ea/NyaoCopier_ClientEA.mq5`
does the same job as a chart-attached Expert Advisor instead. Functionally
equivalent for trade copying — it talks to the same server API, but it does
not implement the subscription flow above, so onboard EA-based clients with
the **Add Client** shortcut in the Admin GUI instead.


## 4. Packaging the desktop apps as `.exe`

This sandbox can't produce Windows binaries, so build the `.exe` on a Windows
machine (or ask the admin/client to run the `.py` directly with Python
installed — that works too):

```
pip install "numpy<2" --upgrade pyinstaller MetaTrader5 Pillow qrcode
pyinstaller --onefile --windowed --name LumoraScalpingAdmin admin_gui/admin_gui.py
pyinstaller --onefile --windowed --collect-all MetaTrader5 --collect-all numpy --collect-all PIL --collect-all qrcode --name LumoraScalpingClient client_gui/client_app.py
```

The client build needs the extra flags to avoid a few PyInstaller pitfalls:
- `--collect-all MetaTrader5` — PyInstaller's dependency scanner can silently
  miss `MetaTrader5`'s compiled extension in `--onefile` mode, which produces
  a client-facing "MetaTrader5 package failed to load" error even though the
  client has MT5 installed and running — the package just never made it into
  the `.exe`.
- `numpy<2` plus `--collect-all numpy` — numpy 2.x renamed its internal
  `numpy.core` module to `numpy._core`; older PyInstaller hooks don't bundle
  the new binary layout correctly, producing a
  `numpy._core.multiarray failed to import` error inside the frozen exe.
- `--collect-all PIL --collect-all qrcode` — used by the "Download Profit
  Card" feature (Pillow renders the card image, `qrcode` generates its QR
  code). Missing this only breaks that one button (its import failure is
  caught and shown as an error dialog, not a crash), but bundle it anyway.

If you ever hit any of these reports from a client, delete `build/`/`dist/`
and rebuild with the flags above.

`MetaTrader5` (the pip package) only works on Windows, so the client app —
and its packaged `.exe` — only run on Windows, same as MT5 itself.

## 5. Shipping an update to clients (auto-update)

The client app checks the server for a newer version on every launch. If one's
available, it downloads it in the background and shows a **Restart to
Update** button — nothing replaces itself silently or without the client
clicking through a confirmation. To ship an update:

1. Bump `CLIENT_VERSION` in `client_gui/client_app.py`.
2. Rebuild the exe (see step 4 above) as `LumoraScalpClient.exe`.
3. Upload that file as a new [GitHub Release](https://github.com) asset on
   this repo (see "Hosting the client `.exe`" above), and copy its direct
   asset URL.
4. In `server/client_version.js`, bump `version` to match step 1 exactly, and
   set `download_url` to that asset URL. Restart the server (it reads
   `client_version.js` at startup, so a redeploy/restart is required for this
   to take effect).

Already-running clients pick up the new version next time they launch (or if
you build a periodic re-check, whenever that fires). This only works for the
packaged `.exe` — running from source (`python client_app.py`) shows an
"update available, rebuild the .exe" notice instead of trying to replace the
Python interpreter, which would be both wrong and dangerous.

## Notes and limitations

- Each Client EA instance always trades the chart symbol it's attached to
  (and the Python client always trades whatever's in its Symbol field) —
  attach it to (or set it to) whatever *your own broker* calls gold. This
  does not need to match the admin's broker's symbol name for it; every
  incoming signal executes against the client's own symbol regardless.
- `MODIFY` signals (including trailing-stop updates) only apply if the
  client still has a mapped open position for that admin trade; if the
  client's fill was rejected, later modifies for it are silently skipped.
- The server keeps the most recent 5,000 signals (older ones are trimmed) so
  its single MongoDB document stays well under MongoDB's 16MB document limit
  indefinitely.
- Payment is handled as manual proof + admin approval (a reference/note the
  client types in, that you verify yourself) — there's no payment gateway
  integration, so nothing is charged automatically.
- Running a service that automatically places trades on other people's
  accounts, and charging a subscription for it, may require a license or
  registration in your jurisdiction (e.g. as a money manager or signal
  provider), separate from the software itself — worth checking with a local
  regulator or lawyer before offering this to clients commercially.
