import csv
import io
import sqlite3
import zipfile
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from xml.sax.saxutils import escape
from dotenv import load_dotenv
from flask import Flask, Response, redirect, render_template_string, request, url_for


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "time_tracker.db"
DEFAULT_TIMEZONE = "Australia/Sydney"

STATUS_WORKING = "WORKING"
STATUS_ON_BREAK = "ON_BREAK"
STATUS_FINISHED = "FINISHED"

app = Flask(__name__)


def get_app_timezone() -> ZoneInfo:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def today_local() -> date:
    return datetime.now(get_app_timezone()).date()


def now_local() -> datetime:
    return datetime.now(get_app_timezone()).replace(tzinfo=None)


def db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def parse_db_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def format_time(value: str | None) -> str:
    parsed = parse_db_datetime(value)
    return parsed.strftime("%H:%M") if parsed else "-"


def format_work_duration(total_seconds: int) -> str:
    total_minutes = max(0, total_seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m"


def format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents / 100:,.2f}"


def parse_money_to_cents(raw_amount: str) -> int:
    cleaned = raw_amount.strip().replace("$", "").replace(",", "")
    amount = float(cleaned)
    if amount < 0:
        raise ValueError("Amount cannot be negative.")
    return int(round(amount * 100))


def parse_time_for_date(work_date: str, raw_time: str) -> str | None:
    raw_time = raw_time.strip()
    if not raw_time:
        return None
    parsed_date = datetime.strptime(work_date, "%Y-%m-%d").date()
    parsed_time = datetime.strptime(raw_time, "%H:%M").time()
    return datetime.combine(parsed_date, parsed_time).replace(microsecond=0).isoformat(sep=" ")


def calculate_work_seconds(row: sqlite3.Row) -> int:
    if not row["in_time"] or not row["out_time"]:
        return 0
    in_time = parse_db_datetime(row["in_time"])
    out_time = parse_db_datetime(row["out_time"])
    break_seconds = int(row["total_break_seconds"] or 0)
    return max(0, int((out_time - in_time).total_seconds()) - break_seconds)



