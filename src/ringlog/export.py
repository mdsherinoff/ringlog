"""Write calls to CSV, Excel and a self-contained HTML report."""

from __future__ import annotations

import csv
import html
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .calls import INCOMING, MISSED, OUTGOING, Call

HEADERS = ["Date", "Time", "Contact", "Number", "Direction", "Duration", "Seconds", "Service", "Country"]


def _row(c: Call) -> list:
    return [
        c.when.strftime("%Y-%m-%d"),
        c.when.strftime("%H:%M:%S"),
        c.name,
        c.number,
        c.direction,
        c.duration_text,
        c.duration,
        c.service,
        c.country,
    ]


def _guard(value):
    # Stop spreadsheet apps treating a value like "+44..." or "=..." as a formula.
    if isinstance(value, str) and value[:1] in ("=", "@", "-"):
        return "'" + value
    return value


def to_csv(calls: list[Call], path: Path) -> None:
    # utf-8-sig so Excel opens names with accents correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        for c in calls:
            w.writerow([_guard(v) for v in _row(c)])


def to_xlsx(calls: list[Call], path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Calls"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for c in calls:
        ws.append(
            [
                c.when.replace(tzinfo=None).date(),
                c.when.replace(tzinfo=None).time(),
                c.name,
                c.number,
                c.direction,
                c.duration_text,
                c.duration,
                c.service,
                c.country,
            ]
        )
    for row in ws.iter_rows(min_row=2):
        row[0].number_format = "yyyy-mm-dd"
        row[1].number_format = "hh:mm:ss"
        row[3].number_format = "@"
    for i, width in enumerate([12, 10, 26, 18, 11, 10, 9, 16, 8], start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    s = summarize(calls)
    sheet = wb.create_sheet("Summary")
    sheet.append(["Metric", "Value"])
    for label, value in s["totals"]:
        sheet.append([label, value])
    sheet.append([])
    sheet.append(["Top contacts", "Calls", "Talk time"])
    for name, count, secs in s["top_contacts"]:
        sheet.append([name, count, _hms(secs)])
    for cell in (sheet["A1"], sheet["B1"]):
        cell.font = Font(bold=True)
    sheet.column_dimensions["A"].width = 30
    sheet.column_dimensions["B"].width = 14
    sheet.column_dimensions["C"].width = 12
    wb.save(path)


def _hms(secs: int) -> str:
    h, rest = divmod(int(secs), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}"


def summarize(calls: list[Call]) -> dict:
    by_dir = Counter(c.direction for c in calls)
    talk = sum(c.duration for c in calls)
    answered = [c for c in calls if c.direction != MISSED]
    per_contact: dict[str, list] = defaultdict(lambda: [0, 0])
    for c in calls:
        per_contact[c.contact][0] += 1
        per_contact[c.contact][1] += c.duration
    top = sorted(per_contact.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[:10]
    per_day = Counter(c.when.date() for c in calls)
    missed_by_hour = Counter(c.when.hour for c in calls if c.direction == MISSED)
    by_weekday = Counter(c.when.strftime("%a") for c in calls)
    first = min((c.when for c in calls), default=None)
    last = max((c.when for c in calls), default=None)
    return {
        "range": (first, last),
        "totals": [
            ("Total calls", len(calls)),
            ("Incoming", by_dir[INCOMING]),
            ("Outgoing", by_dir[OUTGOING]),
            ("Missed", by_dir[MISSED]),
            ("Total talk time", _hms(talk)),
            ("Average call (answered)", _hms(talk / len(answered)) if answered else "0:00:00"),
            ("Different contacts", len(per_contact)),
        ],
        "top_contacts": [(name, n, secs) for name, (n, secs) in top],
        "longest": sorted(calls, key=lambda c: -c.duration)[:10],
        "per_day": sorted(per_day.items()),
        "missed_by_hour": [missed_by_hour.get(h, 0) for h in range(24)],
        "by_weekday": [(d, by_weekday.get(d, 0)) for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")],
    }


def _bars(values: list[tuple[str, int]], height=140, label_every=1) -> str:
    """A small inline SVG bar chart. values: [(label, count)]."""
    if not values:
        return '<p class="muted">No data.</p>'
    peak = max(v for _, v in values) or 1
    n = len(values)
    w = max(4, min(28, 680 // n))
    gap = 2 if w > 6 else 1
    width = n * w
    parts = [f'<svg viewBox="0 0 {width} {height + 18}" class="chart" role="img" preserveAspectRatio="none">']
    for i, (label, v) in enumerate(values):
        h = round(v / peak * height)
        x = i * w
        parts.append(
            f'<rect x="{x + gap / 2}" y="{height - h}" width="{w - gap}" height="{h}" rx="1">'
            f"<title>{html.escape(str(label))}: {v}</title></rect>"
        )
        if i % label_every == 0:
            parts.append(
                f'<text x="{x + w / 2}" y="{height + 13}" text-anchor="middle">{html.escape(str(label))}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1f;--muted:#6e6e73;--line:#e3e3e0;--accent:#2f6fde}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e21;--ink:#f2f2f2;--muted:#9a9aa0;--line:#333338;--accent:#6a9cff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:32px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:17px;margin:0 0 12px}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:24px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
.stat b{display:block;font-size:22px}.stat span{color:var(--muted);font-size:13px}
.chart{width:100%;height:160px}.chart rect{fill:var(--accent)}.chart text{fill:var(--muted);font-size:9px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500}td.n{text-align:right;font-variant-numeric:tabular-nums}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}.wrap{overflow-x:auto}
"""


def to_html(calls: list[Call], path: Path, title: str = "Call history report") -> None:
    s = summarize(calls)
    esc = html.escape
    first, last = s["range"]
    span = f"{first:%d %b %Y} – {last:%d %b %Y}" if first else "No calls"
    stats = "".join(f'<div class="card stat"><b>{esc(str(v))}</b><span>{esc(k)}</span></div>' for k, v in s["totals"])

    per_day = s["per_day"]
    day_values = [(d.strftime("%d %b"), n) for d, n in per_day]
    day_chart = _bars(day_values, label_every=max(1, len(day_values) // 12))
    hour_chart = _bars([(f"{h:02d}", n) for h, n in enumerate(s["missed_by_hour"])], label_every=3)
    week_chart = _bars(s["by_weekday"])

    top_rows = "".join(
        f'<tr><td>{esc(name)}</td><td class="n">{n}</td><td class="n">{_hms(secs)}</td></tr>'
        for name, n, secs in s["top_contacts"]
    )
    long_rows = "".join(
        f'<tr><td>{c.when:%Y-%m-%d %H:%M}</td><td>{esc(c.contact)}</td><td>{c.direction}</td>'
        f'<td class="n">{c.duration_text}</td></tr>'
        for c in s["longest"]
    )
    all_rows = "".join(
        f'<tr><td>{c.when:%Y-%m-%d %H:%M}</td><td>{esc(c.name)}</td><td>{esc(c.number)}</td>'
        f'<td>{c.direction}</td><td class="n">{c.duration_text}</td><td>{esc(c.service)}</td></tr>'
        for c in calls
    )
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><style>{_CSS}</style></head>
<body><main>
<h1>{esc(title)}</h1><p class="muted">{span} · generated {datetime.now():%d %b %Y %H:%M} by Ringlog</p>
<div class="grid">{stats}</div>
<div class="card"><h2>Calls per day</h2>{day_chart}</div>
<div class="two">
<div class="card"><h2>Missed calls by hour</h2>{hour_chart}</div>
<div class="card"><h2>Calls by weekday</h2>{week_chart}</div>
</div>
<div class="two">
<div class="card"><h2>Most frequent contacts</h2><table><tr><th>Contact</th><th>Calls</th><th>Talk time</th></tr>{top_rows}</table></div>
<div class="card"><h2>Longest calls</h2><table><tr><th>When</th><th>Contact</th><th>Type</th><th>Length</th></tr>{long_rows}</table></div>
</div>
<div class="card wrap"><h2>All calls ({len(calls)})</h2><table>
<tr><th>When</th><th>Name</th><th>Number</th><th>Type</th><th>Length</th><th>Service</th></tr>{all_rows}</table></div>
</main></body></html>"""
    Path(path).write_text(doc, encoding="utf-8")


WRITERS = {"csv": to_csv, "xlsx": to_xlsx, "html": to_html}
