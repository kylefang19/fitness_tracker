import os
import csv
import io
import json
import urllib.parse
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")

TABLE_NAME = os.environ["TABLE_NAME"]
USER_ID = os.environ.get("USER_ID", "kyle")
START_DATE = os.environ.get("START_DATE", "2026-01-01")
SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "")

# Year goals
GOALS = {
    "plank_seconds": 1500 * 60,  # 1500 minutes
    "pullups": 2000,
    "dips": 5000,
    "pushups": 15000,
}

LA_TZ = ZoneInfo("America/Los_Angeles")

table = dynamodb.Table(TABLE_NAME)


def _resp(status, body, content_type="text/html"):
    return {
        "statusCode": status,
        "headers": {
            "content-type": f"{content_type}; charset=utf-8",
            "cache-control": "no-store",
        },
        "body": body,
    }


def _json(status, obj):
    return _resp(status, json.dumps(obj), content_type="application/json")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _la_today_str() -> str:
    return datetime.now(LA_TZ).date().isoformat()


def _la_today_date() -> date:
    return datetime.now(LA_TZ).date()


def _parse_start_date():
    try:
        return _parse_date(START_DATE)
    except Exception:
        return _la_today_date()


def _require_token(event) -> bool:
    if not SECRET_TOKEN:
        return True
    qs = event.get("queryStringParameters") or {}
    return qs.get("token") == SECRET_TOKEN


def _week_start(d: date):
    # Monday
    return d - timedelta(days=d.weekday())


def _days_in_month(d: date) -> int:
    first = d.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    return (next_first - first).days


def _query_range(user_id: str, start: str, end: str):
    resp = table.query(
        KeyConditionExpression=Key("user_id").eq(user_id) & Key("date").between(start, end)
    )
    return resp.get("Items", [])


def _get_item(user_id: str, d: str):
    resp = table.get_item(Key={"user_id": user_id, "date": d})
    return resp.get("Item")


def _delete_item(user_id: str, d: str):
    table.delete_item(Key={"user_id": user_id, "date": d})


def _upsert_item(user_id: str, d: str, pushups: int, pullups: int, dips: int, plank_seconds: int):
    table.put_item(
        Item={
            "user_id": user_id,
            "date": d,
            "pushups": int(pushups),
            "pullups": int(pullups),
            "dips": int(dips),
            "plank_seconds": int(plank_seconds),
        }
    )


def _sum_items(items):
    totals = {"plank_seconds": 0, "pullups": 0, "dips": 0, "pushups": 0}
    for it in items:
        for k in totals:
            try:
                totals[k] += int(it.get(k, 0))
            except Exception:
                pass
    return totals


def _pace_metrics(all_totals, start_d: date, today_d: date):
    # "Expected by now" pacing over the year
    if today_d < start_d:
        elapsed_days = 1
    else:
        elapsed_days = (today_d - start_d).days + 1

    expected = {k: (GOALS[k] * elapsed_days) / 365.0 for k in GOALS}
    on_track = {k: all_totals.get(k, 0) >= expected[k] for k in GOALS}
    remaining = {k: max(0, GOALS[k] - all_totals.get(k, 0)) for k in GOALS}
    return elapsed_days, expected, on_track, remaining


def _fmt(key, v):
    if key == "plank_seconds":
        return f"{float(v)/60:.1f} min"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(int(v))


def _pct(done, goal):
    try:
        if goal <= 0:
            return 0
        return max(0, min(100, int(round((done / goal) * 100))))
    except Exception:
        return 0