def excel_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def excel_cell(row_index: int, column_index: int, value) -> str:
    ref = f"{excel_column_name(column_index)}{row_index}"
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def build_xlsx(sheets: list[tuple[str, list[str], list[list]]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as workbook:
        content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>"""
        content_types += "".join(f"<Override PartName=\"/xl/worksheets/sheet{i}.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>" for i in range(1, len(sheets) + 1))
        workbook.writestr("[Content_Types].xml", content_types + "</Types>")
        workbook.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>""")
        workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">"""
        workbook_rels += "".join(f"<Relationship Id=\"rId{i}\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet{i}.xml\"/>" for i in range(1, len(sheets) + 1))
        workbook_rels += f"<Relationship Id=\"rId{len(sheets) + 1}\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles\" Target=\"styles.xml\"/></Relationships>"
        workbook.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        workbook.writestr("xl/styles.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs></styleSheet>""")
        sheet_refs = "".join(f"<sheet name=\"{escape(name[:31])}\" sheetId=\"{i}\" r:id=\"rId{i}\"/>" for i, (name, _, _) in enumerate(sheets, 1))
        workbook.writestr("xl/workbook.xml", f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{sheet_refs}</sheets></workbook>""")
        for sheet_index, (_, headers, rows) in enumerate(sheets, 1):
            all_rows = [headers] + rows
            xml_rows = []
            for row_index, row in enumerate(all_rows, 1):
                cells = "".join(excel_cell(row_index, column_index, value) for column_index, value in enumerate(row, 1))
                xml_rows.append(f'<row r="{row_index}">{cells}</row>')
            worksheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>""" + "".join(xml_rows) + "</sheetData></worksheet>"
            workbook.writestr(f"xl/worksheets/sheet{sheet_index}.xml", worksheet)
    return output.getvalue()


def make_xlsx_response(filename: str, sheets: list[tuple[str, list[str], list[list]]]) -> Response:
    return Response(
        build_xlsx(sheets),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

def init_db() -> None:
    with db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'EMPLOYEE',
                hourly_rate_cents INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = conn.execute("PRAGMA table_info(users)").fetchall()
        names = {column["name"] for column in columns}
        if "role" not in names:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'EMPLOYEE'")
        if "hourly_rate_cents" not in names:
            conn.execute(
                "ALTER TABLE users ADD COLUMN hourly_rate_cents INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timesheets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date DATE NOT NULL,
                in_time DATETIME,
                out_time DATETIME,
                break_start DATETIME,
                total_break_seconds INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS advances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date DATE NOT NULL,
                amount_cents INTEGER NOT NULL,
                note TEXT,
                created_at DATETIME NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
            """
        )
        conn.commit()


def load_users():
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT user_id, full_name, role, hourly_rate_cents
            FROM users
            ORDER BY role, full_name
            """
        ).fetchall()


def load_timesheets_for_date(work_date: str):
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT t.*, u.full_name, u.hourly_rate_cents
            FROM timesheets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.date = ?
            ORDER BY u.full_name, t.id
            """,
            (work_date,),
        ).fetchall()


def load_timesheets_for_month(month: str, user_id: int | None = None):
    params: list[object] = [f"{month}-%"]
    user_filter = ""
    if user_id:
        user_filter = "AND t.user_id = ?"
        params.append(user_id)

    with db_connection() as conn:
        return conn.execute(
            f"""
            SELECT t.*, u.full_name, u.hourly_rate_cents
            FROM timesheets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.date LIKE ?
            {user_filter}
            ORDER BY u.full_name, t.date, t.id
            """,
            params,
        ).fetchall()


def load_advances_for_month(month: str, user_id: int | None = None):
    params: list[object] = [f"{month}-%"]
    user_filter = ""
    if user_id:
        user_filter = "AND a.user_id = ?"
        params.append(user_id)

    with db_connection() as conn:
        return conn.execute(
            f"""
            SELECT a.*, u.full_name
            FROM advances a
            JOIN users u ON u.user_id = a.user_id
            WHERE a.date LIKE ?
            {user_filter}
            ORDER BY u.full_name, a.date, a.id
            """,
            params,
        ).fetchall()


def build_salary_rows(month: str, user_id: int | None = None):
    users = {row["user_id"]: dict(row) for row in load_users()}
    timesheets = load_timesheets_for_month(month, user_id)
    advances = load_advances_for_month(month, user_id)
    grouped: dict[int, dict] = {}

    for user in users.values():
        if user_id and user["user_id"] != user_id:
            continue
        grouped[user["user_id"]] = {
            "user": user,
            "work_seconds": 0,
            "gross_cents": 0,
            "advance_cents": 0,
            "net_cents": 0,
            "shifts": 0,
        }

    for row in timesheets:
        item = grouped.setdefault(
            row["user_id"],
            {
                "user": users.get(row["user_id"], dict(row)),
                "work_seconds": 0,
                "gross_cents": 0,
                "advance_cents": 0,
                "net_cents": 0,
                "shifts": 0,
            },
        )
        work_seconds = calculate_work_seconds(row)
        item["work_seconds"] += work_seconds
        item["shifts"] += 1

    for row in advances:
        item = grouped.setdefault(
            row["user_id"],
            {
                "user": users.get(row["user_id"], dict(row)),
                "work_seconds": 0,
                "gross_cents": 0,
                "advance_cents": 0,
                "net_cents": 0,
                "shifts": 0,
            },
        )
        item["advance_cents"] += int(row["amount_cents"] or 0)

    for item in grouped.values():
        rate = int(item["user"].get("hourly_rate_cents") or 0)
        item["gross_cents"] = round(item["work_seconds"] * rate / 3600)
        item["net_cents"] = item["gross_cents"] - item["advance_cents"]

    return sorted(grouped.values(), key=lambda item: item["user"].get("full_name", ""))


TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Time Tracker Dashboard</title>
  <style>
    :root {
      --bg: #f4f6f8;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #687586;
      --line: #d9e0e7;
      --accent: #0f766e;
      --danger: #b42318;
      --warn: #9a5b00;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 Segoe UI, Arial, sans-serif;
    }
    header {
      background: #14213d;
      color: white;
      padding: 18px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 20px;
    }
    h1, h2 { margin: 0; }
    h1 { font-size: 22px; }
    h2 { font-size: 17px; margin-bottom: 12px; }
    main {
      max-width: 1280px;
      margin: 0 auto;
      padding: 22px;
    }
    .toolbar, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 16px;
    }
    .toolbar {
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 12px;
      align-items: end;
    }
    label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; }
    input, select, button {
      height: 38px;
      border-radius: 6px;
      border: 1px solid var(--line);
      padding: 0 10px;
      font: inherit;
      background: white;
      color: var(--ink);
    }
    button {
      background: var(--accent);
      border-color: var(--accent);
      color: white;
      cursor: pointer;
      font-weight: 600;
    }
    a.button {
      height: 38px;
      border-radius: 6px;
      padding: 9px 12px;
      background: #334155;
      color: white;
      text-decoration: none;
      text-align: center;
      font-weight: 600;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      background: white;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      background: #f9fafb;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, 420px);
      gap: 16px;
    }
    .forms {
      display: grid;
      gap: 16px;
    }
    .form-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .inline-edit {
      display: grid;
      grid-template-columns: 74px 74px 78px 120px 70px;
      gap: 6px;
      align-items: center;
    }
    .inline-edit input, .inline-edit select, .inline-edit button {
      height: 32px;
      font-size: 12px;
    }
    .form-grid .wide { grid-column: 1 / -1; }
    .status {
      display: inline-block;
      border-radius: 999px;
      padding: 3px 9px;
      font-size: 12px;
      font-weight: 700;
      background: #eef2ff;
      color: #3730a3;
    }
    .status.WORKING { background: #dcfce7; color: #166534; }
    .status.ON_BREAK { background: #fef3c7; color: var(--warn); }
    .status.FINISHED { background: #e5e7eb; color: #374151; }
    .money { font-weight: 700; }
    .negative { color: var(--danger); }
    .muted { color: var(--muted); }
    @media (max-width: 900px) {
      .toolbar, .grid { grid-template-columns: 1fr; }
      table { display: block; overflow-x: auto; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Time Tracker Dashboard</h1>
      <div class="muted">Local admin screen for Telegram attendance and salary</div>
    </div>
    <div>{{ today }}</div>
  </header>

  <main>
    <form class="toolbar" method="get" action="/">
      <label>Date
        <input type="date" name="date" value="{{ selected_date }}">
      </label>
      <label>Salary Month
        <input type="month" name="month" value="{{ selected_month }}">
      </label>
      <label>Employee
        <select name="user_id">
          <option value="">All employees</option>
          {% for user in users %}
            <option value="{{ user.user_id }}" {% if selected_user_id == user.user_id|string %}selected{% endif %}>
              {{ user.full_name }} ({{ user.user_id }})
            </option>
          {% endfor %}
        </select>
      </label>
      <button type="submit">Refresh</button>
      <a class="button" href="{{ url_for('export_day_csv', date=selected_date) }}">Export Day CSV</a>
      <a class="button" href="{{ url_for('export_day_xlsx', date=selected_date) }}">Export Day Excel</a>
      <a class="button" href="{{ url_for('export_salary_csv', month=selected_month, user_id=selected_user_id) }}">Export Salary CSV</a>
      <a class="button" href="{{ url_for('export_salary_xlsx', month=selected_month, user_id=selected_user_id) }}">Export Salary Excel</a>
    </form>

    <section>
      <h2>Today / Selected Date</h2>
      <table>
        <thead>
          <tr>
            <th>Employee</th>
            <th>Status</th>
            <th>Clock In</th>
            <th>Clock Out</th>
            <th>Break</th>
            <th>Work Time</th>
            <th>Edit</th>
          </tr>
        </thead>
        <tbody>
          {% for row in day_rows %}
            <tr>
              <td>{{ row.full_name }}<br><span class="muted">{{ row.user_id }}</span></td>
              <td><span class="status {{ row.status }}">{{ row.status }}</span></td>
              <td>{{ format_time(row.in_time) }}</td>
              <td>{{ format_time(row.out_time) }}</td>
              <td>{{ (row.total_break_seconds or 0) // 60 }}m</td>
              <td>{{ format_work_duration(calculate_work_seconds(row)) }}</td>
              <td>
                <form class="inline-edit" method="post" action="/update-timesheet">
                  <input type="hidden" name="timesheet_id" value="{{ row.id }}">
                  <input type="hidden" name="return_date" value="{{ selected_date }}">
                  <input name="in_time" value="{{ format_time(row.in_time) if row.in_time else '' }}" placeholder="In">
                  <input name="out_time" value="{{ format_time(row.out_time) if row.out_time else '' }}" placeholder="Out">
                  <input name="break_minutes" value="{{ (row.total_break_seconds or 0) // 60 }}" placeholder="Break">
                  <select name="status">
                    <option value="WORKING" {% if row.status == 'WORKING' %}selected{% endif %}>WORKING</option>
                    <option value="ON_BREAK" {% if row.status == 'ON_BREAK' %}selected{% endif %}>ON_BREAK</option>
                    <option value="FINISHED" {% if row.status == 'FINISHED' %}selected{% endif %}>FINISHED</option>
                  </select>
                  <button type="submit">Save</button>
                </form>
              </td>
            </tr>
          {% else %}
            <tr><td colspan="7" class="muted">No timesheets for this date.</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </section>

    <div class="grid">
      <section>
        <h2>Salary Report</h2>
        <table>
          <thead>
            <tr>
              <th>Employee</th>
              <th>Rate</th>
              <th>Shifts</th>
              <th>Hours</th>
              <th>Gross</th>
              <th>Advance</th>
              <th>Net</th>
            </tr>
          </thead>
          <tbody>
            {% for item in salary_rows %}
              <tr>
                <td>{{ item.user.full_name }}<br><span class="muted">{{ item.user.user_id }}</span></td>
                <td>{{ format_money(item.user.hourly_rate_cents or 0) }}/h</td>
                <td>{{ item.shifts }}</td>
                <td>{{ format_work_duration(item.work_seconds) }}</td>
                <td class="money">{{ format_money(item.gross_cents) }}</td>
                <td>{{ format_money(item.advance_cents) }}</td>
                <td class="money {% if item.net_cents < 0 %}negative{% endif %}">{{ format_money(item.net_cents) }}</td>
              </tr>
            {% else %}
              <tr><td colspan="7" class="muted">No salary data for this month.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </section>

      <div class="forms">
        <section>
          <h2>Set Hourly Rate</h2>
          <form class="form-grid" method="post" action="/set-rate">
            <label class="wide">Employee
              <select name="user_id" required>
                {% for user in users %}
                  <option value="{{ user.user_id }}">{{ user.full_name }} ({{ user.user_id }})</option>
                {% endfor %}
              </select>
            </label>
            <label>Rate / hour
              <input name="rate" placeholder="16" required>
            </label>
            <button type="submit">Save Rate</button>
          </form>
        </section>

        <section>
          <h2>Add Advance</h2>
          <form class="form-grid" method="post" action="/add-advance">
            <label class="wide">Employee
              <select name="user_id" required>
                {% for user in users %}
                  <option value="{{ user.user_id }}">{{ user.full_name }} ({{ user.user_id }})</option>
                {% endfor %}
              </select>
            </label>
            <label>Amount
              <input name="amount" placeholder="100" required>
            </label>
            <label>Note
              <input name="note" placeholder="optional">
            </label>
            <button class="wide" type="submit">Save Advance</button>
          </form>
        </section>

        <section>
          <h2>Employees</h2>
          <table>
            <thead><tr><th>Name</th><th>Role</th><th>Rate</th></tr></thead>
            <tbody>
              {% for user in users %}
                <tr>
                  <td>{{ user.full_name }}<br><span class="muted">{{ user.user_id }}</span></td>
                  <td>{{ user.role }}</td>
                  <td>{{ format_money(user.hourly_rate_cents or 0) }}/h</td>
                </tr>
              {% else %}
                <tr><td colspan="3" class="muted">No users saved yet.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  </main>
</body>
</html>
"""


@app.route("/")
def index():
    selected_date = request.args.get("date") or today_local().isoformat()
    selected_month = request.args.get("month") or today_local().strftime("%Y-%m")
    selected_user_id = request.args.get("user_id") or ""
    user_id = int(selected_user_id) if selected_user_id.isdigit() else None

    users = load_users()
    day_rows = load_timesheets_for_date(selected_date)
    salary_rows = build_salary_rows(selected_month, user_id)

    return render_template_string(
        TEMPLATE,
        today=today_local().isoformat(),
        selected_date=selected_date,
        selected_month=selected_month,
        selected_user_id=selected_user_id,
        users=users,
        day_rows=day_rows,
        salary_rows=salary_rows,
        format_time=format_time,
        format_money=format_money,
        format_work_duration=format_work_duration,
        calculate_work_seconds=calculate_work_seconds,
    )


@app.post("/set-rate")
def set_rate():
    user_id = int(request.form["user_id"])
    rate_cents = parse_money_to_cents(request.form["rate"])
    with db_connection() as conn:
        conn.execute(
            "UPDATE users SET hourly_rate_cents = ? WHERE user_id = ?",
            (rate_cents, user_id),
        )
        conn.commit()
    return redirect(url_for("index"))


@app.post("/add-advance")
def add_advance():
    now = now_local()
    user_id = int(request.form["user_id"])
    amount_cents = parse_money_to_cents(request.form["amount"])
    note = request.form.get("note") or None
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO advances (user_id, date, amount_cents, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                now.date().isoformat(),
                amount_cents,
                note,
                now.replace(microsecond=0).isoformat(sep=" "),
            ),
        )
        conn.commit()
    return redirect(url_for("index"))


@app.post("/update-timesheet")
def update_timesheet():
    timesheet_id = int(request.form["timesheet_id"])
    return_date = request.form.get("return_date") or today_local().isoformat()

    with db_connection() as conn:
        row = conn.execute(
            "SELECT id, date FROM timesheets WHERE id = ?",
            (timesheet_id,),
        ).fetchone()
        if not row:
            return redirect(url_for("index", date=return_date))

        break_minutes = int(request.form.get("break_minutes") or 0)
        if break_minutes < 0:
            break_minutes = 0

        status = request.form.get("status") or STATUS_WORKING
        if status not in {STATUS_WORKING, STATUS_ON_BREAK, STATUS_FINISHED}:
            status = STATUS_WORKING

        conn.execute(
            """
            UPDATE timesheets
            SET in_time = ?, out_time = ?, total_break_seconds = ?, status = ?
            WHERE id = ?
            """,
            (
                parse_time_for_date(row["date"], request.form.get("in_time") or ""),
                parse_time_for_date(row["date"], request.form.get("out_time") or ""),
                break_minutes * 60,
                status,
                timesheet_id,
            ),
        )
        conn.commit()

    return redirect(url_for("index", date=return_date))


@app.get("/export/day.csv")
def export_day_csv():
    selected_date = request.args.get("date") or today_local().isoformat()
    rows = load_timesheets_for_date(selected_date)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "user_id", "employee", "status", "clock_in", "clock_out", "break_minutes", "work_time"])
    for row in rows:
        writer.writerow(
            [
                row["date"],
                row["user_id"],
                row["full_name"],
                row["status"],
                format_time(row["in_time"]),
                format_time(row["out_time"]),
                int(row["total_break_seconds"] or 0) // 60,
                format_work_duration(calculate_work_seconds(row)),
            ]
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=timesheets_{selected_date}.csv"},
    )


