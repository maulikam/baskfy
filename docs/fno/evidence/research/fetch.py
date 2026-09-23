"""Research fetch: NSE F&O bhavcopy via NSEProvider (1 req/s, archive-then-parse)."""
import datetime as dt, sys, time, threading
from pathlib import Path
import polars as pl
from baskfy_providers.archive import LocalRawArchive
from baskfy_providers.nse import NSEProvider, NSERuntime, build_http_client
from baskfy_providers.settings import ProviderSettings

ROOT = Path(__file__).parent
class Spacer:
    def __init__(self, rate): self.gap=1.0/rate; self.last=0.0; self.lock=threading.Lock()
    def acquire(self, tokens=1.0):
        with self.lock:
            wait=max(0.0,self.last+self.gap-time.monotonic())
            if wait: time.sleep(wait)
            self.last=time.monotonic(); return wait

settings = ProviderSettings()
prov = NSEProvider(settings, LocalRawArchive(ROOT/"archive"),
                   NSERuntime(client=build_http_client(settings), rate_limiter=Spacer(settings.nse_rate_limit_per_second)))
out = ROOT/"days"; out.mkdir(exist_ok=True)
start, end = dt.date.fromisoformat(sys.argv[1]), dt.date.fromisoformat(sys.argv[2])
d = start; ok = miss = 0
while d <= end:
    if d.weekday() < 5 and not (out/f"{d}.parquet").exists() and not (out/f"{d}.missing").exists():
        try:
            f = prov.fo_bhavcopy(d)
            f.with_columns(pl.col(c).cast(pl.Float64) for c in ["strike","open","high","low","close","settle","underlying","turnover"]).write_parquet(out/f"{d}.parquet")
            ok += 1
        except Exception as e:
            (out/f"{d}.missing").write_text(f"{type(e).__name__}: {e}"[:500]); miss += 1
            print(d, type(e).__name__, str(e)[:120], flush=True)
    d += dt.timedelta(days=1)
print("ok", ok, "missing", miss)
