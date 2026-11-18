import argparse, json
from .config import Settings
from .storage import Store
from .reporting import write_report

def main():
    p=argparse.ArgumentParser(prog="bromax"); sub=p.add_subparsers(dest="cmd",required=True)
    r=sub.add_parser("report"); r.add_argument("--output",default="reports/bromax_report.json")
    args=p.parse_args(); s=Settings.from_env(); store=Store(s.database_path)
    if args.cmd=="report":
        report=write_report(store,s.symbols,args.output); print(json.dumps(report,indent=2,default=str)); print(f"Report written: {args.output}")
if __name__=="__main__": main()