@app.get("/export/salary.csv")
def export_salary_csv():
    month = request.args.get("month") or today_local().strftime("%Y-%m")
    raw_user_id = request.args.get("user_id") or ""
    user_id = int(raw_user_id) if raw_user_id.isdigit() else None
    rows = build_salary_rows(month, user_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["month", "user_id", "employee", "hourly_rate", "shifts", "work_time", "gross", "advances", "net"])
    for item in rows:
        writer.writerow(
            [
                month,
                item["user"]["user_id"],
                item["user"]["full_name"],
                format_money(item["user"]["hourly_rate_cents"] or 0),
                item["shifts"],
                format_work_duration(item["work_seconds"]),
                format_money(item["gross_cents"]),
                format_money(item["advance_cents"]),
                format_money(item["net_cents"]),
            ]
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=salary_{month}.csv"},
    )



@app.get("/export/day.xlsx")
def export_day_xlsx():
    selected_date = request.args.get("date") or today_local().isoformat()
    rows = load_timesheets_for_date(selected_date)
    detail_rows = []
    for row in rows:
        work_seconds = calculate_work_seconds(row)
        detail_rows.append([
            row["date"],
            row["user_id"],
            row["full_name"],
            row["status"],
            format_time(row["in_time"]),
            format_time(row["out_time"]),
            int(row["total_break_seconds"] or 0) // 60,
            round(work_seconds / 3600, 2),
            format_work_duration(work_seconds),
        ])
    return make_xlsx_response(
        f"timesheets_{selected_date}.xlsx",
        [("Timesheets", ["date", "user_id", "employee", "status", "clock_in", "clock_out", "break_minutes", "work_hours", "work_time"], detail_rows)],
    )


