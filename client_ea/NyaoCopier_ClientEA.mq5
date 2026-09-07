// +------------------------------------------------------------------+
// | Nyao Copier - Client EA                                          |
// | Mirrors trades broadcast by the Admin EA (Nyao Scalper) onto     |
// | this MT5 account, via the Nyao Copier relay server.              |
// |                                                                   |
// | This EA does NOT run its own strategy. It only:                  |
// |  1. Polls the server for new signals (OPEN / MODIFY / CLOSE)     |
// |  2. Opens/modifies/closes positions on THIS account to match     |
// |  3. Reports back what happened + current profit                 |
// |  4. Sends a periodic heartbeat (balance/equity) for the admin    |
// |     dashboard                                                    |
// +------------------------------------------------------------------+
#property copyright "Nyao Copier Client"
#property version   "1.0"
#property strict

input group "🔗 Copier Connection"
input string ServerURL      = "https://your-server.example.com"; // Copier Server Base URL
input string ClientID       = "";                                 // Client ID (given by admin)
input string ClientKey      = "";                                 // Client Key (given by admin)
input int    PollIntervalSeconds = 2;                              // How often to poll for new signals
input int    HttpTimeoutMs  = 5000;                                // HTTP timeout (ms)

input group "⚙️ Copy Behaviour"
input bool   CopyStopLoss   = true;                                 // Copy admin's SL (recommended)
input bool   CopyTakeProfit = true;                                 // Copy admin's TP
input bool   CopyModifications = true;                              // Mirror trailing-stop / SL-TP changes
input int    MaxSlippagePoints = 30;                                // Max slippage allowed on market orders
input int    MagicNumberClient = 7788990;                           // Magic number for this EA's own positions
input bool   EnableLogging  = true;                                 // Print activity to Experts log

// +------------------------------------------------------------------+
// | Internal state                                                    |
// +------------------------------------------------------------------+
#define LogPrint if(EnableLogging) Print

int    lastSignalId = 0;

// Maps an admin position_id -> the client_ticket we opened for it, so MODIFY/CLOSE
// signals (which only reference the admin's ticket) know which local position to act on.
struct CopyMap { ulong admin_pos_id; ulong client_ticket; };
CopyMap mapping[];

int OnInit()
{
    if(ClientID == "" || ClientKey == "")
    {
        Alert("Nyao Copier: ClientID / ClientKey not set. Get these from your admin.");
        return INIT_PARAMETERS_INCORRECT;
    }
    EventSetTimer(MathMax(PollIntervalSeconds, 1));
    LogPrint("[COPIER] Client EA started. Server: ", ServerURL);
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
    EventKillTimer();
}

void OnTick() {} // all work happens on the timer; no independent strategy logic here

void OnTimer()
{
    PollSignals();
    SendHeartbeat();
}

// +------------------------------------------------------------------+
// | Poll server for new signals and act on each one                  |
// +------------------------------------------------------------------+
void PollSignals()
{
    string url = ServerURL + "/api/client/signals?since=" + IntegerToString(lastSignalId);
    string headers = AuthHeaders();
    char post[]; char result[]; string resultHeaders = "";

    int res = WebRequest("GET", url, headers, HttpTimeoutMs, post, result, resultHeaders);
    if(res == -1)
    {
        LogPrint("[COPIER] Poll failed (", GetLastError(),
                 "). Add the server URL under Tools > Options > Expert Advisors > Allow WebRequest.");
        return;
    }
    if(res != 200)
    {
        LogPrint("[COPIER] Poll error ", res, ": ", CharArrayToString(result));
        return;
    }

    string body = CharArrayToString(result);
    // Response is a JSON array of signal objects. Parse it with the tiny
    // helper parser below (no external JSON library needed in MQL5).
    JsonArray signals;
    if(!ParseSignalArray(body, signals)) return;

    for(int i = 0; i < signals.count; i++)
    {
        HandleSignal(signals.items[i]);
        if(signals.items[i].id > lastSignalId) lastSignalId = signals.items[i].id;
    }
}

// +------------------------------------------------------------------+
// | Act on a single signal                                            |
// +------------------------------------------------------------------+
void HandleSignal(SignalData &sig)
{
    if(sig.action == "OPEN") DoOpen(sig);
    else if(sig.action == "MODIFY" && CopyModifications) DoModify(sig);
    else if(sig.action == "PARTIAL_CLOSE") DoPartialClose(sig);
    else if(sig.action == "CLOSE") DoClose(sig);
}

