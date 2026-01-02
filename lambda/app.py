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

GOALS = {
    "plank_seconds": 1500 * 60,  # 1500 minutes
    "pullups": 2000,
    "dips": 5000,
    "pushups": 15000,
}

LA_TZ = ZoneInfo("America/Los_Angeles")
DATA_RANGE_END = "9999-12-31"

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


def _metric_label(key: str) -> str:
    if key == "plank_seconds":
        return "Plank"
    if key == "pullups":
        return "Pull-ups"
    if key == "dips":
        return "Dips"
    if key == "pushups":
        return "Pushups"
    return key


def _build_week_glance_html(week_totals):
    weekly_targets = {k: (GOALS[k] / 365.0) * 7.0 for k in GOALS}

    def one(key: str) -> str:
        done = float(week_totals.get(key, 0))
        target = float(weekly_targets[key])
        pct = _pct(done, target)

        if key == "plank_seconds":
            done_disp = _fmt(key, done)
            target_disp = _fmt(key, target)
        else:
            done_disp = str(int(done))
            target_disp = f"{target:.1f}"

        return f"""
        <div class="glRow">
          <div class="glTop">
            <div class="glName">{_metric_label(key)}</div>
            <div class="glVal">{done_disp} / {target_disp}</div>
          </div>
          <div class="bar"><div class="fill" style="width:{pct}%"></div></div>
          <div class="muted">{pct}%</div>
        </div>
        """

    return f"""
    <div class="muted" style="margin-bottom:8px;">
      This week’s sprint vs. your weekly pace targets.
    </div>
    <div class="glWrap">
      {one("plank_seconds")}
      {one("pullups")}
      {one("dips")}
      {one("pushups")}
    </div>
    """


def _build_progress_html(
    week_totals,
    month_totals,
    all_totals,
    elapsed_days,
    expected,
    on_track,
    remaining,
    today_d: date,
):
    weekly_targets = {k: (GOALS[k] / 365.0) * 7.0 for k in GOALS}
    dim = _days_in_month(today_d)
    monthly_targets = {k: (GOALS[k] / 365.0) * dim for k in GOALS}
    month_name = today_d.strftime("%B")

    def progress_cell(label, key, done, target):
        pct = _pct(done, target) if target > 0 else 0
        return f"""
          <div class="miniLabel">{label}</div>
          <div class="big">{_fmt(key, done)} / {_fmt(key, target)}</div>
          <div class="bar"><div class="fill" style="width:{pct}%"></div></div>
          <div class="muted">{pct}%</div>
        """

    def row(label, key):
        status = "✅" if on_track[key] else "⚠️"
        return f"""
        <tr>
          <td class="rowhead">{label}</td>
          <td>{progress_cell("Total", key, all_totals.get(key, 0), GOALS[key])}</td>
          <td>{progress_cell("This week", key, week_totals.get(key, 0), weekly_targets[key])}</td>
          <td>{progress_cell("This month", key, month_totals.get(key, 0), monthly_targets[key])}</td>
          <td>{_fmt(key, expected[key])}</td>
          <td>{_fmt(key, remaining[key])}</td>
          <td>{status}</td>
        </tr>
        """

    return f"""
    <div class="muted">Elapsed days since start: {elapsed_days}</div>
    <div class="muted">Week is Monday–Sunday. {month_name} has {dim} days.</div>
    <table>
      <thead>
        <tr>
          <th>Metric</th>
          <th>Total</th>
          <th>This week</th>
          <th>This month</th>
          <th>Expected by now</th>
          <th>Remaining</th>
          <th>On track</th>
        </tr>
      </thead>
      <tbody>
        {row("Plank", "plank_seconds")}
        {row("Pull-ups", "pullups")}
        {row("Dips", "dips")}
        {row("Pushups", "pushups")}
      </tbody>
    </table>
    """