@app.get("/export/salary.xlsx")
def export_salary_xlsx():
    month = request.args.get("month") or today_local().strftime("%Y-%m")
    raw_user_id = request.args.get("user_id") or ""
    user_id = int(raw_user_id) if raw_user_id.isdigit() else None
    salary_rows = build_salary_rows(month, user_id)
    timesheet_rows = load_timesheets_for_month(month, user_id)
    advance_rows = load_advances_for_month(month, user_id)

    summary = []
    for item in salary_rows:
        summary.append([
            month,
            item["user"]["user_id"],
            item["user"]["full_name"],
            round((item["user"]["hourly_rate_cents"] or 0) / 100, 2),
            item["shifts"],
            round(item["work_seconds"] / 3600, 2),
            format_work_duration(item["work_seconds"]),
            round(item["gross_cents"] / 100, 2),
            round(item["advance_cents"] / 100, 2),
            round(item["net_cents"] / 100, 2),
        ])

    details = []
    for row in timesheet_rows:
        work_seconds = calculate_work_seconds(row)
        details.append([
            row["date"],
            row["user_id"],
            row["full_name"],
            row["status"],
            format_time(row["in_time"]),
            format_time(row["out_time"]),
            int(row["total_break_seconds"] or 0) // 60,
            round(work_seconds / 3600, 2),
            format_work_duration(work_seconds),
        ])

    advances = []
    for row in advance_rows:
        advances.append([
            row["date"],
            row["user_id"],
            row["full_name"],
            round(row["amount_cents"] / 100, 2),
            row["note"] or "",
        ])

    return make_xlsx_response(
        f"salary_{month}.xlsx",
        [
            ("Salary Summary", ["month", "user_id", "employee", "hourly_rate", "shifts", "hours_decimal", "work_time", "gross", "advances", "net"], summary),
            ("Timesheet Details", ["date", "user_id", "employee", "status", "clock_in", "clock_out", "break_minutes", "work_hours", "work_time"], details),
            ("Advances", ["date", "user_id", "employee", "amount", "note"], advances),
        ],
    )

if __name__ == "__main__":
    load_dotenv()
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=False)
