"""Answer keys for the desk foundation's engines (DECISIONS-RS R8.4), from the live Python.

Writes three fixtures under ``rust/crates/baskfy-desk/fixtures/``:

* ``pyfmt_probes.json`` — Python's ``fmt % args`` and ``format(value, spec)`` /
  ``template.format(*args)`` over every spec the desk's templates use and the edges around them
  (negative zero, half-even ties, large and tiny magnitudes, ints through float conversions).
* ``wire_probes.json`` — ``orjson.dumps`` of floats across both notation thresholds, and of a
  nested dict with dates, ``None``, unicode and control characters.
* ``template_probes.json`` — Jinja2 output for snippets rendered through the desk's own
  ``Jinja2Templates`` environment (autoescape, ``asset_v``, ``fromjson``), one per feature the
  templates lean on: printing every Python type, the ``format`` filter, ``str.format``,
  ``tojson``, escaping, loops, macros, ``selectattr``/``map``/``join``/``sort``/``sum``/``round``.

Run with the desk venv, the worktree on ``PYTHONPATH``, from the desk directory (so
``Jinja2Templates(directory="app/templates")`` resolves); no network, no database::

    cd kite-momentum-rebalancer && <venv>/bin/python ../tools/parity/desk_probes.py \\
        --out ../rust/crates/baskfy-desk/fixtures
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

FLOATS = [0.0, -0.0, 1.0, -1.0, 0.5, 1.5, 2.5, 0.125, 0.375, 0.045, 1.005, 2.675, 12.345,
          -12.345, 1234.5, 1234567.891, -98765.4321, 1e-05, 1.5e-05, 1e-06, 2.5e-07, 1e15,
          9.99e15, 1e16, 1.5e16, 1e22, 123456789.123, 0.1, 0.2 + 0.1, 99.995, 999999.5,
          5e-324, 1.7976931348623157e308, 100.0, 33.333333333, 0.000123]
INTS = [0, 1, -1, 7, 42, 999, 1000, -1000, 1234567, -9876543210, 10**15]

PERCENT_SPECS = ["%.1f", "%.2f", "%.0f", "%+.1f", "%+.2f", "%.3f", "%.4f", "%+d", "%d", "%s",
                 "%5.1f", "%-8.2f|", "%08.2f", "%e", "%.3e", "%g", "%.3g", "%10s|", "%%",
                 "rgba(225,45,51,%.2f)", "%.1f%%", "%i", "%r", "% d", "%#.0f", "%x", "%o"]
FORMAT_SPECS = ["", ",", ",.0f", ",.1f", ",.2f", ".2f", ".0f", "+.1f", ".1%", ".0%", "d",
                ",d", "10.2f", "<10", ">10,", "^9.1f", "08.2f", "e", ".3e", "g", ".3g", ".2",
                "_", "+,"]


def _arg(v: Any) -> Any:  # noqa: ANN401
    if isinstance(v, float) and not math.isfinite(v):
        return {"float": repr(v)}
    return v


def _attempt(fn: Any) -> dict[str, Any]:  # noqa: ANN401
    try:
        return {"ok": fn()}
    except Exception as exc:  # noqa: BLE001 - the refusal is the answer
        return {"err": type(exc).__name__}


def pyfmt_cases() -> dict[str, Any]:
    percent = []
    for spec in PERCENT_SPECS:
        n = spec.count("%") - 2 * spec.count("%%")
        values: list[Any] = [*FLOATS, *INTS, True, None, "abc", "x'y"] if n == 1 else [None]
        for v in values:
            args: tuple[Any, ...] = (v,) if n == 1 else ()
            percent.append({"fmt": spec, "args": [_arg(a) for a in args],
                            **_attempt(lambda s=spec, a=args: s % a)})
    fmt = []
    for spec in FORMAT_SPECS:
        for v in [*FLOATS, *INTS, True, "abc"]:
            fmt.append({"spec": spec, "value": _arg(v), **_attempt(lambda s=spec, x=v: format(x, s))})
    templates = []
    for tpl, args in [("{:,.0f}", [1234567.891]), ("₹{:,.0f}", [-98765.4321]), ("{:,}", [1234567]),
                      ("{:,}", [1234.5]), ("{0} and {1:.2f}", ["a", 2.5]), ("{{literal}} {}", [3]),
                      ("{} {}", [1]), ("{:.2f}", ["x"])]:
        templates.append({"template": tpl, "args": args,
                          **_attempt(lambda t=tpl, a=args: t.format(*a))})
    return {"percent": percent, "format": fmt, "str_format": templates}


def wire_cases() -> dict[str, Any]:
    import datetime as dt

    import orjson

    floats = [{"value": _arg(v), "orjson": orjson.dumps(v).decode()} for v in
              [*FLOATS, float("nan"), float("inf"), 1e100, -1e-100, 3.0e-5, 4.2e-6]]
    sample = {"plan_id": "abc123", "n": 3, "x": 1e-05, "big": 1e16, "neg": -0.0, "none": None,
              "flag": True, "text": 'é<>&"\\\n\t\x01\x7f', "list": [1, 2.5, "s"],
              "nested": {"b": 1, "a": [None, False]},
              "d": dt.date(2026, 10, 30),
              "t": dt.datetime(2026, 10, 30, 9, 15, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))),
              "tu": dt.datetime(2026, 10, 30, 9, 15, 0, 123456)}
    return {"floats": floats, "sample_json": json.dumps(sample, default=str),
            "sample_orjson": orjson.dumps(sample).decode()}


TEMPLATE_PROBES = [
    ("print_types", "{{ i }}|{{ f }}|{{ g }}|{{ t }}|{{ n }}|{{ s }}|{{ l }}|{{ d }}|{{ u }}|", {}),
    ("format_filter", "{{ '%.1f'|format(f) }} {{ '%+.2f'|format(g) }} {{ '%.0f'|format(i) }} "
                      "{{ '%+d'|format(i) }} {{ 'rgba(0,135,79,%.2f)'|format(0.456) }}", {}),
    ("str_format", "{{ '{:,.0f}'.format(big) }} {{ '{:,}'.format(i) }} {{ '{:,.2f}'.format(g) }} "
                   "{{ '₹{:,.0f}'.format(big) }}", {}),
    ("escape", "{{ html }}|{{ html|e }}|{{ html|safe }}|{{ q }}", {}),
    ("tojson", "<script>var x = {{ d|tojson }}; var y = {{ html|tojson }};</script>", {}),
    ("loop", "{% for k, v in d.items() %}{{ loop.index }}:{{ k }}={{ v }}{% if not loop.last %},"
             "{% endif %}{% endfor %}", {}),
    ("filters", "{{ rows|selectattr('on')|map(attribute='name')|join(', ') }}|{{ l|length }}|"
                "{{ nums|sum }}|{{ nums|max }}|{{ nums|min }}|{{ s|upper }}|{{ s|title }}|"
                "{{ f|round(1) }}|{{ -3|abs }}|{{ '7'|int }}|{{ nums|sort|first }}|"
                "{{ s|replace('l', 'L') }}|{{ '  x  '|trim }}", {}),
    ("macro", "{% macro pill(x, cls='a') %}<b class=\"{{ cls }}\">{{ x }}</b>{% endmacro %}"
              "{{ pill(1) }}{{ pill('<', cls='z') }}", {}),
    ("set_and_math", "{% set total = i * 2 + f %}{{ total }} {{ i / 4 }} {{ i // 4 }} {{ i % 4 }}"
                     " {{ f > 1 and 'yes' or 'no' }}", {}),
    ("default_and_none", "{{ missing|default('dflt') }}|{{ n is none }}|{{ n or 'empty' }}|"
                         "{% if missing is defined %}D{% else %}U{% endif %}", {}),
    ("globals", "{{ asset_v|length }} {{ options_enabled }}", {}),
    ("fromjson", "{% set a = j|fromjson %}{{ a.k }} {{ a.v[1] }}", {}),
    ("whitespace", "a\n  {% if t %}\n  b\n  {% endif %}\nc\n", {}),
    ("dict_get", "{{ d.get('b') }} {{ d.get('zz', 'none-here') }} {{ d['a'] }} "
                 "{{ s.startswith('he') }}", {}),
]

CONTEXT = {"i": 42, "f": 2.5, "g": -1234.567, "t": True, "n": None, "s": "hello world",
           "l": [1, "a", None], "d": {"b": 2, "a": "x<y"}, "u": 1e-05, "big": 1234567.891,
           "html": "<a href=\"x\">O'Neil & co</a>", "q": "it's \"quoted\"",
           "rows": [{"name": "A", "on": True}, {"name": "B", "on": False}, {"name": "C", "on": 1}],
           "nums": [3, 1.5, 2], "j": "{\"k\": \"v\", \"v\": [1, 2]}"}


def template_cases() -> dict[str, Any]:
    import sys

    import dotenv

    setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    sys.argv = ["probe"]
    from app.main import templates  # the desk's own environment, globals and filters

    env = templates.env
    out = []
    for name, src, extra in TEMPLATE_PROBES:
        ctx = {**CONTEXT, **extra}
        out.append({"name": name, "source": src,
                    **_attempt(lambda s=src, c=ctx: env.from_string(s).render(**c))})
    return {"context": CONTEXT, "asset_v_len": len(env.globals["asset_v"]),
            "options_enabled": env.globals["options_enabled"], "probes": out}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    for name, data in [("pyfmt_probes.json", pyfmt_cases()), ("wire_probes.json", wire_cases()),
                       ("template_probes.json", template_cases())]:
        (out / name).write_text(json.dumps(data, indent=1, ensure_ascii=False, allow_nan=False) + "\n")
        print(f"wrote {out / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
