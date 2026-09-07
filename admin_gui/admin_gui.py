"""
Lumora Scalping - Admin Dashboard (desktop GUI)
--------------------------------------------
Shows every connected client (their MT5 login, subscription status, and live
P/L), lets you review and approve/reject pending payments, extend or halt a
subscription, and add clients directly. This app is a pure client of the
Copier Server's REST API -- it does not talk to MT5 directly (the Admin EA
does that).

Run:      python admin_gui.py
Package:  pyinstaller --onefile --windowed --name LumoraScalpingAdmin admin_gui.py
          (run this ON WINDOWS to get a distributable .exe)
"""

import json
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import urllib.request
import urllib.error

APP_TITLE = "Lumora Scalping — Admin"

# Hardcoded so the admin doesn't have to re-enter these every launch. Keep
# this in sync with the server's actual ADMIN_KEY and the tunnel's current
# domain — see the matching SERVER_URL constant in client_gui/client_app.py.
SERVER_URL = "https://clamor-alienable-scouts.ngrok-free.dev"
ADMIN_KEY = "fd58ed032d16bc0e129676c90193bca0f5315c759acbf67e"

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

REFRESH_SECONDS = 3


def api_request(server_url, admin_key, path, method="GET", body=None):
    url = server_url.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Admin-Key", admin_key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


class AdminApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1080x600")
        self.configure(bg=COLORS["bg"])
        self._apply_style()

        self.cfg = {"server_url": SERVER_URL, "admin_key": ADMIN_KEY}
        self._stop = False
        self.packages = []
        self.payment_methods = []

        self._build_connection_bar()
        self._build_tabs()

        self._load_packages()
        self.after(200, self.refresh_once)
        self._start_polling()

    # ---------------------------------------------------------------- styling
    def _apply_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TButton", background=COLORS["panel"], foreground=COLORS["text"],
                         borderwidth=0, padding=6)
        style.map("TButton", background=[("active", COLORS["bg2"])])
        style.configure("Accent.TButton", background=COLORS["accent2"], foreground="#ffffff",
                         borderwidth=0, padding=6)
        style.map("Accent.TButton", background=[("active", COLORS["accent"])])
        style.configure("TCombobox", fieldbackground=COLORS["bg2"], background=COLORS["bg2"],
                         foreground=COLORS["text"])
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=COLORS["panel"], foreground=COLORS["text"], padding=(12, 6))
        style.map("TNotebook.Tab", background=[("selected", COLORS["accent2"])])
        style.configure("Treeview", background=COLORS["bg2"], fieldbackground=COLORS["bg2"],
                         foreground=COLORS["text"], borderwidth=0, rowheight=24)
        style.configure("Treeview.Heading", background=COLORS["panel"], foreground=COLORS["accent"],
                         borderwidth=0)
        style.map("Treeview", background=[("selected", COLORS["accent2"])])

    # ---------------------------------------------------------------- UI
    def _build_connection_bar(self):
        bar = tk.Frame(self, bg=COLORS["bg"])
        bar.pack(fill="x", padx=8, pady=8)

        tk.Label(bar, text="LUMORA SCALPING — ADMIN", bg=COLORS["bg"], fg=COLORS["accent"],
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=(0, 20))

        self.status_lbl = tk.Label(bar, text="Not connected", bg=COLORS["bg"], fg=COLORS["muted"])
        self.status_lbl.pack(side="left", padx=10)

    def _build_tabs(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.clients_tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        self.approvals_tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        self.methods_tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        self.notebook.add(self.clients_tab, text="Clients")
        self.notebook.add(self.approvals_tab, text="Pending Approvals")
        self.notebook.add(self.methods_tab, text="Payment Methods")

        self._build_clients_tab()
        self._build_approvals_tab()
        self._build_payment_methods_tab()

    def _build_clients_tab(self):
        cols = ("name", "login", "status", "expires", "lot_mode", "lot_value", "balance", "equity",
                "floating", "today_pnl", "total_pnl", "trades", "online")
        headers = ("Name", "MT5 Login", "Status", "Expires", "Lot Mode", "Lot Value", "Balance",
                   "Equity", "Floating P/L", "Today P/L", "Total P/L", "Closed Trades", "Online")

        self.tree = ttk.Treeview(self.clients_tab, columns=cols, show="headings", height=16)
        for c, h in zip(cols, headers):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=85, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=4, pady=4)
        self._client_ids = {}  # tree item id -> client id

        bar = tk.Frame(self.clients_tab, bg=COLORS["bg"])
        bar.pack(fill="x", padx=4, pady=(0, 6))
        ttk.Button(bar, text="Add Client", command=self.add_client).pack(side="left")
        ttk.Button(bar, text="Toggle Active", command=self.toggle_active).pack(side="left", padx=6)
        ttk.Button(bar, text="Extend Subscription", style="Accent.TButton", command=self.extend_client).pack(side="left", padx=6)
        ttk.Button(bar, text="Halt", command=self.halt_client).pack(side="left", padx=6)
        ttk.Button(bar, text="Show Client Key", command=self.show_key).pack(side="left", padx=6)
        ttk.Button(bar, text="Refresh Now", command=self.refresh_once).pack(side="left", padx=6)
        self.summary_lbl = tk.Label(bar, text="", bg=COLORS["bg"], fg=COLORS["muted"])
        self.summary_lbl.pack(side="right")

    def _build_approvals_tab(self):
        cols = ("name", "email", "package", "price", "reference", "note", "submitted")
        headers = ("Name", "Email", "Package", "Price", "Reference", "Note", "Submitted")

        self.approvals_tree = ttk.Treeview(self.approvals_tab, columns=cols, show="headings", height=16)
        for c, h in zip(cols, headers):
            self.approvals_tree.heading(c, text=h)
            self.approvals_tree.column(c, width=120, anchor="center")
        self.approvals_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self._approval_ids = {}

        bar = tk.Frame(self.approvals_tab, bg=COLORS["bg"])
        bar.pack(fill="x", padx=4, pady=(0, 6))
        ttk.Button(bar, text="Approve", style="Accent.TButton", command=self.approve_selected).pack(side="left")
        ttk.Button(bar, text="Reject", command=self.reject_selected).pack(side="left", padx=6)
        ttk.Button(bar, text="Refresh Now", command=self.refresh_once).pack(side="left", padx=6)

    def _build_payment_methods_tab(self):
        cols = ("label", "details", "enabled")
        headers = ("Label", "Details", "Enabled")

        self.methods_tree = ttk.Treeview(self.methods_tab, columns=cols, show="headings", height=16)
        for c, h in zip(cols, headers):
            self.methods_tree.heading(c, text=h)
            self.methods_tree.column(c, width=320 if c == "details" else 130,
                                      anchor="w" if c == "details" else "center")
        self.methods_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self._method_ids = {}

        bar = tk.Frame(self.methods_tab, bg=COLORS["bg"])
        bar.pack(fill="x", padx=4, pady=(0, 6))
        ttk.Button(bar, text="Add Method", style="Accent.TButton", command=self.add_payment_method).pack(side="left")
        ttk.Button(bar, text="Edit", command=self.edit_payment_method).pack(side="left", padx=6)
        ttk.Button(bar, text="Toggle Enabled", command=self.toggle_payment_method).pack(side="left", padx=6)
        ttk.Button(bar, text="Delete", command=self.delete_payment_method).pack(side="left", padx=6)
        ttk.Button(bar, text="Refresh Now", command=self.refresh_once).pack(side="left", padx=6)

    # ------------------------------------------------------------ actions
    def _load_packages(self):
        try:
            self.packages = api_request(self.cfg["server_url"], self.cfg["admin_key"], "/api/packages")
        except Exception:
            self.packages = []

    def add_client(self):
        if not self.packages:
            self._load_packages()
        self._open_add_client_dialog()

    def _open_add_client_dialog(self):
        dlg = tk.Toplevel(self, bg=COLORS["panel"])
        dlg.title("Add Client")
        dlg.configure(bg=COLORS["panel"])
        dlg.transient(self)
        dlg.grab_set()

        name_var = tk.StringVar()
        email_var = tk.StringVar()
        login_var = tk.StringVar()
        pkg_ids = [p["id"] for p in self.packages] or ["starter"]
        pkg_var = tk.StringVar(value=pkg_ids[0])
        default_days = self.packages[0]["duration_days"] if self.packages else 30
        days_var = tk.StringVar(value=str(default_days))

        def row(label, widget):
            r = tk.Frame(dlg, bg=COLORS["panel"])
            r.pack(fill="x", padx=12, pady=4)
            tk.Label(r, text=label, bg=COLORS["panel"], fg=COLORS["muted"], width=16, anchor="w").pack(side="left")
            widget.pack(in_=r, side="left", fill="x", expand=True)

        entry_kwargs = dict(bg=COLORS["bg2"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="flat")
        row("Name", tk.Entry(dlg, textvariable=name_var, **entry_kwargs))
        row("Email", tk.Entry(dlg, textvariable=email_var, **entry_kwargs))
        row("MT5 login", tk.Entry(dlg, textvariable=login_var, **entry_kwargs))
        row("Package", ttk.Combobox(dlg, textvariable=pkg_var, state="readonly", values=pkg_ids))
        row("Duration (days)", tk.Entry(dlg, textvariable=days_var, **entry_kwargs))

        result = {}

        def submit():
            if not name_var.get().strip():
                messagebox.showwarning("Missing name", "Name is required.", parent=dlg)
                return
            result["name"] = name_var.get().strip()
            result["email"] = email_var.get().strip()
            result["mt5_login"] = login_var.get().strip()
            result["package_id"] = pkg_var.get()
            try:
                result["duration_days"] = int(days_var.get())
            except ValueError:
                result["duration_days"] = None
            dlg.destroy()

        btn_bar = tk.Frame(dlg, bg=COLORS["panel"])
        btn_bar.pack(fill="x", padx=12, pady=(8, 12))
        ttk.Button(btn_bar, text="Create", style="Accent.TButton", command=submit).pack(side="right")
        ttk.Button(btn_bar, text="Cancel", command=dlg.destroy).pack(side="right", padx=6)

        dlg.wait_window()
        if "name" not in result:
            return

        body = {
            "name": result["name"], "email": result["email"], "mt5_login": result["mt5_login"],
            "lot_mode": "MULTIPLIER", "lot_value": 1.0,
        }
        if result["package_id"]:
            body["package_id"] = result["package_id"]
        if result["duration_days"]:
            body["duration_days"] = result["duration_days"]

        try:
            client = api_request(self.cfg["server_url"], self.cfg["admin_key"], "/api/admin/clients",
                                  method="POST", body=body)
        except Exception as e:
            messagebox.showerror("Error", f"Could not create client:\n{e}")
            return

        messagebox.showinfo(
            "Client Created - give these to the client",
            f"Client ID:\n{client['id']}\n\nClient Key:\n{client['api_key']}\n\n"
            "This client is already ACTIVE, paste these directly into the client app's onboarding "
            "screen (or they can register themselves next time to go through the normal payment flow). "
            "The key will not be shown in the table again - use 'Show Client Key' to retrieve it later."
        )
        self.refresh_once()

    def _selected_client_id(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select a client", "Select a row in the Clients tab first.")
            return None
        return self._client_ids.get(sel[0])

    def _selected_approval_id(self):
        sel = self.approvals_tree.selection()
        if not sel:
            messagebox.showinfo("Select a request", "Select a pending approval row first.")
            return None
        return self._approval_ids.get(sel[0])

    def toggle_active(self):
        cid = self._selected_client_id()
        if not cid:
            return
        try:
            clients = api_request(self.cfg["server_url"], self.cfg["admin_key"], "/api/admin/clients")
            current = next((c for c in clients if c["id"] == cid), None)
            if not current:
                return
            api_request(self.cfg["server_url"], self.cfg["admin_key"], f"/api/admin/clients/{cid}",
                        method="PATCH", body={"active": not current["active"]})
            self.refresh_once()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def extend_client(self):
        cid = self._selected_client_id()
        if not cid:
            return
        try:
            api_request(self.cfg["server_url"], self.cfg["admin_key"], f"/api/admin/clients/{cid}/extend",
                        method="POST")
            self.refresh_once()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def halt_client(self):
        cid = self._selected_client_id()
        if not cid:
            return
        if not messagebox.askyesno("Halt client", "Halt this client's subscription until they pay again?"):
            return
        try:
            api_request(self.cfg["server_url"], self.cfg["admin_key"], f"/api/admin/clients/{cid}/halt",
                        method="POST")
            self.refresh_once()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def approve_selected(self):
        cid = self._selected_approval_id()
        if not cid:
            return
        try:
            api_request(self.cfg["server_url"], self.cfg["admin_key"], f"/api/admin/clients/{cid}/approve",
                        method="POST")
            self.refresh_once()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def reject_selected(self):
        cid = self._selected_approval_id()
        if not cid:
            return
        reason = simpledialog.askstring("Reject payment", "Reason (optional):") or ""
        try:
            api_request(self.cfg["server_url"], self.cfg["admin_key"], f"/api/admin/clients/{cid}/reject",
                        method="POST", body={"reason": reason})
            self.refresh_once()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def show_key(self):
        cid = self._selected_client_id()
        if not cid:
            return
        try:
            info = api_request(self.cfg["server_url"], self.cfg["admin_key"], f"/api/admin/clients/{cid}/key")
            messagebox.showinfo("Client Key", f"Client ID:\n{info['id']}\n\nClient Key:\n{info['api_key']}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _open_payment_method_dialog(self, existing=None):
        dlg = tk.Toplevel(self, bg=COLORS["panel"])
        dlg.title("Edit Payment Method" if existing else "Add Payment Method")
        dlg.configure(bg=COLORS["panel"])
        dlg.transient(self)
        dlg.grab_set()

        label_var = tk.StringVar(value=existing["label"] if existing else "")
        enabled_var = tk.BooleanVar(value=existing["enabled"] if existing else True)

        row = tk.Frame(dlg, bg=COLORS["panel"])
        row.pack(fill="x", padx=12, pady=4)
        tk.Label(row, text="Label", bg=COLORS["panel"], fg=COLORS["muted"], width=14, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=label_var, bg=COLORS["bg2"], fg=COLORS["text"],
                 insertbackground=COLORS["text"], relief="flat").pack(side="left", fill="x", expand=True)

        tk.Label(dlg, text="Details (wallet address / bank info - shown to clients exactly as typed)",
                 bg=COLORS["panel"], fg=COLORS["muted"], wraplength=340, justify="left").pack(anchor="w", padx=12, pady=(8, 2))
        details_box = tk.Text(dlg, height=6, width=44, bg=COLORS["bg2"], fg=COLORS["text"],
                               insertbackground=COLORS["text"], relief="flat")
        details_box.pack(padx=12, fill="x")
        if existing:
            details_box.insert("1.0", existing.get("details", ""))

        tk.Checkbutton(dlg, text="Enabled (shown to clients)", variable=enabled_var,
                       bg=COLORS["panel"], fg=COLORS["text"], selectcolor=COLORS["bg2"],
                       activebackground=COLORS["panel"]).pack(anchor="w", padx=12, pady=(8, 0))

        result = {}

        def submit():
            if not label_var.get().strip():
                messagebox.showwarning("Missing label", "Label is required.", parent=dlg)
                return
            result["label"] = label_var.get().strip()
            result["details"] = details_box.get("1.0", "end").strip()
            result["enabled"] = enabled_var.get()
            dlg.destroy()

        btn_bar = tk.Frame(dlg, bg=COLORS["panel"])
        btn_bar.pack(fill="x", padx=12, pady=(10, 12))
        ttk.Button(btn_bar, text="Save", style="Accent.TButton", command=submit).pack(side="right")
        ttk.Button(btn_bar, text="Cancel", command=dlg.destroy).pack(side="right", padx=6)

        dlg.wait_window()
        return result if "label" in result else None

    def add_payment_method(self):
        result = self._open_payment_method_dialog()
        if not result:
            return
        try:
            api_request(self.cfg["server_url"], self.cfg["admin_key"], "/api/admin/payment-methods",
                        method="POST", body=result)
            self.refresh_once()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _selected_method_id(self):
        sel = self.methods_tree.selection()
        if not sel:
            messagebox.showinfo("Select a method", "Select a payment method row first.")
            return None
        return self._method_ids.get(sel[0])

    def edit_payment_method(self):
        mid = self._selected_method_id()
        if not mid:
            return
        existing = next((m for m in self.payment_methods if m["id"] == mid), None)
        if not existing:
            return
        result = self._open_payment_method_dialog(existing=existing)
        if not result:
            return
        try:
            api_request(self.cfg["server_url"], self.cfg["admin_key"], f"/api/admin/payment-methods/{mid}",
                        method="PATCH", body=result)
            self.refresh_once()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def toggle_payment_method(self):
        mid = self._selected_method_id()
        if not mid:
            return
        existing = next((m for m in self.payment_methods if m["id"] == mid), None)
        if not existing:
            return
        try:
            api_request(self.cfg["server_url"], self.cfg["admin_key"], f"/api/admin/payment-methods/{mid}",
                        method="PATCH", body={"enabled": not existing["enabled"]})
            self.refresh_once()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def delete_payment_method(self):
        mid = self._selected_method_id()
        if not mid:
            return
        if not messagebox.askyesno("Delete method", "Delete this payment method? This cannot be undone."):
            return
        try:
            api_request(self.cfg["server_url"], self.cfg["admin_key"], f"/api/admin/payment-methods/{mid}",
                        method="DELETE")
            self.refresh_once()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ----------------------------------------------------------- polling
    def _start_polling(self):
        def loop():
            while not self._stop:
                time.sleep(REFRESH_SECONDS)
                try:
                    self.refresh_once()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()

    def refresh_once(self):
        try:
            clients = api_request(self.cfg["server_url"], self.cfg["admin_key"], "/api/admin/clients")
            self.status_lbl.config(text="Connected", fg=COLORS["good"])
        except Exception as e:
            self.status_lbl.config(text=f"Disconnected ({e})", fg=COLORS["bad"])
            return

        self._populate_clients(clients)
        self._populate_approvals(clients)

        try:
            self.payment_methods = api_request(self.cfg["server_url"], self.cfg["admin_key"],
                                                "/api/admin/payment-methods")
        except Exception:
            self.payment_methods = []
        self._populate_payment_methods(self.payment_methods)

    def _populate_payment_methods(self, methods):
        for row in self.methods_tree.get_children():
            self.methods_tree.delete(row)
        self._method_ids.clear()

        for m in methods:
            preview = (m.get("details") or "").replace("\n", " ")
            if len(preview) > 70:
                preview = preview[:70] + "…"
            item = self.methods_tree.insert("", "end", values=(
                m["label"], preview, "Yes" if m["enabled"] else "No",
            ))
            self._method_ids[item] = m["id"]

    def _populate_clients(self, clients):
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._client_ids.clear()

        total_today, total_all = 0.0, 0.0
        for c in clients:
            pnl = c.get("pnl", {})
            total_today += pnl.get("todayProfit", 0)
            total_all += pnl.get("totalProfit", 0)
            online = "🟢" if c.get("online") else "⚪"
            days_left = c.get("days_left")
            expires = f"{days_left}d" if days_left is not None else "—"
            item = self.tree.insert("", "end", values=(
                c.get("name"), c.get("mt5_login"), c.get("status", "ACTIVE"), expires,
                c.get("lot_mode"), c.get("lot_value"), c.get("balance"), c.get("equity"),
                c.get("floating_profit"), pnl.get("todayProfit"), pnl.get("totalProfit"),
                pnl.get("closedTrades"), online,
            ))
            self._client_ids[item] = c["id"]

        self.summary_lbl.config(
            text=f"Clients: {len(clients)}   Today total: {total_today:.2f}   All-time total: {total_all:.2f}")

    def _populate_approvals(self, clients):
        for row in self.approvals_tree.get_children():
            self.approvals_tree.delete(row)
        self._approval_ids.clear()

        pkg_by_id = {p["id"]: p for p in self.packages}
        pending = [c for c in clients if c.get("status") == "PENDING_APPROVAL"]
        for c in pending:
            pp = c.get("pending_payment") or {}
            pkg = pkg_by_id.get(pp.get("package_id"), {})
            item = self.approvals_tree.insert("", "end", values=(
                c.get("name"), c.get("email", ""), pkg.get("name", pp.get("package_id", "")),
                f"{pkg.get('currency', '')} {pkg.get('price', '')}".strip(),
                pp.get("reference", ""), pp.get("note", ""), pp.get("submitted_at", ""),
            ))
            self._approval_ids[item] = c["id"]

        self.notebook.tab(self.approvals_tab, text=f"Pending Approvals ({len(pending)})")

    def destroy(self):
        self._stop = True
        super().destroy()


if __name__ == "__main__":
    app = AdminApp()
    app.mainloop()