// Always mirrors the admin's exact trade size - no client-side lot scaling
// or risk sizing. The only adjustment is snapping to the broker's allowed
// volume step/min/max, which isn't a choice, it's a requirement for the
// order to be accepted at all.
double ComputeLot(double adminVolume)
{
    double lot = adminVolume;
    double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
    double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    lot = MathFloor(lot / stepLot) * stepLot;
    lot = MathMax(minLot, MathMin(maxLot, lot));
    return NormalizeDouble(lot, 2);
}

// Different brokers support different order-filling modes per symbol (a
// bitmask via SYMBOL_FILLING_MODE). Hardcoding ORDER_FILLING_IOC - as this
// used to do - makes OrderSend() silently reject the trade on any broker
// that doesn't support that exact mode for this symbol.
ENUM_ORDER_TYPE_FILLING PickFillingMode()
{
    int mode = (int)SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
    if((mode & SYMBOL_FILLING_IOC) != 0) return ORDER_FILLING_IOC;
    if((mode & SYMBOL_FILLING_FOK) != 0) return ORDER_FILLING_FOK;
    return ORDER_FILLING_RETURN;
}

void DoOpen(SignalData &sig)
{
    // No symbol-name matching against the admin's signal here on purpose:
    // every trade always executes against this chart's own _Symbol, which
    // is whatever THIS client's broker calls gold - it doesn't need to
    // match the admin's broker's literal symbol string (e.g. admin's
    // "XAUUSD" vs this client's broker-suffixed "XAUUSDm").
    double lot = ComputeLot(sig.volume);
    if(lot <= 0) { LogPrint("[COPIER] Computed lot <= 0, skipping OPEN for admin pos ", sig.position_id); return; }

    MqlTradeRequest request = {};
    MqlTradeResult  result  = {};

    request.action    = TRADE_ACTION_DEAL;
    request.symbol     = _Symbol;
    request.volume      = lot;
    request.type        = (sig.type == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
    request.price        = (sig.type == "BUY") ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
    request.deviation     = MaxSlippagePoints;
    request.magic          = MagicNumberClient;
    request.comment         = "copy#" + IntegerToString(sig.position_id);
    request.sl              = CopyStopLoss   ? sig.sl : 0;
    request.tp              = CopyTakeProfit ? sig.tp : 0;
    request.type_filling      = PickFillingMode();

    if(!OrderSend(request, result) || result.retcode != TRADE_RETCODE_DONE)
    {
        LogPrint("[COPIER] OPEN failed for admin pos ", sig.position_id, " retcode=", result.retcode,
                 " GetLastError()=", GetLastError());
        ReportToServer(sig.id, 0, "FAILED", 0, 0);
        return;
    }

    AddMapping(sig.position_id, result.order);
    LogPrint("[COPIER] Opened ", sig.type, " ", lot, " lots, ticket=", result.order, " (mirrors admin pos ", sig.position_id, ")");
    ReportToServer(sig.id, result.order, "OPENED", result.price, 0);
}

void DoModify(SignalData &sig)
{
    ulong ticket = FindMapping(sig.position_id);
    if(ticket == 0) return; // we don't have a local position for this admin position
    if(!PositionSelectByTicket(ticket)) return;

    MqlTradeRequest request = {};
    MqlTradeResult  result  = {};
    request.action   = TRADE_ACTION_SLTP;
    request.position   = ticket;
    request.symbol       = _Symbol;
    request.sl             = CopyStopLoss   ? sig.sl : PositionGetDouble(POSITION_SL);
    request.tp             = CopyTakeProfit ? sig.tp : PositionGetDouble(POSITION_TP);

    if(OrderSend(request, result) && result.retcode == TRADE_RETCODE_DONE)
    {
        LogPrint("[COPIER] Modified ticket ", ticket, " SL=", sig.sl, " TP=", sig.tp);
        ReportToServer(sig.id, ticket, "MODIFIED", 0, 0);
    }
    else
    {
        LogPrint("[COPIER] MODIFY failed for ticket ", ticket, " retcode=", result.retcode,
                 " GetLastError()=", GetLastError());
    }
}

void DoPartialClose(SignalData &sig)
{
    ulong ticket = FindMapping(sig.position_id);
    if(ticket == 0) return;
    if(!PositionSelectByTicket(ticket)) return;

    // Mirrors the admin's exact closed volume (same principle as OPEN),
    // clamped to what's actually left on this position and snapped to the
    // broker's volume step.
    double volume = PositionGetDouble(POSITION_VOLUME);
    double closeVol = MathMin(sig.volume, volume);
    double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    double minLot   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    closeVol = MathFloor(closeVol / stepLot) * stepLot;
    if(closeVol <= 0) return;

    double remaining = NormalizeDouble(volume - closeVol, 2);
    if(remaining < minLot)
    {
        // Nothing tradeable would be left on this broker - close it fully instead.
        DoClose(sig);
        return;
    }

    ENUM_POSITION_TYPE ptype = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

    MqlTradeRequest request = {};
    MqlTradeResult  result  = {};
    request.action    = TRADE_ACTION_DEAL;
    request.position    = ticket;
    request.symbol        = _Symbol;
    request.volume          = closeVol;
    request.type              = (ptype == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
    request.price              = (ptype == POSITION_TYPE_BUY) ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    request.deviation           = MaxSlippagePoints;
    request.magic                = MagicNumberClient;
    request.type_filling           = PickFillingMode();

    if(OrderSend(request, result) && result.retcode == TRADE_RETCODE_DONE)
    {
        LogPrint("[COPIER] Partially closed ", closeVol, " lots of ticket ", ticket,
                 " (mirrors admin pos ", sig.position_id, ")");
        ReportToServer(sig.id, ticket, "PARTIAL_CLOSED", result.price, 0);
    }
    else
    {
        LogPrint("[COPIER] PARTIAL_CLOSE failed for ticket ", ticket, " retcode=", result.retcode,
                 " GetLastError()=", GetLastError());
    }
}

void DoClose(SignalData &sig)
{
    ulong ticket = FindMapping(sig.position_id);
    if(ticket == 0) return;
    if(!PositionSelectByTicket(ticket)) { RemoveMapping(sig.position_id); return; }

    double volume = PositionGetDouble(POSITION_VOLUME);
    ENUM_POSITION_TYPE ptype = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);

    MqlTradeRequest request = {};
    MqlTradeResult  result  = {};
    request.action    = TRADE_ACTION_DEAL;
    request.position    = ticket;
    request.symbol        = _Symbol;
    request.volume          = volume;
    request.type              = (ptype == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
    request.price              = (ptype == POSITION_TYPE_BUY) ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    request.deviation           = MaxSlippagePoints;
    request.magic                = MagicNumberClient;
    request.type_filling           = PickFillingMode();

    double profitBeforeClose = PositionGetDouble(POSITION_PROFIT);

    if(OrderSend(request, result) && result.retcode == TRADE_RETCODE_DONE)
    {
        LogPrint("[COPIER] Closed ticket ", ticket, " (mirrors admin pos ", sig.position_id, ") P/L=", profitBeforeClose);
        ReportToServer(sig.id, ticket, "CLOSED", result.price, profitBeforeClose);
        RemoveMapping(sig.position_id);
    }
    else
    {
        LogPrint("[COPIER] CLOSE failed for ticket ", ticket, " retcode=", result.retcode,
                 " GetLastError()=", GetLastError());
    }
}

// +------------------------------------------------------------------+
// | Report a signal's outcome back to the server                     |
// +------------------------------------------------------------------+
void ReportToServer(int signalId, ulong clientTicket, string status, double price, double profit)
{
    string json = "{";
    json += "\"signal_id\":" + IntegerToString(signalId) + ",";
    json += "\"client_ticket\":" + IntegerToString((long)clientTicket) + ",";
    json += "\"status\":\"" + status + "\",";
    json += "\"price\":" + DoubleToString(price, _Digits) + ",";
    json += "\"profit\":" + DoubleToString(profit, 2);
    json += "}";

    char post[]; char result[]; string resultHeaders = "";
    StringToCharArray(json, post, 0, WHOLE_ARRAY, CP_UTF8);
    ArrayResize(post, ArraySize(post) - 1);

    string url = ServerURL + "/api/client/report";
    WebRequest("POST", url, AuthHeaders(), HttpTimeoutMs, post, result, resultHeaders);
}

// +------------------------------------------------------------------+
// | Periodic account snapshot so the admin dashboard shows live data |
// +------------------------------------------------------------------+
datetime lastHeartbeat = 0;
void SendHeartbeat()
{
    double balance = AccountInfoDouble(ACCOUNT_BALANCE);
    double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
    double floatingProfit = equity - balance;

    string json = "{";
    json += "\"balance\":" + DoubleToString(balance, 2) + ",";
    json += "\"equity\":" + DoubleToString(equity, 2) + ",";
    json += "\"floating_profit\":" + DoubleToString(floatingProfit, 2);
    json += "}";

    char post[]; char result[]; string resultHeaders = "";
    StringToCharArray(json, post, 0, WHOLE_ARRAY, CP_UTF8);
    ArrayResize(post, ArraySize(post) - 1);

    string url = ServerURL + "/api/client/heartbeat";
    WebRequest("POST", url, AuthHeaders(), HttpTimeoutMs, post, result, resultHeaders);
}

string AuthHeaders()
{
    return "Content-Type: application/json\r\nX-Client-Id: " + ClientID + "\r\nX-Client-Key: " + ClientKey + "\r\n";
}

// +------------------------------------------------------------------+
// | admin_pos_id <-> client_ticket mapping helpers                    |
// +------------------------------------------------------------------+
void AddMapping(ulong adminPosId, ulong clientTicket)
{
    int n = ArraySize(mapping);
    ArrayResize(mapping, n + 1);
    mapping[n].admin_pos_id = adminPosId;
    mapping[n].client_ticket = clientTicket;
}

ulong FindMapping(ulong adminPosId)
{
    for(int i = 0; i < ArraySize(mapping); i++)
        if(mapping[i].admin_pos_id == adminPosId) return mapping[i].client_ticket;
    return 0;
}

void RemoveMapping(ulong adminPosId)
{
    for(int i = 0; i < ArraySize(mapping); i++)
    {
        if(mapping[i].admin_pos_id == adminPosId)
        {
            for(int j = i; j < ArraySize(mapping) - 1; j++) mapping[j] = mapping[j + 1];
            ArrayResize(mapping, ArraySize(mapping) - 1);
            return;
        }
    }
}

// +------------------------------------------------------------------+
// | Minimal JSON parsing (only what this EA needs — flat array of    |
// | flat objects with known keys). Not a general-purpose JSON parser.|
// +------------------------------------------------------------------+
struct SignalData
{
    int    id;
    string action;
    string symbol;
    string type;
    long   admin_ticket;
    long   position_id;
    double volume;
    double sl;
    double tp;
    double price;
    string comment;
};

struct JsonArray
{
    SignalData items[];
    int count;
};

string JsonGetString(string obj, string key)
{
    string pattern = "\"" + key + "\":\"";
    int p = StringFind(obj, pattern);
    if(p == -1) return "";
    p += StringLen(pattern);
    int e = StringFind(obj, "\"", p);
    if(e == -1) return "";
    return StringSubstr(obj, p, e - p);
}

double JsonGetNumber(string obj, string key)
{
    string pattern = "\"" + key + "\":";
    int p = StringFind(obj, pattern);
    if(p == -1) return 0;
    p += StringLen(pattern);
    int e = p;
    while(e < StringLen(obj))
    {
        ushort c = StringGetCharacter(obj, e);
        if((c >= '0' && c <= '9') || c == '-' || c == '.' ) e++;
        else break;
    }
    return StringToDouble(StringSubstr(obj, p, e - p));
}

// Splits a top-level JSON array of objects "[{...},{...}]" into its object strings
bool ParseSignalArray(string body, JsonArray &out)
{
    out.count = 0;
    ArrayResize(out.items, 0);

    body = StringTrimLeft(body);
    body = StringTrimRight(body);
    if(StringLen(body) < 2 || StringGetCharacter(body, 0) != '[') return false;

    int depth = 0;
    int start = -1;
    for(int i = 0; i < StringLen(body); i++)
    {
        ushort c = StringGetCharacter(body, i);
        if(c == '{')
        {
            if(depth == 0) start = i;
            depth++;
        }
        else if(c == '}')
        {
            depth--;
            if(depth == 0 && start != -1)
            {
                string objStr = StringSubstr(body, start, i - start + 1);
                SignalData sd;
                sd.id           = (int)JsonGetNumber(objStr, "id");
                sd.action        = JsonGetString(objStr, "action");
                sd.symbol         = JsonGetString(objStr, "symbol");
                sd.type            = JsonGetString(objStr, "type");
                sd.admin_ticket     = (long)JsonGetNumber(objStr, "admin_ticket");
                sd.position_id       = (long)JsonGetNumber(objStr, "position_id");
                sd.volume             = JsonGetNumber(objStr, "volume");
                sd.sl                  = JsonGetNumber(objStr, "sl");
                sd.tp                   = JsonGetNumber(objStr, "tp");
                sd.price                 = JsonGetNumber(objStr, "price");
                sd.comment                = JsonGetString(objStr, "comment");

                int n = out.count;
                ArrayResize(out.items, n + 1);
                out.items[n] = sd;
                out.count++;
                start = -1;
            }
        }
    }
    return true;
}
