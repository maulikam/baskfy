"""The answer key for kite.rs's `coercions_match_the_live_python` unit test: what the live
Python's `_decimal`, `_int`, `_text`, `_date` and `_aware_ist` return, or which class they raise,
for each input. Regenerate from decile-blueprint with
`PYTHONPATH=..:packages/core/src:packages/providers/src .venv/bin/python -I <this file>
> kite_coercions.json`.
"""
import json, sys
from decimal import Decimal
from baskfy_providers import kite as k

def enc(v):
    if v is None: return {"t":"none"}
    if isinstance(v,bool): return {"t":"bool","v":v}
    if isinstance(v,int): return {"t":"int","v":v}
    if isinstance(v,float): return {"t":"float","v":repr(v)}
    if isinstance(v,str): return {"t":"str","v":v}
    raise TypeError(v)

def res(fn, v):
    try:
        out = fn(v)
    except Exception as e:
        return {"err": type(e).__name__}
    if out is None: return {"ok": None}
    if isinstance(out, Decimal):
        exp = out.as_tuple().exponent
        if isinstance(exp, str): return {"ok": str(out), "special": True}
        return {"ok": format(out, "f"), "scale": max(0, -exp)}
    if hasattr(out, "isoformat"): return {"ok": out.isoformat()}
    return {"ok": out}

strs = ["412.35","25000.0","1e3","1E-5"," 12 ","1_000","1__0","_1","1_","+5","-0","-0.0",".5","5.","abc",""," ","nan","NaN","inf","-Infinity","0x10","1,000","\t7\n","1e","e5","++1","+-1","1.2.3","0.000000000000000000000000001","1e-28","1e+2","12345678901234567890","1.0e2","-1.5E+3","2.5","3.7","-3.7","0.0001","1e+0","00012.50","1.","-.5"]
ints = [0,1,-5,2**53,2**63-1,-(2**63)]
floats = [0.0,-0.0,412.35,25000.0,1e16,1e-05,0.1,1e22,123456.789,2.5,-3.7,1e15,1234567.0,0.0001,5e-324]
vals = [None,True,False] + ints + floats + strs
cases=[]
for v in vals:
    cases.append({"in":enc(v),"decimal":res(k._decimal,v),"int":res(k._int,v),"text":res(k._text,v)})
dates = ["2026-10-27","2026-10-27T09:15:00","2026-10-27 09:15:00","2026-10-27 09:15:00+05:30","2026-10-27T09:15:00Z","2026-10-27T09:15:00+0530","2026-10-27T09:15","2026-10-27T09","20261027","2026-W44-1","27-10-2026","2026-10-27T09:15:00.123456","2026-10-27T09:15:00.123","2026-10-27T24:00:00","2026-10-27T09:15:00+05:30:00","2026-10-27T09:15:00 +05:30"," 2026-10-27","2026-02-30","",
 '2026-10-27T09:15:00  +05:30','2026-10-27T09:15:00 ','2026-10-27T09:15 ','2026-10-27T09:15:00.1 +05:30','2026-10-27T09:15:00x+05:30','2026-10-27T09:15:00+05:30 ','2026-10-27 09:15:00.1234567','2026-10-27T0915','2026-10-27T091500,5','2026-10-27T09:15:00-00:00','2026-10-27T09:15:00+00','2026-10-27T09:15:00+0530Z','2026-10-27T09Z','2026-10-27T24:00','2026-10-27x09','2026-10-27T09:15:00+24:00','2026-10-27T09:15:00+23:59','2026-10-27T09:15:60','2026-10-27T09:15:00.','2026-10-27T09:15:00Zx','202610','2026W441','2026-W44','2026W44','2026-W53-1','2025-W53-1','2026-10-2','2026-1-27','abcdefgh','2026-10-27T','2026-10-27T9','2026-10-27T09:1','2026-10-27T09:15:0','2026-10-27T09:15:00.1+5','2026-13-01','0000-01-01','2026-W54-1','2026-W44-8','2026-W00-1','2026-10-27T09:15:00+05:75','2026-10-27T09:15:00+99','2026-10-27é09:15','2026-10-27T09:15:00.1234567x','2026-10-27T09:15:00:12','2026-10-27T091500123','2026-10-27T09:15:00-05:30','2026-10-27T09 +05:30','2026-10-27T0915+0530','2024-02-29T23:59:59.999999','2026-10-27T09:15:00.000001Z','20261027T091500','2026-10-27T09:15:00+05','2026-10-27T09:15:00+053000','2026-W44-1T09:15','2026W44109:15']
dcases=[]
for s in dates:
    dcases.append({"in":s,"date":res(k._date,s),"aware":res(k._aware_ist,s)})
json.dump({"scalars":cases,"dates":dcases}, sys.stdout, indent=1, ensure_ascii=False)