HTML_TEMPLATE = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Daily Fitness Tracker</title>
  <style>
    :root {
      --muted: #9aa4b2;
      --text: #e8eef6;
      --line: rgba(255,255,255,0.10);
      --accent: #7dd3fc;
      --danger: #fb7185;
      --ok: #34d399;
      --bg1: #0b0c10;
      --bg2: #0f172a;
    }
    body {
      margin: 0;
      background: linear-gradient(180deg, var(--bg1), var(--bg2));
      color: var(--text);
      font-family: -apple-system, system-ui, Segoe UI, Roboto, Arial;
    }
    .wrap { max-width: 980px; margin: 0 auto; padding: 18px 14px 40px; }
    h1 { margin: 6px 0 0; font-size: 26px; letter-spacing: 0.2px; }

    .sub {
      color: var(--muted);
      font-size: 13px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      padding: 5px 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      font-size: 12px;
      color: var(--muted);
      background: rgba(255,255,255,0.02);
    }

    .tabs { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
    .tab {
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.02);
      color: var(--text);
      padding: 8px 10px;
      border-radius: 10px;
      cursor: pointer;
      font-size: 14px;
      flex: 1 1 0;
      min-width: 120px;
    }
    .tab.active {
      border-color: rgba(125,211,252,0.35);
      box-shadow: 0 0 0 2px rgba(125,211,252,0.12) inset;
    }

    .card {
      background: rgba(18,20,28,0.85);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.20);
      margin-top: 12px;
      overflow: hidden;
    }

    .grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
    @media (min-width: 920px) {
      .grid { grid-template-columns: 0.82fr 1.18fr; align-items: start; }
    }

    label { display: block; color: var(--muted); font-size: 12px; margin: 8px 0 6px; }
    input {
      width: 100%;
      padding: 12px 12px;
      font-size: 16px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      outline: none;
      box-sizing: border-box;
    }
    input:focus {
      border-color: rgba(125,211,252,0.35);
      box-shadow: 0 0 0 3px rgba(125,211,252,0.12);
    }

    /* Smaller log bubbles + keep them from stretching into right column */
    .logInput, .logDate {
      padding: 6px 9px;
      font-size: 13px;
      border-radius: 999px;
      max-width: 420px;
    }

    button {
      width: 100%;
      margin-top: 10px;
      padding: 12px 12px;
      font-size: 16px;
      border-radius: 12px;
      border: 1px solid rgba(125,211,252,0.35);
      background: rgba(125,211,252,0.12);
      color: var(--text);
      cursor: pointer;
    }
    button:hover { background: rgba(125,211,252,0.18); }

    .msg {
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(125,211,252,0.25);
      background: rgba(125,211,252,0.08);
      color: var(--text);
      font-weight: 600;
      font-size: 14px;
    }

    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; font-size: 14px; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; }
    .rowhead { font-weight: 700; }
    .miniLabel { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }
    .big { font-weight: 700; margin-bottom: 6px; }

    .bar {
      height: 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.02);
      overflow: hidden;
      width: 100%;
      margin-top: 2px;
    }
    .fill { height: 100%; background: rgba(125,211,252,0.75); width: 0%; }

    .muted { color: var(--muted); font-size: 12px; }
    .hide { display: none; }

    .glWrap { display: grid; gap: 10px; margin-top: 6px; }
    .glRow { padding: 10px 10px; border: 1px solid var(--line); border-radius: 14px; background: rgba(255,255,255,0.02); }
    .glTop { display:flex; justify-content: space-between; align-items: baseline; gap: 10px; margin-bottom: 6px; }
    .glName { font-weight: 800; }
    .glVal { color: var(--muted); font-size: 12px; white-space: nowrap; }

    .toolbar {
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      align-items:center;
      justify-content:space-between;
      margin-top:8px;
    }
    .toolbar .left { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    .mini {
      padding: 10px 10px;
      font-size: 14px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      outline: none;
    }

    .gridwrap { overflow:auto; border-radius: 14px; border: 1px solid var(--line); margin-top: 10px; }
    .datatable { width: 100%; border-collapse: collapse; margin: 0; min-width: 720px; }
    .datatable th, .datatable td { border-bottom: 1px solid var(--line); padding: 10px 8px; font-size: 14px; vertical-align: middle; }

    .cell {
      width: 70px;
      max-width: 70px;
      padding: 8px 10px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.02);
      color: var(--text);
      font-size: 14px;
      outline: none;
      text-align: center;
      box-sizing: border-box;
    }
    .cell:focus { border-color: rgba(125,211,252,0.35); box-shadow: 0 0 0 3px rgba(125,211,252,0.12); }

    .smallbtn {
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
    }
    .smallbtn:hover { background: rgba(255,255,255,0.06); }

    .rowbtn {
      padding: 8px 10px;
      font-size: 13px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.03);
      color: var(--text);
      cursor: pointer;
      width: 92px;
      text-align:center;
    }
    .rowbtn:hover { background: rgba(255,255,255,0.06); }
    .danger { border-color: rgba(251,113,133,0.35); background: rgba(251,113,133,0.08); }
    .ok { border-color: rgba(52,211,153,0.35); background: rgba(52,211,153,0.08); }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Daily Fitness Tracker</h1>
    <div class="sub">
      <span class="pill">User: __USER_ID__</span>
      <span class="pill">Start: __START_DATE__</span>
      <span class="pill">Today (LA): __TODAY__</span>
    </div>

    <div class="tabs">
      <button class="tab active" data-tab="log">Log</button>
      <button class="tab" data-tab="progress">Progress</button>
      <button class="tab" data-tab="data">Data</button>
    </div>

    __MESSAGE_BLOCK__

    <div id="tab-log" class="card">
      <div class="grid">
        <div>
          <h3 style="margin: 0 0 4px;">Log</h3>
          <div class="muted">Pick a date, edit the numbers, and save.</div>

          <form method="POST" action="__TOKEN_Q__">
            <label>Date</label>
            <input class="logDate" id="log_date" name="log_date" type="date" value="__SELECTED_DATE__" />

            <label>Pushups</label>
            <input class="logInput" id="pushups" name="pushups" inputmode="numeric" pattern="\d*" value="__PUSHUPS__" />

            <label>Pull-ups</label>
            <input class="logInput" id="pullups" name="pullups" inputmode="numeric" pattern="\d*" value="__PULLUPS__" />

            <label>Dips</label>
            <input class="logInput" id="dips" name="dips" inputmode="numeric" pattern="\d*" value="__DIPS__" />

            <label>Plank (minutes)</label>
            <input class="logInput" id="plank_minutes" name="plank_minutes" inputmode="decimal" value="__PLANK_MIN__" />

            <button type="submit">Save</button>
          </form>
        </div>

        <div>
          <h3 style="margin: 0 0 6px;">Weekly paceboard</h3>
          __WEEK_GLANCE_HTML__
        </div>
      </div>
    </div>

    <div id="tab-progress" class="card hide">
      <h3 style="margin:0 0 6px;">Progress</h3>
      __PROGRESS_HTML__
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
          <a class="smallbtn" href="__EXPORT_LINK__">Export CSV</a>
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
    const tabs = document.querySelectorAll(".tab");
    const panes = {
      log: document.getElementById("tab-log"),
      progress: document.getElementById("tab-progress"),
      data: document.getElementById("tab-data"),
    };

    tabs.forEach(btn => {
      btn.addEventListener("click", () => {
        tabs.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        Object.values(panes).forEach(p => p.classList.add("hide"));
        panes[btn.dataset.tab].classList.remove("hide");
        if (btn.dataset.tab === "data") loadData();
      });
    });

    const API_GET = "__API_GET__";
    const logDate = document.getElementById("log_date");

    async function loadLogForDate(d) {
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
    }

    logDate.addEventListener("change", (e) => loadLogForDate(e.target.value));

    const API_DATA = "__API_DATA__";
    const API_UPSERT = "__API_UPSERT__";
    const API_DELETE = "__API_DELETE__";

    const statusEl = document.getElementById("status");
    const tbody = document.getElementById("dtbody");
    const filterEl = document.getElementById("filter");
    const reloadBtn = document.getElementById("reload");
    const saveAllBtn = document.getElementById("saveAll");
    const newDateEl = document.getElementById("newDate");
    const addRowBtn = document.getElementById("addRow");

    const dirty = new Set();
    let cache = [];

    function setStatus(msg) { if (statusEl) statusEl.textContent = msg; }

    function esc(s) {
      return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    }

    function render(rows) {
      if (!tbody) return;
      tbody.innerHTML = "";
      const f = (filterEl?.value || "").trim();
      const filtered = f ? rows.filter(r => (r.date || "").includes(f)) : rows;

      for (const r of filtered) {
        const key = r.date;
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><strong>${esc(r.date)}</strong></td>
          <td><input class="cell" data-key="${esc(key)}" data-field="pushups" value="${esc(r.pushups ?? 0)}" /></td>
          <td><input class="cell" data-key="${esc(key)}" data-field="pullups" value="${esc(r.pullups ?? 0)}" /></td>
          <td><input class="cell" data-key="${esc(key)}" data-field="dips" value="${esc(r.dips ?? 0)}" /></td>
          <td><input class="cell" data-key="${esc(key)}" data-field="plank_minutes" value="${esc(r.plank_minutes ?? 0)}" /></td>
          <td style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
            <button class="rowbtn ok" data-action="save" data-key="${esc(key)}">Save</button>
            <button class="rowbtn danger" data-action="delete" data-key="${esc(key)}">Delete</button>
          </td>
        `;
        tbody.appendChild(tr);
      }

      document.querySelectorAll(".cell").forEach(inp => {
        inp.addEventListener("input", () => {
          dirty.add(inp.dataset.key);
          setStatus(`Modified rows: ${dirty.size}`);
        });
      });

      document.querySelectorAll(".rowbtn").forEach(btn => {
        btn.addEventListener("click", async () => {
          const action = btn.dataset.action;
          const key = btn.dataset.key;
          if (action === "save") await saveRow(key);
          else if (action === "delete") {
            const ok = confirm(`Delete ${key}?`);
            if (ok) await deleteRow(key);
          }
        });
      });
    }

    async function loadData() {
      setStatus("Loading data...");
      const res = await fetch(API_DATA);
      if (!res.ok) {
        const t = await res.text().catch(() => "");
        setStatus(`Load failed (${res.status}) ${t}`);
        return;
      }
      const data = await res.json();
      cache = data.rows || [];
      dirty.clear();
      render(cache);
      setStatus(`Loaded ${cache.length} rows.`);
    }

    function getRowFromInputs(dateStr) {
      const inputs = document.querySelectorAll(`.cell[data-key="${CSS.escape(dateStr)}"]`);
      const obj = { date: dateStr, pushups: 0, pullups: 0, dips: 0, plank_minutes: 0 };
      inputs.forEach(inp => { obj[inp.dataset.field] = inp.value; });
      obj.pushups = parseInt(obj.pushups || "0", 10) || 0;
      obj.pullups = parseInt(obj.pullups || "0", 10) || 0;
      obj.dips = parseInt(obj.dips || "0", 10) || 0;
      obj.plank_minutes = parseFloat(obj.plank_minutes || "0") || 0;
      return obj;
    }

    async function saveRow(dateStr) {
      const r = getRowFromInputs(dateStr);
      setStatus(`Saving ${dateStr}...`);
      const res = await fetch(API_UPSERT, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(r),
      });
      if (!res.ok) {
        const t = await res.text().catch(() => "");
        setStatus(`Save failed for ${dateStr} (${res.status}) ${t}`);
        return;
      }
      dirty.delete(dateStr);
      setStatus(`Saved ${dateStr}. Modified rows: ${dirty.size}`);
      const idx = cache.findIndex(x => x.date === dateStr);
      if (idx >= 0) cache[idx] = r;
      else cache = [r].concat(cache);
    }

    async function deleteRow(dateStr) {
      setStatus(`Deleting ${dateStr}...`);
      const res = await fetch(API_DELETE, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ date: dateStr }),
      });
      if (!res.ok) {
        const t = await res.text().catch(() => "");
        setStatus(`Delete failed for ${dateStr} (${res.status}) ${t}`);
        return;
      }
      dirty.delete(dateStr);
      cache = cache.filter(x => x.date !== dateStr);
      render(cache);
      setStatus(`Deleted ${dateStr}. Rows now: ${cache.length}`);
    }

    if (reloadBtn) reloadBtn.addEventListener("click", loadData);
    if (filterEl) filterEl.addEventListener("input", () => render(cache));

    if (saveAllBtn) saveAllBtn.addEventListener("click", async () => {
      if (dirty.size === 0) { setStatus("No modified rows to save."); return; }
      const keys = Array.from(dirty);
      setStatus(`Saving ${keys.length} rows...`);
      for (const k of keys) await saveRow(k);
      setStatus("Saved all modified rows.");
    });

    if (addRowBtn) addRowBtn.addEventListener("click", () => {
      const d = (newDateEl.value || "").trim();
      if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) { alert("Enter date as YYYY-MM-DD"); return; }
      if (cache.some(x => x.date === d)) { alert("That date already exists."); return; }
      cache = [{ date: d, pushups: 0, pullups: 0, dips: 0, plank_minutes: 0 }].concat(cache);
      render(cache);
      dirty.add(d);
      setStatus(`Added ${d} (not saved yet). Modified rows: ${dirty.size}`);
    });
  </script>
</body>
</html>
"""


def _render_page(
    token_param: str,
    selected_date: str,
    selected_vals,
    progress_html: str,
    week_glance_html: str,
    message: str,
    export_link: str,
    api_get: str,
    api_data: str,
    api_upsert: str,
    api_delete: str,
):
    token_q = ""
    if token_param:
        token_q = f"?token={urllib.parse.quote(token_param)}"

    message_block = ""
    if message:
        safe_msg = (message or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        message_block = f"<div class='msg'>{safe_msg}</div>"

    html = HTML_TEMPLATE
    html = html.replace("__USER_ID__", USER_ID)
    html = html.replace("__START_DATE__", START_DATE)
    html = html.replace("__TODAY__", _la_today_str())
    html = html.replace("__MESSAGE_BLOCK__", message_block)
    html = html.replace("__TOKEN_Q__", token_q)

    html = html.replace("__SELECTED_DATE__", selected_date)
    html = html.replace("__PUSHUPS__", str(selected_vals.get("pushups", "")))
    html = html.replace("__PULLUPS__", str(selected_vals.get("pullups", "")))
    html = html.replace("__DIPS__", str(selected_vals.get("dips", "")))
    html = html.replace("__PLANK_MIN__", str(selected_vals.get("plank_minutes", "")))

    html = html.replace("__PROGRESS_HTML__", progress_html)
    html = html.replace("__WEEK_GLANCE_HTML__", week_glance_html)

    html = html.replace("__EXPORT_LINK__", export_link)
    html = html.replace("__API_GET__", api_get)
    html = html.replace("__API_DATA__", api_data)
    html = html.replace("__API_UPSERT__", api_upsert)
    html = html.replace("__API_DELETE__", api_delete)
    return html


def handler(event, context):
    try:
        if not _require_token(event):
            return _resp(403, "Forbidden (bad token)", content_type="text/plain")

        method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
        qs = event.get("queryStringParameters") or {}
        token_param = qs.get("token", "")
        view = qs.get("view", "")
        api = qs.get("api", "")

        def q_add(param: str) -> str:
            if token_param:
                base = f"?token={urllib.parse.quote(token_param)}"
                return f"{base}&{param}" if param else base
            return f"?{param}" if param else "?"

        export_link = q_add("view=csv")
        api_data = q_add("api=data")
        api_upsert = q_add("api=upsert")
        api_delete = q_add("api=delete")
        api_get = q_add("api=get")

        today_d = _la_today_date()
        today_s = today_d.isoformat()
        la_today = today_s
        start_d = _parse_start_date()

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

        if method == "GET" and api == "data":
            items = _query_range(USER_ID, start_d.isoformat(), DATA_RANGE_END)
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

        if method == "GET" and view == "csv":
            items = _query_range(USER_ID, start_d.isoformat(), DATA_RANGE_END)
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

        message = ""
        selected_date = qs.get("log_date") or la_today

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

        if method == "POST" and not api:
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

        wk_start = _week_start(today_d).isoformat()
        wk_end = (_week_start(today_d) + timedelta(days=6)).isoformat()
        week_items = _query_range(USER_ID, wk_start, wk_end)
        week_totals = _sum_items(week_items)

        month_start = today_d.replace(day=1).isoformat()
        month_items = _query_range(USER_ID, month_start, today_s)
        month_totals = _sum_items(month_items)

        all_items = _query_range(USER_ID, start_d.isoformat(), DATA_RANGE_END)
        all_totals = _sum_items(all_items)

        elapsed_days, expected, on_track, remaining = _pace_metrics(all_totals, start_d, today_d)

        progress_html = _build_progress_html(
            week_totals=week_totals,
            month_totals=month_totals,
            all_totals=all_totals,
            elapsed_days=elapsed_days,
            expected=expected,
            on_track=on_track,
            remaining=remaining,
            today_d=today_d,
        )

        week_glance_html = _build_week_glance_html(week_totals)

        return _resp(
            200,
            _render_page(
                token_param=token_param,
                selected_date=selected_date,
                selected_vals=selected_vals,
                progress_html=progress_html,
                week_glance_html=week_glance_html,
                message=message,
                export_link=export_link,
                api_get=api_get,
                api_data=api_data,
                api_upsert=api_upsert,
                api_delete=api_delete,
            ),
        )

    except Exception as e:
        return _resp(500, f"Internal Server Error:\n{type(e).__name__}: {e}", content_type="text/plain")