def _build_summary_html(
    week_totals,
    month_totals,
    all_totals,
    elapsed_days,
    expected,
    on_track,
    remaining,
    today_d: date,
):
    # Weekly quota (Mon–Sun): goal/365 * 7
    weekly_targets = {k: (GOALS[k] / 365.0) * 7.0 for k in GOALS}
    weekly_need = {k: max(0, weekly_targets[k] - week_totals.get(k, 0)) for k in GOALS}

    # Monthly quota = total/365 * days in current month
    dim = _days_in_month(today_d)
    monthly_targets = {k: (GOALS[k] / 365.0) * dim for k in GOALS}
    monthly_need = {k: max(0, monthly_targets[k] - month_totals.get(k, 0)) for k in GOALS}

    p_plank = _pct(all_totals.get("plank_seconds", 0), GOALS["plank_seconds"])
    p_pull = _pct(all_totals.get("pullups", 0), GOALS["pullups"])
    p_dips = _pct(all_totals.get("dips", 0), GOALS["dips"])
    p_push = _pct(all_totals.get("pushups", 0), GOALS["pushups"])

    def row(label, key, pct):
        status = "✅" if on_track[key] else "⚠️"
        return f"""
        <tr>
          <td class="rowhead">{label}</td>
          <td>
            {_fmt(key, all_totals.get(key, 0))} / {_fmt(key, GOALS[key])}
            <div class="bar"><div class="fill" style="width:{pct}%"></div></div>
            <div class="muted">{pct}%</div>
          </td>
          <td>{_fmt(key, week_totals.get(key, 0))}</td>
          <td><strong>{_fmt(key, weekly_need[key])}</strong></td>
          <td>{_fmt(key, month_totals.get(key, 0))}</td>
          <td><strong>{_fmt(key, monthly_need[key])}</strong></td>
          <td>{_fmt(key, expected[key])}</td>
          <td>{_fmt(key, remaining[key])}</td>
          <td>{status}</td>
        </tr>
        """

    return f"""
    <div class="muted">Elapsed days since start: {elapsed_days}</div>
    <div class="muted">Week is Monday–Sunday. Monthly quota uses days in current month ({dim} days): goal/365 × {dim}.</div>
    <table>
      <thead>
        <tr>
          <th>Metric</th>
          <th>Total</th>
          <th>This week</th>
          <th>Need this week</th>
          <th>This month</th>
          <th>Need this month</th>
          <th>Expected by now</th>
          <th>Remaining</th>
          <th>On track</th>
        </tr>
      </thead>
      <tbody>
        {row("Plank", "plank_seconds", p_plank)}
        {row("Pull-ups", "pullups", p_pull)}
        {row("Dips", "dips", p_dips)}
        {row("Pushups", "pushups", p_push)}
      </tbody>
    </table>
    """


def _build_history_html(items, token_param):
    def q_add(param: str):
        if token_param:
            return f"?token={urllib.parse.quote(token_param)}&{param}"
        return f"?{param}"

    rows = []
    for it in items:
        d = it.get("date", "")
        pushups = int(it.get("pushups", 0))
        pullups = int(it.get("pullups", 0))
        dips = int(it.get("dips", 0))
        plank_seconds = int(it.get("plank_seconds", 0))
        edit_link = q_add(f"edit={urllib.parse.quote(d)}")

        rows.append(f"""
        <tr>
          <td><a href="{edit_link}" style="color:var(--accent);text-decoration:none;">{d}</a></td>
          <td>{pushups}</td>
          <td>{pullups}</td>
          <td>{dips}</td>
          <td>{plank_seconds/60:.1f}</td>
        </tr>
        """)

    if not rows:
        return "<div class='muted' style='margin-top:10px;'>No data yet.</div>"

    return f"""
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Pushups</th>
          <th>Pull-ups</th>
          <th>Dips</th>
          <th>Plank (min)</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    <div class="muted" style="margin-top:10px;">Tip: tap a date to edit that day.</div>
    """


