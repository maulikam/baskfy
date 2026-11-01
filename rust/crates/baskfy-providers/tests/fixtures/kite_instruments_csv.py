"""The answer key for kite.rs's `instrument_dumps_parse_like_the_sdk` unit test: what kiteconnect
5.2.1's `KiteConnect._parse_instruments` returns (as `repr`) or raises (its class) for each body.
No network: the method is a pure function of the bytes. Regenerate from decile-blueprint with
`.venv/bin/python -I <this file> > kite_instruments_csv.json`.
"""
import json
import sys

from kiteconnect import KiteConnect

H = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange"
R1 = "408065,1594,INFY,INFOSYS,0,,0,0.05,1,EQ,NSE,NSE"
R2 = "12345,48,NIFTY26OCT25000CE,NIFTY,101.5,2026-10-27,25000,0.05,65,CE,NFO-OPT,NFO"
bodies = {
    "plain": f"{H}\n{R1}\n{R2}\n",
    "bom": f"﻿{H}\n{R1}\n",
    "crlf": f"{H}\r\n{R1}\r\n{R2}\r\n",
    "cr_only": f"{H}\r{R1}\r",
    "blank_lines": f"{H}\n\n{R1}\n\n\n{R2}\n",
    "header_only": f"{H}\n",
    "empty": "",
    "whitespace": "  \n \n",
    "leading_blank": f"\n\n{H}\n{R1}\n",
    "quoted_comma": f'{H}\n408065,1594,INFY,"INFOSYS, LTD",0,,0,0.05,1,EQ,NSE,NSE\n',
    "quoted_quote": f'{H}\n408065,1594,INFY,"INFO ""SYS""",0,,0,0.05,1,EQ,NSE,NSE\n',
    "short_row": f"{H}\n408065,1594,INFY\n",
    "extra_field": f"{H}\n{R1},EXTRA\n",
    "no_expiry_column": "instrument_token,last_price,strike,tick_size,lot_size\n1,0,0,0.05,1\n",
    "bad_int": f"{H}\nabc,1594,INFY,INFOSYS,0,,0,0.05,1,EQ,NSE,NSE\n",
    "float_token": f"{H}\n1.0,1594,INFY,INFOSYS,0,,0,0.05,1,EQ,NSE,NSE\n",
    "spaced_numbers": f"{H}\n 7 ,1594,INFY,INFOSYS, 1.5 ,,0,0.05, 2 ,EQ,NSE,NSE\n",
    "nan_price": f"{H}\n7,1594,INFY,INFOSYS,nan,,inf,0.05,2,EQ,NSE,NSE\n",
    "expiry_9": f"{H}\n7,1,X,X,0,2026-1-27,0,0.05,1,CE,NFO-OPT,NFO\n",
    "expiry_basic": f"{H}\n7,1,X,X,0,2026102700,0,0.05,1,CE,NFO-OPT,NFO\n",
    "duplicate_header": "instrument_token,last_price,strike,tick_size,lot_size,expiry,last_price\n1,2,0,0.05,1,,3\n",
    "space_row": f"{H}\n   \n{R1}\n",
    "trailing_spaces": f"  {H}\n{R1}  \n",
    "invalid_utf8": b"\xff\xfe",
}
out = {}
k = KiteConnect(api_key="x")
for name, body in bodies.items():
    raw = body if isinstance(body, bytes) else body.encode("utf-8")
    try:
        out[name] = {"body": raw.decode("latin-1"), "ok": repr(k._parse_instruments(raw))}
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        out[name] = {"body": raw.decode("latin-1"), "err": type(exc).__name__}
json.dump(out, sys.stdout, indent=1, ensure_ascii=True)
