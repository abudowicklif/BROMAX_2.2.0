"""Windows-friendly floating research panel for the existing BROMAX API."""
import argparse
import base64
import json
import os
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import threading
from tkinter import simpledialog, ttk


class AnalysisPanel:
    def __init__(self, root, base_url, symbol):
        self.root=root
        self.base_url=base_url.rstrip("/")
        self.symbol=symbol
        self.username=os.getenv("BROMAX_AUTH_USERNAME","")
        self.password=os.getenv("BROMAX_AUTH_PASSWORD","")
        self.compact=False
        self.topmost=True
        self.credentials_prompted=False
        self.request_in_progress=False
        root.title("BROMAX · Live analysis")
        root.geometry("400x350")
        root.minsize(340,300)
        root.resizable(True,True)
        root.configure(background="#101719")
        root.attributes("-topmost",True)
        self.style=ttk.Style(root)
        self.style.theme_use("clam")
        self.style.configure("TFrame",background="#101719")
        self.style.configure("Card.TFrame",background="#182124")
        self.style.configure("TLabel",background="#101719",foreground="#dce8e4",font=("Segoe UI",10))
        self.style.configure("Muted.TLabel",background="#101719",foreground="#91a29c",font=("Segoe UI",9))
        self.style.configure("Hero.TLabel",background="#182124",foreground="#85d2a3",font=("Segoe UI",30,"bold"))
        self.style.configure("Metric.TLabel",background="#182124",foreground="#ecf1ee",font=("Segoe UI",14,"bold"))
        self.style.configure("Treeview",background="#182124",foreground="#dce8e4",fieldbackground="#182124",rowheight=27,borderwidth=0)
        self.style.configure("Treeview.Heading",background="#202b2e",foreground="#9fada8",font=("Segoe UI",9,"bold"))
        self.style.map("Treeview",background=[("selected","#274b3a")])
        self._build()
        self.root.after(200,self.refresh)

    def _build(self):
        header=ttk.Frame(self.root,padding=(12,10))
        header.pack(fill="x")
        ttk.Label(header,text="BROMAX",font=("Segoe UI",12,"bold")).pack(side="left")
        ttk.Button(header,text="Login",command=self._login).pack(side="right",padx=(5,0))
        self.top_button=ttk.Button(header,text="Always on top",command=self.toggle_topmost)
        self.top_button.pack(side="right",padx=(5,0))
        self.compact_button=ttk.Button(header,text="Compact",command=self.toggle_compact)
        self.compact_button.pack(side="right")

        self.summary=ttk.Frame(self.root,style="Card.TFrame",padding=14)
        self.summary.pack(fill="x",padx=12,pady=(0,10))
        self.fields={}
        for key,title in (
            ("digit","Recommended digit"),("probability","Win probability"),
            ("health","Health"),("duration","Contract duration"),
            ("volatility","Volatility approval"),("status","TRADE STATUS"),
        ):
            row=ttk.Frame(self.summary,style="Card.TFrame")
            row.pack(fill="x",pady=4)
            ttk.Label(row,text=title,style="Muted.TLabel",width=22).pack(side="left")
            value=ttk.Label(row,text="—",style="Metric.TLabel")
            value.pack(side="right")
            self.fields[key]=value
        actions=ttk.Frame(self.root,padding=(12,0,12,10))
        actions.pack(fill="x")
        ttk.Button(actions,text="Open dashboard",command=self.open_dashboard).pack(side="left")
        ttk.Button(actions,text="Hide",command=self.root.iconify).pack(side="right")

    def _request(self):
        headers={"Accept":"application/json"}
        if self.username and self.password:
            token=base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            headers["Authorization"]=f"Basic {token}"
        symbol=urllib.parse.quote(self.symbol)
        request=urllib.request.Request(f"{self.base_url}/decision?symbol={symbol}",headers=headers)
        with urllib.request.urlopen(request,timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def refresh(self):
        if self.request_in_progress:
            return
        self.request_in_progress=True
        threading.Thread(target=self._fetch_analysis,daemon=True).start()

    def _fetch_analysis(self):
        try:
            data=self._request()
            result=(data,None)
        except urllib.error.HTTPError as error:
            result=(None,("http",error.code))
        except (urllib.error.URLError,TimeoutError,ValueError,json.JSONDecodeError) as error:
            result=(None,("connection",str(error)))
        self.root.after(0,self._finish_refresh,*result)

    def _finish_refresh(self,data,error):
        self.request_in_progress=False
        if error is None:
            self._render(data)
        elif error[0]=="http":
            code=error[1]
            if code==401:
                if not (self.username and self.password) and not self.credentials_prompted:
                    self._ask_credentials()
            self._render_unavailable(f"API ERROR {code}")
        else:
            self._render_unavailable("API OFFLINE")
        self.root.after(2500,self.refresh)

    def _login(self):
        self.credentials_prompted=False
        self._ask_credentials()

    def _ask_credentials(self):
        self.credentials_prompted=True
        username=simpledialog.askstring("BROMAX login","API username:",parent=self.root)
        if not username:
            return
        password=simpledialog.askstring("BROMAX login","API password:",show="*",parent=self.root)
        if password:
            self.username=username
            self.password=password
            self.credentials_prompted=False
            self.root.after(50,self.refresh)

    def _render(self,data):
        signal=data.get("signal") if data.get("trade_authorized") is True else None
        digit_row=next((row for row in data.get("digits",[])
                        if signal and int(row.get("digit",-1))==int(signal.get("predicted_digit",-2))),None)
        qualified=bool(signal and digit_row and digit_row.get("decision")=="TRADE")
        volatility=data.get("volatility") or {}
        if not qualified:
            for key in ("probability","health","duration"):
                self.fields[key].configure(text="—")
            self.fields["digit"].configure(text="NO QUALIFIED SIGNAL")
            self.fields["volatility"].configure(text=volatility.get("status","INSUFFICIENT_EVIDENCE"))
            self.fields["status"].configure(text="NO TRADE",foreground="#d78e88")
            return
        self.fields["digit"].configure(text=str(signal["predicted_digit"]))
        self.fields["probability"].configure(text=f"{float(signal['probability'])*100:.1f}%")
        health=digit_row.get("health_percent")
        self.fields["health"].configure(text=f"{health}%" if health is not None else "—")
        self.fields["duration"].configure(text=f"{signal['duration']} tick(s)")
        self.fields["volatility"].configure(text="PASS" if volatility.get("status")=="APPROVED" else "FAIL")
        self.fields["status"].configure(text="TRADE · PAPER ONLY",foreground="#85d2a3")

    def _render_unavailable(self,status):
        for key in ("digit","probability","health","duration","volatility"):
            self.fields[key].configure(text="—")
        self.fields["digit"].configure(text="NO QUALIFIED SIGNAL")
        status_text="NO TRADE" if status=="NO TRADE" else f"NO TRADE · {status}"
        self.fields["status"].configure(text=status_text,foreground="#d78e88")

    def toggle_topmost(self):
        self.topmost=not self.topmost
        self.root.attributes("-topmost",self.topmost)
        self.top_button.configure(text="Always on top" if self.topmost else "Normal window")

    def toggle_compact(self):
        self.compact=not self.compact
        if self.compact:
            self.root.geometry("350x320")
            self.compact_button.configure(text="Expand")
        else:
            self.root.geometry("400x350")
            self.compact_button.configure(text="Compact")

    def open_dashboard(self):
        webbrowser.open(f"{self.base_url}/dashboard")


def main():
    parser=argparse.ArgumentParser(description="BROMAX floating research panel")
    parser.add_argument("--url",default=os.getenv("BROMAX_API_URL","http://127.0.0.1:8765"))
    parser.add_argument("--symbol",default=os.getenv("DERIV_SYMBOLS","1HZ100V").split(",")[0].strip())
    args=parser.parse_args()
    root=tk.Tk()
    AnalysisPanel(root,args.url,args.symbol)
    root.mainloop()


if __name__=="__main__":
    main()