def _render_edit_form(token_param: str, edit_date: str, item):
    def q_add(param: str):
        if token_param:
            return f"?token={urllib.parse.quote(token_param)}&{param}"
        return f"?{param}"

    post_action = q_add(f"edit={urllib.parse.quote(edit_date)}")
    back_link = q_add("")

    pushups = str(int(item.get("pushups", 0)))
    pullups = str(int(item.get("pullups", 0)))
    dips = str(int(item.get("dips", 0)))
    plank_minutes = f"{int(item.get('plank_seconds', 0))/60:.1f}"

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Edit {edit_date}</title>
  <style>
    body {{ font-family: -apple-system, system-ui, Segoe UI, Roboto, Arial; margin: 16px; }}
    .card {{ max-width: 720px; margin: 0 auto; padding: 16px; border: 1px solid #ddd; border-radius: 12px; }}
    label {{ display:block; margin-top: 12px; }}
    input {{ width: 100%; padding: 10px; font-size: 16px; margin-top: 6px; }}
    button {{ width:100%; margin-top: 14px; padding: 12px; font-size:16px; }}
    a {{ display:inline-block; margin-top: 12px; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Edit {edit_date}</h2>
    <form method="POST" action="{post_action}">
      <label>Pushups</label><input name="pushups" inputmode="numeric" value="{pushups}"/>
      <label>Pull-ups</label><input name="pullups" inputmode="numeric" value="{pullups}"/>
      <label>Dips</label><input name="dips" inputmode="numeric" value="{dips}"/>
      <label>Plank (minutes)</label><input name="plank_minutes" inputmode="decimal" value="{plank_minutes}"/>
      <button type="submit">Save changes</button>
    </form>
    <a href="{back_link}">← Back</a>
  </div>
</body>
</html>
"""


def _render_page(token_param: str, selected_date: str, selected_vals, summary_html, history_html, message: str):
    if token_param:
        token_q = f"?token={urllib.parse.quote(token_param)}"
    else:
        token_q = ""

    def q_add(param: str):
        if token_q:
            return f"{token_q}&{param}"
        return f"?{param}"

    export_link = q_add("view=csv")
    api_data = q_add("api=data")
    api_upsert = q_add("api=upsert")
    api_delete = q_add("api=delete")
    api_get = q_add("api=get")

    pushups = selected_vals.get("pushups", "")
    pullups = selected_vals.get("pullups", "")
    dips = selected_vals.get("dips", "")
    plank_minutes = selected_vals.get("plank_minutes", "")

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Daily Fitness Tracker</title>
  <style>
    :root {{
      --muted: #9aa4b2;
      --text: #e8eef6;
      --line: rgba(255,255,255,0.10);
      --accent: #7dd3fc;
      --danger: #fb7185;
      --ok: #34d399;
      --bg1: #0b0c10;
      --bg2: #0f172a;
    }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, var(--bg1), var(--bg2));
      color: var(--text);
      font-family: -apple-system, system-ui, Segoe UI, Roboto, Arial;
    }}
    .wrap {{
      max-width: 980px;
      margin: 0 auto;
      padding: 18px 14px 40px;
    }}
    h1 {{
      margin: 6px 0 0;
      font-size: 26px;
      letter-spacing: 0.2px;
    }}
    .sub {{
      color: var(--muted);
      font-size: 13px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      font-size: 12px;
      color: var(--muted);
      background: rgba(255,255,255,0.02);
    }}
    .tabs {{
      display: flex;
      gap: 8px;
      margin-top: 14px;
      flex-wrap: wrap;
    }}
    .tab {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.02);
      color: var(--text);
      padding: 8px 10px;
      border-radius: 10px;
      cursor: pointer;
      font-size: 14px;
    }}
    .tab.active {{
      border-color: rgba(125,211,252,0.35);
      box-shadow: 0 0 0 2px rgba(125,211,252,0.12) inset;
    }}
    .card {{
      background: rgba(18,20,28,0.85);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.20);
      margin-top: 12px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }}
    @media (min-width: 920px) {{
      .grid {{
        grid-template-columns: 1.05fr 0.95fr;
        align-items: start;
      }}
    }}
    label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin: 8px 0 6px;
    }}
    input {{
      width: 100%;
      padding: 12px 12px;
      font-size: 16px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      outline: none;
    }}
    input:focus {{
      border-color: rgba(125,211,252,0.35);
      box-shadow: 0 0 0 3px rgba(125,211,252,0.12);
    }}
    button {{
      width: 100%;
      margin-top: 10px;
      padding: 12px 12px;
      font-size: 16px;
      border-radius: 12px;
      border: 1px solid rgba(125,211,252,0.35);
      background: rgba(125,211,252,0.12);
      color: var(--text);
      cursor: pointer;
    }}
    button:hover {{ background: rgba(125,211,252,0.18); }}
    .msg {{
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(125,211,252,0.25);
      background: rgba(125,211,252,0.08);
      color: var(--text);
      font-weight: 600;
      font-size: 14px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 10px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      text-align: left;
      font-size: 14px;
      vertical-align: middle;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .rowhead {{ font-weight: 700; }}
    .bar {{
      height: 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.02);
      overflow: hidden;
      width: 100%;
      margin-top: 6px;
    }}
    .fill {{
      height: 100%;
      background: rgba(125,211,252,0.75);
      width: 0%;
    }}
    .muted {{
      color: var(--muted);
      font-size: 12px;
    }}
    .actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }}
    .smallbtn {{
      width: auto;
      padding: 10px 12px;
      font-size: 14px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      text-decoration: none;
      display: inline-block;
      cursor: pointer;
    }}
    .smallbtn:hover {{ background: rgba(255,255,255,0.06); }}
    .hide {{ display: none; }}

    /* Editable dataset grid */
    .toolbar {{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      align-items:center;
      justify-content:space-between;
      margin-top:8px;
    }}
    .toolbar .left {{
      display:flex; gap:10px; flex-wrap:wrap; align-items:center;
    }}
    .mini {{
      padding: 10px 10px;
      font-size: 14px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      outline: none;
    }}
    .gridwrap {{
      overflow:auto;
      border-radius: 14px;
      border: 1px solid var(--line);
      margin-top: 10px;
    }}
    .datatable {{
      width: 100%;
      border-collapse: collapse;
      margin: 0;
      min-width: 720px;
    }}
    .datatable th, .datatable td {{
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      font-size: 14px;
    }}
    .cell {{
      width: 100%;
      padding: 8px 10px;
      border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.02);
      color: var(--text);
      font-size: 14px;
      outline: none;
    }}
    .cell:focus {{
      border-color: rgba(125,211,252,0.35);
      box-shadow: 0 0 0 3px rgba(125,211,252,0.12);
    }}
    .rowbtn {{
      padding: 8px 10px;
      font-size: 13px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      cursor: pointer;
    }}
    .rowbtn:hover {{ background: rgba(255,255,255,0.06); }}
    .danger {{
      border-color: rgba(251,113,133,0.35);
      background: rgba(251,113,133,0.08);
    }}
    .ok {{
      border-color: rgba(52,211,153,0.35);
      background: rgba(52,211,153,0.08);
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Daily Fitness Tracker</h1>
    <div class="sub">
      <span class="pill">User: {USER_ID}</span>
      <span class="pill">Start: {START_DATE}</span>
      <span class="pill">Today (LA): {_la_today_str()}</span>
    </div>

    <div class="tabs">
      <button class="tab active" data-tab="log">Log</button>
      <button class="tab" data-tab="summary">Summary</button>
      <button class="tab" data-tab="history">History</button>
      <button class="tab" data-tab="data">Data (Editable)</button>
    </div>

    {f"<div class='msg'>{message}</div>" if message else ""}

    <div id="tab-log" class="card">
      <div class="grid">
        <div>
          <h3 style="margin: 0 0 4px;">Log</h3>
          <div class="muted">Pick a date, edit the numbers, and save.</div>

          <form method="POST" action="{token_q}">
            <label>Date</label>
            <input id="log_date" name="log_date" type="date" value="{selected_date}" />

            <label>Pushups</label>
            <input id="pushups" name="pushups" inputmode="numeric" pattern="\\d*" value="{pushups}" />

            <label>Pull-ups</label>
            <input id="pullups" name="pullups" inputmode="numeric" pattern="\\d*" value="{pullups}" />

            <label>Dips</label>
            <input id="dips" name="dips" inputmode="numeric" pattern="\\d*" value="{dips}" />

            <label>Plank (minutes)</label>
            <input id="plank_minutes" name="plank_minutes" inputmode="decimal" value="{plank_minutes}" />

            <button type="submit">Save</button>
          </form>
        </div>

        <div>
          <h3 style="margin: 0 0 4px;">At-a-glance</h3>
          <div class="muted">Full details in Summary tab.</div>
          {summary_html}
          <div class="actions">
            <a class="smallbtn" href="{export_link}">Export CSV</a>
          </div>
        </div>
      </div>
    </div>

    <div id="tab-summary" class="card hide">
      <h3 style="margin:0 0 6px;">Summary</h3>
      {summary_html}
    </div>

    <div id="tab-history" class="card hide">
      <h3 style="margin:0 0 6px;">History (last 30 days)</h3>
      <div class="muted">Tap a date to edit that day.</div>
      {history_html}
    </div>

    <div id="tab-data" class="card hide">
      <h3 style="margin:0 0 6px;">Full dataset (editable)</h3>
      <div class="muted">Edit cells and save rows. This writes directly to DynamoDB.</div>

      <div class="toolbar">
        <div class="left">
          <input id="filter" class="mini" placeholder="Filter date (e.g., 2026-01)" />
          <button id="reload" class="smallbtn">Reload</button>
          <button id="saveAll" class="smallbtn ok">Save all modified rows</button>
        </div>
        <div class="left">
          <input id="newDate" class="mini" placeholder="New date YYYY-MM-DD" />
          <button id="addRow" class="smallbtn">Add row</button>
        </div>
      </div>

      <div id="status" class="muted" style="margin-top:10px;"></div>

      <div class="gridwrap">
        <table class="datatable" id="datatable">
          <thead>
            <tr>
              <th>Date</th>
              <th>Pushups</th>
              <th>Pull-ups</th>
              <th>Dips</th>
              <th>Plank (min)</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="dtbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    // Tabs
    const tabs = document.querySelectorAll(".tab");
    const panes = {{
      log: document.getElementById("tab-log"),
      summary: document.getElementById("tab-summary"),
      history: document.getElementById("tab-history"),
      data: document.getElementById("tab-data"),
    }};
    tabs.forEach(btn => {{
      btn.addEventListener("click", () => {{
        tabs.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        Object.values(panes).forEach(p => p.classList.add("hide"));
        panes[btn.dataset.tab].classList.remove("hide");
        if (btn.dataset.tab === "data") {{
          loadData();
        }}
      }});
    }});

    // --------- Log date -> auto load values ----------
    const API_GET = "{api_get}";
    const logDate = document.getElementById("log_date");

    async function loadLogForDate(d) {{
      if (!d) return;
      const url = API_GET + "&date=" + encodeURIComponent(d);
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      const row = data.row || null;
      document.getElementById("pushups").value = row ? row.pushups : "";
      document.getElementById("pullups").value = row ? row.pullups : "";
      document.getElementById("dips").value = row ? row.dips : "";
      document.getElementById("plank_minutes").value = row ? row.plank_minutes : "";
    }}

    logDate.addEventListener("change", (e) => {{
      loadLogForDate(e.target.value);
    }});

    // --------- Editable dataset ----------
    const API_DATA = "{api_data}";
    const API_UPSERT = "{api_upsert}";
    const API_DELETE = "{api_delete}";

    const statusEl = document.getElementById("status");
    const tbody = document.getElementById("dtbody");
    const filterEl = document.getElementById("filter");
    const reloadBtn = document.getElementById("reload");
    const saveAllBtn = document.getElementById("saveAll");
    const newDateEl = document.getElementById("newDate");
    const addRowBtn = document.getElementById("addRow");

    const dirty = new Set();
    let cache = [];

    function setStatus(msg) {{
      if (statusEl) statusEl.textContent = msg;
    }}

    function esc(s) {{
      return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    }}

    function render(rows) {{
      if (!tbody) return;
      tbody.innerHTML = "";
      const f = (filterEl?.value || "").trim();
      const filtered = f ? rows.filter(r => (r.date || "").includes(f)) : rows;

      for (const r of filtered) {{
        const key = r.date;
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><strong>${{esc(r.date)}}</strong></td>
          <td><input class="cell" data-key="${{esc(key)}}" data-field="pushups" value="${{esc(r.pushups ?? 0)}}" /></td>
          <td><input class="cell" data-key="${{esc(key)}}" data-field="pullups" value="${{esc(r.pullups ?? 0)}}" /></td>
          <td><input class="cell" data-key="${{esc(key)}}" data-field="dips" value="${{esc(r.dips ?? 0)}}" /></td>
          <td><input class="cell" data-key="${{esc(key)}}" data-field="plank_minutes" value="${{esc(r.plank_minutes ?? 0)}}" /></td>
          <td style="display:flex;gap:8px;flex-wrap:wrap;">
            <button class="rowbtn ok" data-action="save" data-key="${{esc(key)}}">Save</button>
            <button class="rowbtn danger" data-action="delete" data-key="${{esc(key)}}">Delete</button>
          </td>
        `;
        tbody.appendChild(tr);
      }}

      document.querySelectorAll(".cell").forEach(inp => {{
        inp.addEventListener("input", () => {{
          dirty.add(inp.dataset.key);
          setStatus(`Modified rows: ${{dirty.size}}`);
        }});
      }});

      document.querySelectorAll(".rowbtn").forEach(btn => {{
        btn.addEventListener("click", async () => {{
          const action = btn.dataset.action;
          const key = btn.dataset.key;
          if (action === "save") {{
            await saveRow(key);
          }} else if (action === "delete") {{
            const ok = confirm(`Delete ${{key}}?`);
            if (ok) await deleteRow(key);
          }}
        }});
      }});
    }}

    async function loadData() {{
      setStatus("Loading data...");
      const res = await fetch(API_DATA);
      const data = await res.json();
      cache = data.rows || [];
      dirty.clear();
      render(cache);
      setStatus(`Loaded ${{cache.length}} rows.`);
    }}

    function getRowFromInputs(dateStr) {{
      const inputs = document.querySelectorAll(`.cell[data-key="${{CSS.escape(dateStr)}}"]`);
      const obj = {{ date: dateStr, pushups: 0, pullups: 0, dips: 0, plank_minutes: 0 }};
      inputs.forEach(inp => {{
        obj[inp.dataset.field] = inp.value;
      }});
      obj.pushups = parseInt(obj.pushups || "0", 10) || 0;
      obj.pullups = parseInt(obj.pullups || "0", 10) || 0;
      obj.dips = parseInt(obj.dips || "0", 10) || 0;
      obj.plank_minutes = parseFloat(obj.plank_minutes || "0") || 0;
      return obj;
    }}

    async function saveRow(dateStr) {{
      const r = getRowFromInputs(dateStr);
      setStatus(`Saving ${{dateStr}}...`);
      const res = await fetch(API_UPSERT, {{
        method: "POST",
        headers: {{ "content-type": "application/json" }},
        body: JSON.stringify(r),
      }});
      if (!res.ok) {{
        setStatus(`Save failed for ${{dateStr}}`);
        return;
      }}
      dirty.delete(dateStr);
      setStatus(`Saved ${{dateStr}}. Modified rows: ${{dirty.size}}`);
      const idx = cache.findIndex(x => x.date === dateStr);
      if (idx >= 0) cache[idx] = r;
    }}

    async function deleteRow(dateStr) {{
      setStatus(`Deleting ${{dateStr}}...`);
      const res = await fetch(API_DELETE, {{
        method: "POST",
        headers: {{ "content-type": "application/json" }},
        body: JSON.stringify({{ date: dateStr }}),
      }});
      if (!res.ok) {{
        setStatus(`Delete failed for ${{dateStr}}`);
        return;
      }}
      dirty.delete(dateStr);
      cache = cache.filter(x => x.date !== dateStr);
      render(cache);
      setStatus(`Deleted ${{dateStr}}. Rows now: ${{cache.length}}`);
    }}

    if (reloadBtn) reloadBtn.addEventListener("click", loadData);
    if (filterEl) filterEl.addEventListener("input", () => render(cache));

    if (saveAllBtn) saveAllBtn.addEventListener("click", async () => {{
      if (dirty.size === 0) {{
        setStatus("No modified rows to save.");
        return;
      }}
      const keys = Array.from(dirty);
      setStatus(`Saving ${{keys.length}} rows...`);
      for (const k of keys) {{
        await saveRow(k);
      }}
      setStatus("Saved all modified rows.");
    }});

    if (addRowBtn) addRowBtn.addEventListener("click", () => {{
      const d = (newDateEl.value || "").trim();
      if (!/^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(d)) {{
        alert("Enter date as YYYY-MM-DD");
        return;
      }}
      if (cache.some(x => x.date === d)) {{
        alert("That date already exists.");
        return;
      }}
      cache = [{{ date: d, pushups: 0, pullups: 0, dips: 0, plank_minutes: 0 }}].concat(cache);
      render(cache);
      dirty.add(d);
      setStatus(`Added ${{d}} (not saved yet). Modified rows: ${{dirty.size}}`);
    }});
  </script>
</body>
</html>
"""


def handler(event, context):
    if not _require_token(event):
        return _resp(403, "Forbidden (bad token)", content_type="text/plain")

    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    qs = event.get("queryStringParameters") or {}
    token_param = qs.get("token", "")
    view = qs.get("view", "")
    edit_date = qs.get("edit", "")
    api = qs.get("api", "")

    today_d = _la_today_date()
    today_s = today_d.isoformat()
    la_today = today_s

    start_d = _parse_start_date()

    # ---------------- API: get single date ----------------
    if method == "GET" and api == "get":
        d = (qs.get("date") or "").strip()
        if not d:
            return _json(400, {"error": "date required"})
        try:
            _parse_date(d)
        except Exception:
            return _json(400, {"error": "invalid date"})
        item = _get_item(USER_ID, d)
        if not item:
            return _json(200, {"row": None})
        return _json(
            200,
            {
                "row": {
                    "date": item.get("date", ""),
                    "pushups": int(item.get("pushups", 0)),
                    "pullups": int(item.get("pullups", 0)),
                    "dips": int(item.get("dips", 0)),
                    "plank_minutes": round(int(item.get("plank_seconds", 0)) / 60.0, 1),
                }
            },
        )

    # ---------------- API: full dataset (since start) ----------------
    if method == "GET" and api == "data":
        items = _query_range(USER_ID, start_d.isoformat(), today_s)
        items.sort(key=lambda x: x.get("date", ""), reverse=True)
        rows = []
        for it in items:
            rows.append(
                {
                    "date": it.get("date", ""),
                    "pushups": int(it.get("pushups", 0)),
                    "pullups": int(it.get("pullups", 0)),
                    "dips": int(it.get("dips", 0)),
                    "plank_minutes": round(int(it.get("plank_seconds", 0)) / 60.0, 1),
                }
            )
        return _json(200, {"rows": rows})

    # ---------------- API: upsert row ----------------
    if method == "POST" and api == "upsert":
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8", "ignore")
        try:
            payload = json.loads(body)
        except Exception:
            return _json(400, {"error": "Invalid JSON"})

        d = str(payload.get("date", "")).strip()
        if not d:
            return _json(400, {"error": "date is required"})
        try:
            _parse_date(d)
        except Exception:
            return _json(400, {"error": "invalid date format"})

        pushups = int(payload.get("pushups", 0) or 0)
        pullups = int(payload.get("pullups", 0) or 0)
        dips = int(payload.get("dips", 0) or 0)
        plank_minutes = float(payload.get("plank_minutes", 0) or 0)
        plank_seconds = int(plank_minutes * 60)

        _upsert_item(USER_ID, d, pushups, pullups, dips, plank_seconds)
        return _json(200, {"ok": True})

    # ---------------- API: delete row ----------------
    if method == "POST" and api == "delete":
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8", "ignore")
        try:
            payload = json.loads(body)
        except Exception:
            return _json(400, {"error": "Invalid JSON"})
        d = str(payload.get("date", "")).strip()
        if not d:
            return _json(400, {"error": "date is required"})
        _delete_item(USER_ID, d)
        return _json(200, {"ok": True})

    # ---------------- CSV export ----------------
    if method == "GET" and view == "csv":
        items = _query_range(USER_ID, start_d.isoformat(), today_s)
        items.sort(key=lambda x: x.get("date", ""))

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["date", "pushups", "pullups", "dips", "plank_minutes"])
        for it in items:
            w.writerow(
                [
                    it.get("date", ""),
                    int(it.get("pushups", 0)),
                    int(it.get("pullups", 0)),
                    int(it.get("dips", 0)),
                    f"{int(it.get('plank_seconds', 0))/60:.1f}",
                ]
            )
        return _resp(200, out.getvalue(), content_type="text/csv")

    # ---------------- Old edit-by-date page ----------------
    if edit_date:
        if method == "GET":
            item = _get_item(USER_ID, edit_date)
            if not item:
                return _resp(404, "Not found", content_type="text/plain")
            return _resp(200, _render_edit_form(token_param, edit_date, item))

        if method == "POST":
            body = event.get("body") or ""
            if event.get("isBase64Encoded"):
                import base64

                body = base64.b64decode(body).decode("utf-8", "ignore")
            form = urllib.parse.parse_qs(body)

            def get_int(name):
                try:
                    return int(float(form.get(name, ["0"])[0] or 0))
                except Exception:
                    return 0

            pushups = get_int("pushups")
            pullups = get_int("pullups")
            dips = get_int("dips")
            try:
                plank_minutes = float(form.get("plank_minutes", ["0"])[0] or 0)
            except Exception:
                plank_minutes = 0.0
            plank_seconds = int(plank_minutes * 60)

            _upsert_item(USER_ID, edit_date, pushups, pullups, dips, plank_seconds)
            message = f"Saved changes for {edit_date}."
        else:
            return _resp(405, "Method not allowed", content_type="text/plain")
    else:
        message = ""

    # ---------------- Main page behavior ----------------
    selected_date = qs.get("log_date") or la_today

    # Prefill values for selected_date (GET)
    selected_vals = {"pushups": "", "pullups": "", "dips": "", "plank_minutes": ""}
    try:
        _parse_date(selected_date)
        item = _get_item(USER_ID, selected_date)
        if item:
            selected_vals = {
                "pushups": str(int(item.get("pushups", 0))),
                "pullups": str(int(item.get("pullups", 0))),
                "dips": str(int(item.get("dips", 0))),
                "plank_minutes": f"{int(item.get('plank_seconds', 0))/60:.1f}",
            }
    except Exception:
        selected_date = la_today

    # Save log form (POST): save to log_date, not always today
    if method == "POST" and not edit_date and not api:
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8", "ignore")
        form = urllib.parse.parse_qs(body)

        log_date = (form.get("log_date", [la_today])[0] or la_today).strip()
        try:
            _parse_date(log_date)
        except Exception:
            log_date = la_today

        def get_int(name):
            try:
                return int(float(form.get(name, ["0"])[0] or 0))
            except Exception:
                return 0

        pushups = get_int("pushups")
        pullups = get_int("pullups")
        dips = get_int("dips")
        try:
            plank_minutes = float(form.get("plank_minutes", ["0"])[0] or 0)
        except Exception:
            plank_minutes = 0.0
        plank_seconds = int(plank_minutes * 60)

        _upsert_item(USER_ID, log_date, pushups, pullups, dips, plank_seconds)

        selected_date = log_date
        selected_vals = {
            "pushups": str(pushups),
            "pullups": str(pullups),
            "dips": str(dips),
            "plank_minutes": f"{plank_minutes:g}",
        }
        message = f"Saved for {log_date}."

    # ---------------- Summary calculations (LA date) ----------------
    wk_start = _week_start(today_d).isoformat()
    wk_end = (_week_start(today_d) + timedelta(days=6)).isoformat()

    week_items = _query_range(USER_ID, wk_start, wk_end)
    week_totals = _sum_items(week_items)

    month_start = today_d.replace(day=1).isoformat()
    month_items = _query_range(USER_ID, month_start, today_s)
    month_totals = _sum_items(month_items)

    all_items = _query_range(USER_ID, start_d.isoformat(), today_s)
    all_totals = _sum_items(all_items)

    elapsed_days, expected, on_track, remaining = _pace_metrics(all_totals, start_d, today_d)
    summary_html = _build_summary_html(
        week_totals=week_totals,
        month_totals=month_totals,
        all_totals=all_totals,
        elapsed_days=elapsed_days,
        expected=expected,
        on_track=on_track,
        remaining=remaining,
        today_d=today_d,
    )

    # last 30 days history (LA date)
    start_hist = (today_d - timedelta(days=29)).isoformat()
    hist_items = _query_range(USER_ID, start_hist, today_s)
    hist_items.sort(key=lambda x: x.get("date", ""), reverse=True)
    history_html = _build_history_html(hist_items, token_param)

    return _resp(
        200,
        _render_page(
            token_param=token_param,
            selected_date=selected_date,
            selected_vals=selected_vals,
            summary_html=summary_html,
            history_html=history_html,
            message=message,
        ),
    )
