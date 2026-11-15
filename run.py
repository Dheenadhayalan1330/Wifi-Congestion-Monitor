"""
College Wi-Fi Load Monitoring, Congestion Prediction & Bandwidth Recommendation System
Operating Hours: Mon-Fri Working Days (8:30 AM - 6:30 PM), Sat-Sun Non-Working Days (Closed).
Line charts: Day (8:30-18:30), Week (Sunday-Saturday), Month (30-day).
Dedicated Bandwidth Recommendations & Predictive Forecasting View.
No recommendations on Saturday & Sunday (Non-working days).
Excel/CSV export with UTF-8 BOM and direct CSV import.
Single localhost: http://localhost:5000
"""

import os, math, random, datetime, csv, io, sqlite3, json
from flask import Flask, jsonify, request, send_file, g
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Vercel serverless: filesystem is read-only except /tmp
# Local dev: use project directory
IS_VERCEL = os.environ.get('VERCEL', '') == '1'
if IS_VERCEL:
    DB_PATH = '/tmp/wifi.db'
else:
    DB_PATH = os.path.join(BASE_DIR, 'wifi.db')

app = Flask(__name__)
CORS(app)

# ─────────────────────────── DB HELPERS ──────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rv  = cur.fetchall()
    return (rv[0] if rv else None) if one else rv

def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur

# ─────────────────────────── SCHEMA ──────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    zone_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT UNIQUE NOT NULL,
    capacity_users INTEGER NOT NULL,
    capacity_mbps  REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS user_activity (
    record_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date                 TEXT NOT NULL,
    time                 TEXT NOT NULL,
    zone_id              INTEGER NOT NULL REFERENCES zones(zone_id),
    connected_users      INTEGER NOT NULL,
    bandwidth_usage_mbps REAL    NOT NULL,
    data_source          TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS signal_congestion (
    record_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT NOT NULL,
    time                TEXT NOT NULL,
    zone_id             INTEGER NOT NULL REFERENCES zones(zone_id),
    signal_strength_dbm REAL    NOT NULL,
    congestion_level    TEXT    NOT NULL,
    alert_status        TEXT    NOT NULL,
    data_source         TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS bandwidth_recommendations (
    record_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date                 TEXT NOT NULL,
    peak_hour            TEXT NOT NULL,
    current_bandwidth    REAL NOT NULL,
    peak_demand_mbps     REAL NOT NULL,
    required_bandwidth   REAL NOT NULL,
    additional_bandwidth REAL NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

def init_schema():
    db = sqlite3.connect(DB_PATH)
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(bandwidth_recommendations)").fetchall()]
        if cols and 'date' not in cols:
            db.execute("DROP TABLE bandwidth_recommendations")
    except: pass
    db.executescript(SCHEMA)
    db.commit()
    db.close()

def get_setting(key, default=1000.0):
    row = query("SELECT value FROM settings WHERE key=?", (key,), one=True)
    try: return float(row['value']) if row else default
    except: return default

def utilization_state(util):
    if util < 0.70: return 'Low'
    if util < 0.90: return 'Moderate'
    if util < 1.00: return 'High'
    return 'Congested'

def _poisson_cdf(k, lam):
    if lam <= 0: return 1.0
    cumulative, term = 0.0, math.exp(-lam)
    for i in range(int(k) + 1):
        cumulative += term
        if cumulative >= 1.0: return 1.0
        term *= lam / (i + 1)
    return min(cumulative, 1.0)

# ─────────────────────────── COLLEGE TIME & ZONE PROFILES ────────────────────
# Strictly 8:30 AM to 6:30 PM (Working days: Mon-Fri)
# Saturday and Sunday: Non-Working Days (0 users)

ZONES_INFO = [
    ('Library',       200, 400.0),
    ('Computer Labs', 180, 360.0),
    ('Classrooms',    250, 500.0),
    ('Auditorium',    100, 200.0),
    ('Canteen',       150, 300.0),
    ('Hostel Block',  300, 600.0),
]

HOURLY_SLOTS = [
    ("08:30", "08:30 AM"),
    ("09:30", "09:30 AM"),
    ("10:30", "10:30 AM"),
    ("11:30", "11:30 AM"),
    ("12:30", "12:30 PM"),
    ("13:30", "01:30 PM"),
    ("14:30", "02:30 PM"),
    ("15:30", "03:30 PM"),
    ("16:30", "04:30 PM"),
    ("17:30", "05:30 PM"),
    ("18:30", "06:30 PM"),
]

def is_college_weekend(d):
    return d.weekday() in (5, 6) # Sat=5, Sun=6

def get_base_traffic(zone_name, time_str, is_weekend):
    if is_weekend:
        return 0

    if zone_name in ('Library', 'Computer Labs', 'Classrooms'):
        if time_str == "08:30": return 65   # College opening
        if time_str == "09:30": return 135  # Morning lectures
        if time_str == "10:30": return 160  # Peak morning classes
        if time_str == "11:30": return 170  # Mid-day academic load
        if time_str == "12:30": return 95   # Lunch break recess
        if time_str == "13:30": return 155  # Afternoon practicals
        if time_str == "14:30": return 180  # Peak lab work & research
        if time_str == "15:30": return 145  # Seminars & classes
        if time_str == "16:30": return 110  # Evening review & library
        if time_str == "17:30": return 55   # Dispersal
        if time_str == "18:30": return 15   # Campus closing
        return 10
    elif zone_name == 'Canteen':
        if time_str == "08:30": return 25   # Breakfast
        if time_str in ("09:30", "10:30"): return 35
        if time_str == "11:30": return 50
        if time_str == "12:30": return 140  # Lunch peak rush
        if time_str == "13:30": return 90
        if time_str == "14:30": return 40
        if time_str == "15:30": return 30
        if time_str == "16:30": return 75   # Evening tea break
        if time_str in ("17:30", "18:30"): return 20
        return 10
    elif zone_name == 'Auditorium':
        if time_str in ("10:30", "11:30", "14:30", "15:30"): return 70
        return 10
    elif zone_name == 'Hostel Block':
        if time_str == "08:30": return 60
        if "09:30" <= time_str <= "16:30": return 20 # Students in college
        if time_str in ("17:30", "18:30"): return 95 # Students returning
        return 30
    return 10

SIGNAL_RANGE = {
    'Library':       (-65, -53),
    'Computer Labs': (-62, -52),
    'Classrooms':    (-68, -55),
    'Auditorium':    (-73, -56),
    'Canteen':       (-71, -58),
    'Hostel Block':  (-77, -60),
}

DEFAULTS = {
    'total_capacity_mbps':        '1000',
    'target_utilization':         '0.75',
    'avg_mbps_per_user':          '2.0',
    'congestion_threshold_green': '0.7',
    'congestion_threshold_yellow':'0.9',
    'signal_threshold_fair':      '-70',
}

# ─────────────────────────── SEED DATABASE ───────────────────────────────────

def seed_database(days_back=35):
    """Seed college database spanning past 35 days up through this coming Saturday."""
    db = get_db()
    db.executescript("""
        DELETE FROM bandwidth_recommendations;
        DELETE FROM signal_congestion;
        DELETE FROM user_activity;
        DELETE FROM zones;
        DELETE FROM settings;
    """)
    db.commit()

    db.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", list(DEFAULTS.items()))

    zone_ids = {}
    for name, cap_u, cap_m in ZONES_INFO:
        cur = db.execute("INSERT INTO zones(name,capacity_users,capacity_mbps) VALUES(?,?,?)", (name, cap_u, cap_m))
        zone_ids[name] = cur.lastrowid

    today = datetime.date.today()
    days_to_saturday = (5 - today.weekday()) if today.weekday() <= 5 else 6
    end_date = today + datetime.timedelta(days=max(0, days_to_saturday))
    start_date = today - datetime.timedelta(days=days_back)

    total_days = (end_date - start_date).days + 1
    for day_idx in range(total_days):
        day_date = start_date + datetime.timedelta(days=day_idx)
        date_str = day_date.isoformat()
        is_weekend = is_college_weekend(day_date)
        day_rng = random.Random(101 + day_idx * 37)

        ua_rows, sc_rows = [], []
        for time_str, _ in HOURLY_SLOTS:
            for zname, zid in zone_ids.items():
                cap_u = next(c for n,c,_ in ZONES_INFO if n==zname)
                cap_m = next(c for n,_,c in ZONES_INFO if n==zname)
                base = get_base_traffic(zname, time_str, is_weekend)

                if base > 0:
                    noise = day_rng.gauss(0, max(1.0, base * 0.08))
                    users = max(0, min(cap_u, int(round(base + noise))))
                else:
                    users = 0

                bw = round(users * 2.0, 2)
                ua_rows.append((date_str, time_str, zid, users, bw, 'simulated'))

                if is_weekend:
                    sig = -55.0
                    cong = 'Low'
                    alert = 'resolved'
                else:
                    lo, hi = SIGNAL_RANGE.get(zname, (-74,-55))
                    sig = round(day_rng.uniform(lo, hi), 1)
                    util = bw / cap_m if cap_m else 0
                    cong = utilization_state(util)
                    alert = 'active' if (cong == 'Congested' or sig < -70) else 'resolved'

                sc_rows.append((date_str, time_str, zid, sig, cong, alert, 'simulated'))

        db.executemany(
            "INSERT INTO user_activity(date,time,zone_id,connected_users,bandwidth_usage_mbps,data_source) VALUES(?,?,?,?,?,?)",
            ua_rows)
        db.executemany(
            "INSERT INTO signal_congestion(date,time,zone_id,signal_strength_dbm,congestion_level,alert_status,data_source) VALUES(?,?,?,?,?,?,?)",
            sc_rows)

    db.commit()
    _update_recommendation(today.isoformat())

def _update_recommendation(target_date):
    total_cap = get_setting('total_capacity_mbps', 1000)
    tgt_util  = get_setting('target_utilization',  0.75)
    avg_mbps  = get_setting('avg_mbps_per_user',   2.0)

    rows = query("SELECT time, SUM(connected_users) as users FROM user_activity WHERE date=? GROUP BY time ORDER BY users DESC", (target_date,))
    if not rows: return
    peak = rows[0]
    peak_time, peak_users = peak['time'], int(peak['users'])
    est = round(peak_users * avg_mbps, 2)
    req = round(est / tgt_util, 2)
    add = round(max(0.0, req - total_cap), 2)

    execute("DELETE FROM bandwidth_recommendations WHERE date=?", (target_date,))
    execute("""INSERT INTO bandwidth_recommendations
        (date, peak_hour, current_bandwidth, peak_demand_mbps, required_bandwidth, additional_bandwidth, created_at)
        VALUES(?,?,?,?,?,?,?)""",
        (target_date, peak_time, total_cap, est, req, add, datetime.datetime.now().isoformat()))

# ─────────────────────────── ANALYTICS ───────────────────────────────────────

def compute_summary(target_date=None):
    if target_date is None:
        target_date = datetime.date.today().isoformat()
    total_cap = get_setting('total_capacity_mbps', 1000)
    avg_mbps  = get_setting('avg_mbps_per_user',   2.0)
    thresh    = get_setting('signal_threshold_fair', -70)

    hour_rows = query(
        "SELECT time, SUM(connected_users) as users FROM user_activity WHERE date=? GROUP BY time",
        (target_date,))
    hour_map = {r['time']: int(r['users']) for r in hour_rows}
    if hour_map and max(hour_map.values()) > 0:
        peak_time  = max(hour_map, key=hour_map.get)
        peak_users = hour_map[peak_time]
    else:
        peak_time, peak_users = "08:30", 0
    total_users = sum(hour_map.values())
    util_pct    = round((peak_users * avg_mbps) / total_cap * 100, 1) if total_cap else 0

    cong_count = query(
        "SELECT COUNT(*) as c FROM signal_congestion WHERE date=? AND congestion_level='Congested' AND alert_status='active'",
        (target_date,), one=True)['c']
    sig_count = query(
        "SELECT COUNT(*) as c FROM signal_congestion WHERE date=? AND signal_strength_dbm<? AND alert_status='active'",
        (target_date, thresh), one=True)['c']

    sources = {r['data_source'] for r in query("SELECT DISTINCT data_source FROM user_activity WHERE date=?", (target_date,))}
    data_source = 'mixed' if len(sources) > 1 else (sources.pop() if sources else 'simulated')

    cur_d = datetime.date.fromisoformat(target_date)
    is_weekend = is_college_weekend(cur_d)

    return {
        'date':                 target_date,
        'day_name':             cur_d.strftime('%A'),
        'is_weekend':           is_weekend,
        'total_connected_users':total_users,
        'peak_hour':            peak_time,
        'peak_users':           peak_users,
        'utilization_percent':  util_pct,
        'alert_counts':         {'congestion': cong_count, 'low_signal': sig_count},
        'data_source':          data_source,
    }

def compute_recommendation(target_date=None):
    if target_date is None:
        target_date = datetime.date.today().isoformat()
    total_cap = get_setting('total_capacity_mbps', 1000)
    tgt_util  = get_setting('target_utilization',  0.75)
    avg_mbps  = get_setting('avg_mbps_per_user',   2.0)

    rows = query("SELECT time, SUM(connected_users) as users FROM user_activity WHERE date=? GROUP BY time ORDER BY users DESC", (target_date,))
    if not rows or int(rows[0]['users']) == 0:
        return {'current_bandwidth':total_cap,'required_bandwidth':total_cap,
                'additional_bandwidth':0,'recommended_total':total_cap,'peak_users':0,
                'peak_hour':'08:30','estimated_peak_demand':0}
    peak = rows[0]
    peak_users = int(peak['users'])
    est = round(peak_users * avg_mbps, 1)
    req = round(est / tgt_util, 1)
    add = round(max(0.0, req - total_cap), 1)
    return {
        'current_bandwidth':     total_cap,
        'peak_users':            peak_users,
        'peak_hour':             peak['time'],
        'estimated_peak_demand': est,
        'required_bandwidth':    req,
        'additional_bandwidth':  add,
        'recommended_total':     round(total_cap + add, 1),
    }

def compute_markov(target_date=None):
    total_cap = get_setting('total_capacity_mbps', 1000)
    avg_mbps  = get_setting('avg_mbps_per_user',   2.0)
    if target_date is None:
        target_date = datetime.date.today().isoformat()

    cur_d = datetime.date.fromisoformat(target_date)
    if is_college_weekend(cur_d):
        return {
            'is_weekend': True,
            'current_state': 'Closed',
            'next_state': 'Closed',
            'probability': 1.0,
            'forecast_text': 'Weekend / Non-working day. Campus network is idle.',
            'confidence_pct': 100
        }

    rows = query(
        "SELECT time, SUM(connected_users) as users FROM user_activity WHERE date=? GROUP BY time ORDER BY time",
        (target_date,))
    states_order = ['Low','Moderate','High','Congested']
    idx = {s:i for i,s in enumerate(states_order)}
    trans = [[0]*4 for _ in range(4)]
    sequence = []
    for r in rows:
        bw   = int(r['users']) * avg_mbps
        util = bw / total_cap if total_cap else 0
        sequence.append(utilization_state(util))
    for i in range(len(sequence)-1):
        a, b = idx[sequence[i]], idx[sequence[i+1]]
        trans[a][b] += 1
    matrix = []
    for row in trans:
        t = sum(row)
        matrix.append([round(c/t, 3) if t else 0.25 for c in row])
    current = sequence[-1] if sequence else 'Low'
    ci      = idx[current]
    probs   = matrix[ci]
    next_st = states_order[probs.index(max(probs))]

    forecast_descriptions = {
        'Low': 'Traffic expected to remain light. Adequate network speed across all zones.',
        'Moderate': 'Normal academic traffic expected. Core academic zones active.',
        'High': 'High concurrent traffic expected. Approaching zone saturation limits.',
        'Congested': 'Severe congestion predicted! Students will experience lag without bandwidth upgrades.'
    }

    return {
        'is_weekend': False,
        'current_state': current,
        'next_state': next_st,
        'probability': round(max(probs), 2),
        'confidence_pct': int(round(max(probs) * 100)),
        'forecast_text': forecast_descriptions.get(next_st, 'Normal traffic flow.'),
        'states': states_order,
        'transition_matrix': matrix
    }

def compute_poisson_risk(target_date=None):
    if target_date is None:
        target_date = datetime.date.today().isoformat()
    cur_d = datetime.date.fromisoformat(target_date)
    if is_college_weekend(cur_d):
        return []

    zones = query("SELECT zone_id, name, capacity_users FROM zones")
    result = []
    for z in zones:
        hourly = query(
            "SELECT SUM(connected_users) as users FROM user_activity WHERE zone_id=? AND date=? GROUP BY time",
            (z['zone_id'], target_date))
        vals = [int(r['users']) for r in hourly if r['users']]
        lam  = sum(vals)/len(vals) if vals else 0
        risk = round(1.0 - _poisson_cdf(z['capacity_users'], lam), 4)
        result.append({
            'zone': z['name'],
            'capacity': z['capacity_users'],
            'lambda': round(lam, 1),
            'risk_percent': round(risk * 100, 1),
            'risk_level': 'High' if risk > 0.4 else ('Moderate' if risk > 0.1 else 'Low')
        })
    return result

# ─────────────────────────── API ROUTES ──────────────────────────────────────

@app.route('/api/summary')
def api_summary():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    try: return jsonify(compute_summary(target_date))
    except Exception as e: return jsonify({'error':str(e)}), 500

@app.route('/api/usage/hourly')
def api_hourly():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    rows = query(
        "SELECT time, SUM(connected_users) as users FROM user_activity WHERE date=? GROUP BY time",
        (target_date,))
    summ = compute_summary(target_date)
    peak = summ.get('peak_hour')
    hour_map = {r['time']: int(r['users']) for r in rows}

    return jsonify([
        {'hour': time_str,
         'label': label,
         'connected_users': hour_map.get(time_str, 0),
         'is_peak': (time_str == peak and hour_map.get(time_str, 0) > 0)}
        for time_str, label in HOURLY_SLOTS
    ])

@app.route('/api/alerts')
def api_alerts():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    thresh = get_setting('signal_threshold_fair', -70)
    cong = query("""
        SELECT sc.record_id, z.name as zone, sc.time, sc.signal_strength_dbm,
               sc.congestion_level, sc.alert_status, sc.data_source
        FROM signal_congestion sc JOIN zones z ON sc.zone_id=z.zone_id
        WHERE sc.date=? AND sc.congestion_level='Congested' AND sc.alert_status='active' ORDER BY sc.time DESC""", (target_date,))
    low_s = query("""
        SELECT sc.record_id, z.name as zone, sc.time, sc.signal_strength_dbm,
               sc.congestion_level, sc.alert_status, sc.data_source
        FROM signal_congestion sc JOIN zones z ON sc.zone_id=z.zone_id
        WHERE sc.date=? AND sc.signal_strength_dbm<? AND sc.alert_status='active' ORDER BY sc.time DESC""", (target_date, thresh))
    def ser(r): return {k: round(r[k],1) if k=='signal_strength_dbm' else r[k] for k in r.keys()}
    return jsonify({'congestion_alerts': [ser(r) for r in cong],
                    'low_signal_alerts':  [ser(r) for r in low_s]})

@app.route('/api/dates')
def api_dates():
    rows = query("SELECT DISTINCT date FROM user_activity ORDER BY date DESC")
    dates_list = []
    for r in rows:
        d_str = r['date']
        try:
            d_obj = datetime.date.fromisoformat(d_str)
            day_name = d_obj.strftime('%A')
            is_off = is_college_weekend(d_obj)
            dates_list.append({
                'date': d_str,
                'day': day_name,
                'is_weekend': is_off,
                'display': f"{d_str} ({day_name} — {'Weekend / Closed' if is_off else 'Working Day'})"
            })
        except:
            dates_list.append({'date': d_str, 'day': '', 'is_weekend': False, 'display': d_str})
    return jsonify(dates_list)

@app.route('/api/usage/weekly')
def api_weekly():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    try: cur_d = datetime.date.fromisoformat(target_date)
    except: cur_d = datetime.date.today()

    days_from_sunday = (cur_d.weekday() + 1) % 7
    sunday = cur_d - datetime.timedelta(days=days_from_sunday)
    day_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

    results = []
    for i in range(7):
        d = sunday + datetime.timedelta(days=i)
        d_str = d.isoformat()
        is_off = (i in (0, 6))
        row = query("SELECT SUM(connected_users) as total FROM user_activity WHERE date=?", (d_str,), one=True)
        users = int(row['total']) if (row and row['total'] is not None) else 0
        results.append({
            'date': d_str,
            'day': day_names[i],
            'label': f"{day_names[i]} ({'Weekend Off' if is_off else 'Working Day'})",
            'is_weekend': is_off,
            'total_users': users
        })
    return jsonify(results)

@app.route('/api/usage/monthly')
def api_monthly():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    try: cur_d = datetime.date.fromisoformat(target_date)
    except: cur_d = datetime.date.today()

    start_d = cur_d - datetime.timedelta(days=29)
    rows = query("""
        SELECT date, SUM(connected_users) as users FROM user_activity
        WHERE date >= ? AND date <= ? GROUP BY date ORDER BY date""", (start_d.isoformat(), cur_d.isoformat()))
    data_map = {r['date']: int(r['users']) for r in rows}

    results = []
    for i in range(30):
        d = start_d + datetime.timedelta(days=i)
        d_str = d.isoformat()
        is_off = is_college_weekend(d)
        results.append({
            'date': d_str,
            'day_name': d.strftime('%a'),
            'label': f"{d_str} ({d.strftime('%a')})",
            'is_weekend': is_off,
            'total_users': data_map.get(d_str, 0)
        })
    return jsonify(results)

@app.route('/api/prediction')
def api_prediction():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    try:
        markov_data = compute_markov(target_date)
        markov_data['poisson_risks'] = compute_poisson_risk(target_date)
        return jsonify(markov_data)
    except Exception as e: return jsonify({'error':str(e)}), 500

@app.route('/api/recommendation')
def api_recommendation():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    try: return jsonify(compute_recommendation(target_date))
    except Exception as e: return jsonify({'error':str(e)}), 500

# ─────────────────────────── ZONE BANDWIDTH RECOMMENDATIONS ───────────────────

@app.route('/api/recommendations/zones')
def api_recommendations_zones():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    try: cur_d = datetime.date.fromisoformat(target_date)
    except: cur_d = datetime.date.today()

    is_weekend = is_college_weekend(cur_d)

    # STRICT USER REQUIREMENT:
    # "on sunday and saturday there should be no recemmendatoin since its not a working day"
    if is_weekend:
        return jsonify({
            'date': target_date,
            'day_name': cur_d.strftime('%A'),
            'is_weekend': True,
            'message': 'No bandwidth recommendations available: Saturday and Sunday are non-working days. College is closed and student network activity is 0.',
            'campus_gateway': {
                'current_capacity_mbps': 1000.0,
                'peak_demand_mbps': 0.0,
                'required_capacity_mbps': 1000.0,
                'additional_needed_mbps': 0.0,
                'recommended_total_mbps': 1000.0
            },
            'prediction': {
                'forecast_state': 'Closed / Non-Working',
                'confidence_pct': 100,
                'predicted_peak_time': 'None (Closed)',
                'summary': 'College campus is closed on weekends. No traffic is expected and zero bandwidth expansion is required.'
            },
            'zones_needing_increase_count': 0,
            'total_additional_needed_mbps': 0.0,
            'zones': []
        })

    # Working Day: Monday to Friday
    zones = query("SELECT zone_id, name, capacity_users, capacity_mbps FROM zones ORDER BY zone_id")
    total_cap = get_setting('total_capacity_mbps', 1000)
    tgt_util  = get_setting('target_utilization', 0.75) # 75% target headroom
    avg_mbps  = get_setting('avg_mbps_per_user', 2.0)

    recommendations = []
    total_peak_demand = 0
    total_additional_needed = 0

    for z in zones:
        zid = z['zone_id']
        zname = z['name']
        cap_mbps = float(z['capacity_mbps'])
        cap_users = int(z['capacity_users'])

        row = query("""
            SELECT time, connected_users, bandwidth_usage_mbps
            FROM user_activity
            WHERE zone_id=? AND date=?
            ORDER BY connected_users DESC LIMIT 1
        """, (zid, target_date), one=True)

        if row:
            peak_users = int(row['connected_users'])
            peak_time = row['time']
        else:
            peak_users = 0
            peak_time = '08:30'

        peak_demand_mbps = round(peak_users * avg_mbps, 1)
        total_peak_demand += peak_demand_mbps
        current_util = round((peak_demand_mbps / cap_mbps) * 100, 1) if cap_mbps else 0

        # Required capacity with 25% safety headroom (target 75% max utilization)
        required_bw = round(peak_demand_mbps / tgt_util, 1)
        add_needed = round(max(0.0, required_bw - cap_mbps), 1)
        recommended_bw = round(cap_mbps + add_needed, 1)

        total_additional_needed += add_needed

        # Zone-specific plain-English action plan
        if add_needed > 50 or current_util >= 95:
            priority = 'HIGH'
            priority_label = '🚨 Urgent Upgrade Required'
            status_color = '#ef4444'
            action = f"High congestion predicted at {peak_time} ({peak_users} students). Increase bandwidth by +{add_needed} Mbps to provision at least {recommended_bw} Mbps."
        elif add_needed > 0 or current_util >= 80:
            priority = 'MEDIUM'
            priority_label = '⚠️ Moderate Increase Advised'
            status_color = '#f59e0b'
            action = f"Approaching 75% headroom limit during {peak_time} classes. Recommended expansion of +{add_needed} Mbps to maintain smooth throughput."
        elif current_util >= 60:
            priority = 'LOW'
            priority_label = '🟡 Stable / Monitor'
            status_color = '#60a5fa'
            action = f"Current {cap_mbps} Mbps capacity handles the {peak_time} rush ({current_util}% load). Bandwidth increase not urgent."
        else:
            priority = 'OPTIMAL'
            priority_label = '✅ Optimal Headroom'
            status_color = '#10b981'
            action = f"Ample headroom available ({current_util}% peak load). No bandwidth increase needed at this time."

        recommendations.append({
            'zone_id': zid,
            'name': zname,
            'current_capacity_mbps': cap_mbps,
            'capacity_users': cap_users,
            'peak_users': peak_users,
            'peak_time': peak_time,
            'peak_demand_mbps': peak_demand_mbps,
            'current_utilization_pct': current_util,
            'required_bandwidth_mbps': required_bw,
            'additional_bandwidth_needed': add_needed,
            'recommended_bandwidth_mbps': recommended_bw,
            'priority': priority,
            'priority_label': priority_label,
            'status_color': status_color,
            'action': action
        })

    # Sort descending so zones needing the most bandwidth appear first
    recommendations.sort(key=lambda x: (x['additional_bandwidth_needed'], x['current_utilization_pct']), reverse=True)

    campus_req = round(total_peak_demand / tgt_util, 1)
    campus_add = round(max(0.0, campus_req - total_cap), 1)
    campus_rec = round(total_cap + campus_add, 1)

    # Predictive analysis for this working day
    markov_info = compute_markov(target_date)
    top_deficit_zone = recommendations[0] if recommendations else None
    
    if top_deficit_zone and top_deficit_zone['additional_bandwidth_needed'] > 0:
        pred_summary = f"Predictive model forecasts high traffic at {top_deficit_zone['peak_time']}. {top_deficit_zone['name']} is expected to reach {top_deficit_zone['current_utilization_pct']}% capacity (+{top_deficit_zone['additional_bandwidth_needed']} Mbps deficit). Upgrading now will prevent network bottlenecks."
    else:
        pred_summary = f"Predictive model forecasts stable network traffic throughout the day. All campus zones are expected to operate safely within their capacity limits."

    return jsonify({
        'date': target_date,
        'day_name': cur_d.strftime('%A'),
        'is_weekend': False,
        'message': 'Working day: active bandwidth recommendations and predictive congestion forecast computed.',
        'campus_gateway': {
            'current_capacity_mbps': total_cap,
            'peak_demand_mbps': round(total_peak_demand, 1),
            'required_capacity_mbps': campus_req,
            'additional_needed_mbps': campus_add,
            'recommended_total_mbps': campus_rec
        },
        'prediction': {
            'forecast_state': markov_info.get('next_state', 'Moderate'),
            'confidence_pct': markov_info.get('confidence_pct', 85),
            'predicted_peak_time': top_deficit_zone['peak_time'] if top_deficit_zone else '02:30 PM',
            'summary': pred_summary
        },
        'zones_needing_increase_count': len([z for z in recommendations if z['additional_bandwidth_needed'] > 0]),
        'total_additional_needed_mbps': round(total_additional_needed, 1),
        'zones': recommendations
    })

@app.route('/api/export/recommendations')
def api_export_recommendations():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    res_data = api_recommendations_zones().get_json()

    out = io.StringIO()
    wr  = csv.writer(out)
    wr.writerow(['Wi-Fi Zone', 'Current Bandwidth (Mbps)', 'Peak Demand (Mbps)', 'Peak Time Slot', 'Peak Concurrency (Students)', 'Peak Utilization (%)', 'Recommended Bandwidth (Mbps)', 'Additional Bandwidth Needed (+Mbps)', 'Priority Status', 'Recommended Network Action'])

    if res_data.get('is_weekend'):
        wr.writerow(['Weekend Notice', '-', '-', '-', '-', '-', '-', '0', 'Closed', 'No recommendations: Saturday and Sunday are non-working days.'])
    else:
        for z in res_data.get('zones', []):
            wr.writerow([
                z['name'],
                z['current_capacity_mbps'],
                z['peak_demand_mbps'],
                z['peak_time'],
                z['peak_users'],
                f"{z['current_utilization_pct']}%",
                z['recommended_bandwidth_mbps'],
                f"+{z['additional_bandwidth_needed']}" if z['additional_bandwidth_needed'] > 0 else '0',
                z['priority_label'],
                z['action']
            ])

    csv_bytes = out.getvalue().encode('utf-8-sig')
    return send_file(io.BytesIO(csv_bytes), mimetype='text/csv',
                     as_attachment=True, download_name=f'College_WiFi_Bandwidth_Upgrade_Plan_{target_date}.csv')

# ─────────────────────────── RECORDS API ─────────────────────────────────────

@app.route('/api/records/usage')
def api_records_usage():
    page    = max(1, int(request.args.get('page', 1)))
    size    = max(1, int(request.args.get('size', 15)))
    search  = request.args.get('search', '').strip()
    zone_f  = request.args.get('zone', '').strip()
    date_f  = request.args.get('date', '').strip()
    src_f   = request.args.get('data_source', '').strip()
    sort_by = request.args.get('sort', 'record_id')
    order   = 'DESC' if request.args.get('order','asc')=='desc' else 'ASC'
    allowed_sorts = {'record_id','date','time','connected_users','bandwidth_usage_mbps','data_source'}
    if sort_by not in allowed_sorts: sort_by = 'record_id'

    where, params = ["1=1"], []
    if zone_f:  where.append("z.name LIKE ?"); params.append(f'%{zone_f}%')
    if date_f:  where.append("ua.date LIKE ?"); params.append(f'%{date_f}%')
    if src_f:   where.append("ua.data_source=?"); params.append(src_f)
    if search:
        where.append("(z.name LIKE ? OR ua.date LIKE ? OR ua.time LIKE ? OR ua.data_source LIKE ? OR CAST(ua.record_id AS TEXT) LIKE ? OR CAST(ua.connected_users AS TEXT) LIKE ? OR CAST(ua.bandwidth_usage_mbps AS TEXT) LIKE ?)")
        params += [f'%{search}%'] * 7
    w = ' AND '.join(where)

    base = f"""FROM user_activity ua JOIN zones z ON ua.zone_id=z.zone_id WHERE {w}"""
    total = query(f"SELECT COUNT(*) as c {base}", params, one=True)['c']
    rows  = query(f"SELECT ua.*,z.name as zone_name {base} ORDER BY ua.{sort_by} {order} LIMIT ? OFFSET ?",
                  params + [size, (page-1)*size])
    data  = [{'record_id':r['record_id'],'date':r['date'],'time':r['time'],'zone':r['zone_name'],
               'connected_users':r['connected_users'],'bandwidth_usage_mbps':r['bandwidth_usage_mbps'],
               'data_source':r['data_source']} for r in rows]
    return jsonify({'total':total,'page':page,'size':size,'pages':math.ceil(total/size),'data':data})

@app.route('/api/records/signal')
def api_records_signal():
    page    = max(1, int(request.args.get('page', 1)))
    size    = max(1, int(request.args.get('size', 15)))
    search  = request.args.get('search', '').strip()
    zone_f  = request.args.get('zone', '').strip()
    date_f  = request.args.get('date', '').strip()
    src_f   = request.args.get('data_source', '').strip()
    level_f = request.args.get('level', '').strip()
    sort_by = request.args.get('sort', 'record_id')
    order   = 'DESC' if request.args.get('order','asc')=='desc' else 'ASC'
    allowed_sorts = {'record_id','date','time','signal_strength_dbm','congestion_level','alert_status','data_source'}
    if sort_by not in allowed_sorts: sort_by = 'record_id'

    where, params = ["1=1"], []
    if zone_f:  where.append("z.name LIKE ?"); params.append(f'%{zone_f}%')
    if date_f:  where.append("sc.date LIKE ?"); params.append(f'%{date_f}%')
    if src_f:   where.append("sc.data_source=?"); params.append(src_f)
    if level_f: where.append("sc.congestion_level=?"); params.append(level_f)
    if search:
        where.append("(z.name LIKE ? OR sc.date LIKE ? OR sc.time LIKE ? OR sc.congestion_level LIKE ? OR sc.alert_status LIKE ? OR sc.data_source LIKE ? OR CAST(sc.record_id AS TEXT) LIKE ? OR CAST(sc.signal_strength_dbm AS TEXT) LIKE ?)")
        params += [f'%{search}%'] * 8
    w = ' AND '.join(where)

    base = f"""FROM signal_congestion sc JOIN zones z ON sc.zone_id=z.zone_id WHERE {w}"""
    total = query(f"SELECT COUNT(*) as c {base}", params, one=True)['c']
    rows  = query(f"SELECT sc.*,z.name as zone_name {base} ORDER BY sc.{sort_by} {order} LIMIT ? OFFSET ?",
                  params + [size, (page-1)*size])
    data  = [{'record_id':r['record_id'],'date':r['date'],'time':r['time'],'zone':r['zone_name'],
               'signal_strength_dbm':round(r['signal_strength_dbm'],1),
               'congestion_level':r['congestion_level'],'alert_status':r['alert_status'],
               'data_source':r['data_source']} for r in rows]
    return jsonify({'total':total,'page':page,'size':size,'pages':math.ceil(total/size),'data':data})

# ─────────────────────────── EXCEL & CSV EXPORTS ────────────────────────────

@app.route('/api/export/usage')
def api_export_usage():
    zone_f = request.args.get('zone','').strip()
    date_f = request.args.get('date','').strip()
    src_f  = request.args.get('data_source','').strip()
    search = request.args.get('search','').strip()

    where, params = ["1=1"], []
    if zone_f: where.append("z.name LIKE ?"); params.append(f'%{zone_f}%')
    if date_f: where.append("ua.date LIKE ?"); params.append(f'%{date_f}%')
    if src_f:  where.append("ua.data_source=?"); params.append(src_f)
    if search:
        where.append("(z.name LIKE ? OR ua.date LIKE ? OR ua.time LIKE ? OR ua.data_source LIKE ?)")
        params += [f'%{search}%'] * 4
    w = ' AND '.join(where)

    rows = query(f"SELECT ua.*,z.name as zone_name FROM user_activity ua JOIN zones z ON ua.zone_id=z.zone_id WHERE {w} ORDER BY ua.date DESC, ua.time ASC", params)
    out = io.StringIO()
    wr  = csv.writer(out)
    wr.writerow(['Record ID', 'Date', 'Day of Week', 'College Time Slot', 'Wi-Fi Zone', 'Connected Students', 'Bandwidth Consumption (Mbps)', 'Data Source'])
    for r in rows:
        try: d_name = datetime.date.fromisoformat(r['date']).strftime('%A')
        except: d_name = ''
        wr.writerow([r['record_id'], r['date'], d_name, r['time'], r['zone_name'], r['connected_users'], r['bandwidth_usage_mbps'], r['data_source']])

    csv_bytes = out.getvalue().encode('utf-8-sig')
    return send_file(io.BytesIO(csv_bytes), mimetype='text/csv',
                     as_attachment=True, download_name='College_WiFi_Student_Usage_Data.csv')

@app.route('/api/export/signal')
def api_export_signal():
    zone_f  = request.args.get('zone','').strip()
    date_f  = request.args.get('date','').strip()
    src_f   = request.args.get('data_source','').strip()
    level_f = request.args.get('level','').strip()
    search  = request.args.get('search','').strip()

    where, params = ["1=1"], []
    if zone_f:  where.append("z.name LIKE ?"); params.append(f'%{zone_f}%')
    if date_f:  where.append("sc.date LIKE ?"); params.append(f'%{date_f}%')
    if src_f:   where.append("sc.data_source=?"); params.append(src_f)
    if level_f: where.append("sc.congestion_level=?"); params.append(level_f)
    if search:
        where.append("(z.name LIKE ? OR sc.date LIKE ? OR sc.time LIKE ? OR sc.congestion_level LIKE ? OR sc.alert_status LIKE ? OR sc.data_source LIKE ?)")
        params += [f'%{search}%'] * 6
    w = ' AND '.join(where)

    rows = query(f"SELECT sc.*,z.name as zone_name FROM signal_congestion sc JOIN zones z ON sc.zone_id=z.zone_id WHERE {w} ORDER BY sc.date DESC, sc.time ASC", params)
    out = io.StringIO()
    wr  = csv.writer(out)
    wr.writerow(['Record ID', 'Date', 'Day of Week', 'Time Slot', 'Wi-Fi Zone', 'Signal Strength (dBm)', 'Congestion Level', 'Alert Status', 'Data Source'])
    for r in rows:
        try: d_name = datetime.date.fromisoformat(r['date']).strftime('%A')
        except: d_name = ''
        wr.writerow([r['record_id'], r['date'], d_name, r['time'], r['zone_name'], round(r['signal_strength_dbm'],1), r['congestion_level'], r['alert_status'], r['data_source']])

    csv_bytes = out.getvalue().encode('utf-8-sig')
    return send_file(io.BytesIO(csv_bytes), mimetype='text/csv',
                     as_attachment=True, download_name='College_WiFi_Signal_and_Congestion_Logs.csv')

    rows = query(f"SELECT sc.*,z.name as zone_name FROM signal_congestion sc JOIN zones z ON sc.zone_id=z.zone_id WHERE {w} ORDER BY sc.date DESC, sc.time ASC", params)
    out = io.StringIO()
    wr  = csv.writer(out)
    wr.writerow(['Record ID', 'Date', 'Day of Week', 'Time Slot', 'Wi-Fi Zone', 'Signal Strength (dBm)', 'Congestion Level', 'Alert Status', 'Data Source'])
    for r in rows:
        try: d_name = datetime.date.fromisoformat(r['date']).strftime('%A')
        except: d_name = ''
        wr.writerow([r['record_id'], r['date'], d_name, r['time'], r['zone_name'], round(r['signal_strength_dbm'],1), r['congestion_level'], r['alert_status'], r['data_source']])

    csv_bytes = out.getvalue().encode('utf-8-sig')
    return send_file(io.BytesIO(csv_bytes), mimetype='text/csv',
                     as_attachment=True, download_name='College_WiFi_Signal_and_Congestion_Logs.csv')

@app.route('/api/export/template')
def api_export_template():
    out = io.StringIO()
    wr  = csv.writer(out)
    wr.writerow(['Date', 'Time', 'Wi-Fi Zone', 'Connected Users', 'Bandwidth Consumption (Mbps)'])
    wr.writerow(['2026-11-16', '08:30', 'Library', '45', '90.0'])
    wr.writerow(['2026-11-16', '09:30', 'Computer Labs', '130', '260.0'])
    wr.writerow(['2026-11-16', '11:30', 'Classrooms', '210', '420.0'])
    wr.writerow(['2026-11-16', '12:30', 'Canteen', '140', '280.0'])
    wr.writerow(['2026-11-16', '14:30', 'Auditorium', '90', '180.0'])
    csv_bytes = out.getvalue().encode('utf-8-sig')
    return send_file(io.BytesIO(csv_bytes), mimetype='text/csv',
                     as_attachment=True, download_name='College_WiFi_Import_Template.csv')

# ─────────────────────────── SIMULATION & IMPORTS ────────────────────────────

@app.route('/api/simulate-peak', methods=['POST'])
def api_simulate_peak():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    target_slot = "14:30"

    zones_q = query("SELECT zone_id, name, capacity_users, capacity_mbps FROM zones")
    total_added = 0
    for z in zones_q:
        ua = query("SELECT record_id, connected_users FROM user_activity WHERE date=? AND time=? AND zone_id=?",
                   (target_date, target_slot, z['zone_id']), one=True)
        new_users = int(z['capacity_users'] * 0.92)
        new_bw    = round(new_users * 2.0, 2)
        new_util  = new_bw / z['capacity_mbps']
        new_cong  = utilization_state(new_util)

        if ua:
            execute("UPDATE user_activity SET connected_users=?, bandwidth_usage_mbps=?, data_source='simulated' WHERE record_id=?",
                    (new_users, new_bw, ua['record_id']))
        else:
            execute("INSERT INTO user_activity(date,time,zone_id,connected_users,bandwidth_usage_mbps,data_source) VALUES(?,?,?,?,?,?)",
                    (target_date, target_slot, z['zone_id'], new_users, new_bw, 'simulated'))

        sc = query("SELECT record_id, signal_strength_dbm FROM signal_congestion WHERE date=? AND time=? AND zone_id=?",
                   (target_date, target_slot, z['zone_id']), one=True)
        new_alert = 'active'
        if sc:
            execute("UPDATE signal_congestion SET congestion_level=?, alert_status=?, data_source='simulated' WHERE record_id=?",
                    (new_cong, new_alert, sc['record_id']))
        else:
            execute("INSERT INTO signal_congestion(date,time,zone_id,signal_strength_dbm,congestion_level,alert_status,data_source) VALUES(?,?,?,?,?,?,?)",
                    (target_date, target_slot, z['zone_id'], -73.5, new_cong, new_alert, 'simulated'))

        total_added += new_users

    _update_recommendation(target_date)
    return jsonify({'status':'simulated','hour':'02:30 PM (14:30)','date':target_date,'simulated_users':total_added})

@app.route('/api/simulate-reset', methods=['POST'])
def api_simulate_reset():
    target_date = request.args.get('date', datetime.date.today().isoformat())
    cur_d = datetime.date.fromisoformat(target_date)
    is_weekend = is_college_weekend(cur_d)

    for time_str, _ in HOURLY_SLOTS:
        for zname, cap_u, cap_m in ZONES_INFO:
            zid = query("SELECT zone_id FROM zones WHERE name=?", (zname,), one=True)['zone_id']
            base = get_base_traffic(zname, time_str, is_weekend)
            users = base
            bw = round(users * 2.0, 2)
            execute("UPDATE user_activity SET connected_users=?, bandwidth_usage_mbps=?, data_source='simulated' WHERE date=? AND time=? AND zone_id=?",
                    (users, bw, target_date, time_str, zid))
            util = bw / cap_m if cap_m else 0
            cong = utilization_state(util)
            alert = 'active' if cong == 'Congested' else 'resolved'
            execute("UPDATE signal_congestion SET congestion_level=?, alert_status=?, data_source='simulated' WHERE date=? AND time=? AND zone_id=?",
                    (cong, alert, target_date, time_str, zid))

    _update_recommendation(target_date)
    return jsonify({'status':'reset','date':target_date})

@app.route('/api/import', methods=['POST'])
def api_import():
    if 'file' not in request.files: return jsonify({'error':'No file uploaded'}), 400
    f = request.files['file']
    table = request.args.get('table','usage')
    today = datetime.date.today().isoformat()
    try:
        content = f.read().decode('utf-8-sig', errors='replace')
        reader = csv.DictReader(io.StringIO(content))
        count = 0
        for raw_row in reader:
            row = {k.strip().lower(): v.strip() for k, v in raw_row.items() if k}
            zname = row.get('wi-fi zone') or row.get('zone') or row.get('wifi zone') or row.get('zone name') or ''
            zrow = query("SELECT zone_id FROM zones WHERE LOWER(name)=LOWER(?)", (zname.strip(),), one=True)
            if not zrow: continue
            zid = zrow['zone_id']
            rdate = row.get('date') or today
            rtime = row.get('college time slot') or row.get('time slot') or row.get('time') or '08:30'

            if table == 'usage':
                u_val = row.get('connected students') or row.get('connected users') or row.get('users') or 0
                b_val = row.get('bandwidth consumption (mbps)') or row.get('bandwidth (mbps)') or row.get('bandwidth') or (float(u_val) * 2.0)
                execute("INSERT INTO user_activity(date,time,zone_id,connected_users,bandwidth_usage_mbps,data_source) VALUES(?,?,?,?,?,?)",
                        (rdate, rtime, zid, int(u_val), float(b_val), 'actual'))
                count += 1
            elif table == 'signal':
                s_val = row.get('signal strength (dbm)') or row.get('signal') or -64.0
                c_val = row.get('congestion level') or 'Low'
                a_val = row.get('alert status') or 'resolved'
                execute("INSERT INTO signal_congestion(date,time,zone_id,signal_strength_dbm,congestion_level,alert_status,data_source) VALUES(?,?,?,?,?,?,?)",
                        (rdate, rtime, zid, float(s_val), c_val, a_val, 'actual'))
                count += 1

        _update_recommendation(today)
        return jsonify({'status':'imported','rows':count})
    except Exception as e: return jsonify({'error':str(e)}), 400

# ─────────────────────────── FRONTEND APPLICATION ────────────────────────────

@app.route('/')
@app.route('/api/index')
@app.route('/analysis')
@app.route('/recommendations')
@app.route('/db')
def render_app():
    zones = [dict(r) for r in query("SELECT name FROM zones ORDER BY name")]
    zone_opts = ''.join(f'<option value="{z["name"]}">{z["name"]}</option>' for z in zones)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>College Wi-Fi Network & Bandwidth Recommendations</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg: #090e17;
      --sidebar-bg: #0d1527;
      --surface: #111a2e;
      --surface-card: #162238;
      --surface-hover: #1c2b47;
      --border: #223455;
      --primary: #3b82f6;
      --primary-glow: rgba(59, 130, 246, 0.25);
      --primary-light: #60a5fa;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Plus Jakarta Sans', sans-serif;
      background: var(--bg);
      color: var(--text-main);
      display: flex;
      min-height: 100vh;
      overflow-x: hidden;
    }}

    .sidebar {{
      width: 270px;
      background: var(--sidebar-bg);
      border-right: 1px solid var(--border);
      padding: 24px 16px;
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      position: sticky;
      top: 0;
      height: 100vh;
    }}

    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 0 8px 22px 8px;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      margin-bottom: 20px;
    }}

    .brand-icon {{
      width: 40px;
      height: 40px;
      background: linear-gradient(135deg, #2563eb, #6366f1);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      box-shadow: 0 4px 14px var(--primary-glow);
    }}

    .brand-title h2 {{
      font-size: 1.05rem;
      font-weight: 800;
      color: #fff;
    }}

    .brand-title p {{
      font-size: 0.72rem;
      color: var(--text-dim);
    }}

    .nav-link {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 14px;
      border-radius: 10px;
      color: var(--text-muted);
      text-decoration: none;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      margin-bottom: 6px;
      transition: all 0.2s;
    }}

    .nav-link:hover {{
      background: var(--surface);
      color: #fff;
    }}

    .nav-link.active {{
      background: linear-gradient(90deg, rgba(37, 99, 235, 0.22) 0%, rgba(37, 99, 235, 0.05) 100%);
      color: var(--primary-light);
      border: 1px solid rgba(96, 165, 250, 0.35);
    }}

    .sidebar-footer {{
      margin-top: auto;
      padding-top: 16px;
      border-top: 1px solid rgba(255,255,255,0.06);
      font-size: 0.76rem;
      color: var(--text-muted);
    }}

    .main-wrap {{
      flex: 1;
      padding: 28px 36px;
      overflow-y: auto;
      max-width: 1450px;
    }}

    .top-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 22px;
      flex-wrap: wrap;
      gap: 16px;
    }}

    .page-title h1 {{
      font-size: 1.6rem;
      font-weight: 800;
      letter-spacing: -0.02em;
    }}

    .page-title p {{
      font-size: 0.86rem;
      color: var(--text-muted);
      margin-top: 3px;
    }}

    .btn {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 9px 18px;
      border-radius: 9px;
      font-size: 0.88rem;
      font-weight: 600;
      border: none;
      cursor: pointer;
      transition: all 0.15s;
      text-decoration: none;
    }}
    .btn:active {{ transform: scale(0.98); }}
    .btn-primary {{
      background: linear-gradient(135deg, #2563eb, #1d4ed8);
      color: #fff;
      box-shadow: 0 4px 14px var(--primary-glow);
    }}
    .btn-primary:hover {{ filter: brightness(1.1); }}
    .btn-secondary {{
      background: var(--surface);
      color: var(--text-main);
      border: 1px solid var(--border);
    }}
    .btn-secondary:hover {{
      background: var(--surface-hover);
    }}
    .btn-success {{
      background: linear-gradient(135deg, #059669, #10b981);
      color: #fff;
    }}

    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 12px;
      border-radius: 20px;
      font-size: 0.76rem;
      font-weight: 700;
    }}
    .badge-sim {{ background: rgba(59, 130, 246, 0.15); color: #93c5fd; border: 1px solid rgba(59, 130, 246, 0.3); }}
    .badge-act {{ background: rgba(16, 185, 129, 0.15); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.3); }}
    .badge-green {{ background: rgba(16, 185, 129, 0.15); color: #34d399; }}
    .badge-yellow {{ background: rgba(245, 158, 11, 0.15); color: #fbbf24; }}
    .badge-red {{ background: rgba(239, 68, 68, 0.15); color: #f87171; }}
    .badge-neutral {{ background: rgba(255,255,255,0.06); color: #cbd5e1; }}

    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin-bottom: 22px;
    }}

    .card {{
      background: var(--surface-card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px 20px;
      box-shadow: 0 4px 15px rgba(0,0,0,0.2);
    }}

    .card-label {{
      font-size: 0.74rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      font-weight: 700;
      margin-bottom: 8px;
    }}

    .card-val {{
      font-size: 2rem;
      font-weight: 800;
      color: #fff;
      line-height: 1;
      margin-bottom: 6px;
    }}

    .card-desc {{
      font-size: 0.78rem;
      color: var(--text-dim);
    }}

    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 22px;
      margin-bottom: 20px;
    }}

    .panel-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      flex-wrap: wrap;
      gap: 12px;
    }}

    .panel-title {{
      font-size: 1.05rem;
      font-weight: 700;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    .grid-2 {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
      margin-bottom: 20px;
    }}

    .grid-3 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 18px;
      margin-bottom: 24px;
    }}

    @media (max-width: 992px) {{
      .grid-2, .grid-3 {{ grid-template-columns: 1fr; }}
      body {{ flex-direction: column; }}
      .sidebar {{ width: 100%; height: auto; position: relative; }}
    }}

    .alert-card {{
      background: rgba(255,255,255,0.02);
      border-radius: 10px;
      padding: 12px 14px;
      margin-bottom: 8px;
      border: 1px solid rgba(255,255,255,0.05);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .alert-card.cong {{ border-left: 4px solid var(--danger); }}
    .alert-card.sig {{ border-left: 4px solid var(--warning); }}

    .table-responsive {{
      overflow-x: auto;
      border-radius: 10px;
      border: 1px solid var(--border);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.86rem;
      text-align: left;
    }}
    th {{
      background: #0d1527;
      padding: 11px 14px;
      font-weight: 700;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
      cursor: pointer;
      user-select: none;
      white-space: nowrap;
    }}
    th:hover {{ color: var(--primary-light); }}
    td {{
      padding: 11px 14px;
      border-bottom: 1px solid rgba(255,255,255,0.04);
      color: #cbd5e1;
    }}
    tr:hover td {{ background: rgba(255,255,255,0.02); }}

    .input-field {{
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 12px;
      color: #fff;
      font-size: 0.84rem;
      outline: none;
    }}
    .input-field:focus {{ border-color: var(--primary); }}
    select.input-field option {{ background: var(--surface); color: #fff; }}

    .view-section {{ display: none; }}
    .view-section.active {{ display: block; }}

    .sub-tabs {{
      display: flex;
      gap: 4px;
      background: var(--bg);
      padding: 4px;
      border-radius: 8px;
      border: 1px solid var(--border);
      width: fit-content;
    }}
    .sub-tab {{
      padding: 7px 16px;
      border-radius: 6px;
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--text-muted);
      cursor: pointer;
      border: none;
      background: transparent;
      transition: all 0.15s;
    }}
    .sub-tab.active {{
      background: var(--surface-card);
      color: #fff;
    }}

    .status-banner {{
      padding: 12px 16px;
      border-radius: 10px;
      margin-bottom: 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 0.88rem;
    }}
    .status-banner.working {{
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid rgba(16, 185, 129, 0.25);
      color: #6ee7b7;
    }}
    .status-banner.weekend {{
      background: rgba(245, 158, 11, 0.1);
      border: 1px solid rgba(245, 158, 11, 0.25);
      color: #fbbf24;
    }}

    /* Recommendation Specific Styles */
    .rec-zone-card {{
      background: var(--surface-card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      transition: transform 0.2s, border-color 0.2s;
    }}
    .rec-zone-card:hover {{
      transform: translateY(-2px);
      border-color: rgba(96, 165, 250, 0.4);
    }}
    .rec-zone-card.urgent {{
      border-left: 5px solid var(--danger);
    }}
    .rec-zone-card.moderate {{
      border-left: 5px solid var(--warning);
    }}
    .rec-zone-card.optimal {{
      border-left: 5px solid var(--success);
    }}

    .progress-bar-bg {{
      background: rgba(255,255,255,0.06);
      border-radius: 6px;
      height: 8px;
      width: 100%;
      overflow: hidden;
      margin-top: 6px;
    }}
    .progress-bar-fill {{
      height: 100%;
      border-radius: 6px;
      transition: width 0.4s ease;
    }}

    .highlight-box {{
      background: linear-gradient(135deg, rgba(37, 99, 235, 0.15) 0%, rgba(99, 102, 241, 0.08) 100%);
      border: 1px solid rgba(96, 165, 250, 0.3);
      border-radius: 12px;
      padding: 18px 22px;
      margin-bottom: 24px;
    }}

    .weekend-card {{
      background: rgba(245, 158, 11, 0.08);
      border: 1px solid rgba(245, 158, 11, 0.25);
      border-radius: 14px;
      padding: 32px 24px;
      text-align: center;
      margin: 20px 0;
    }}
  </style>
</head>
<body>

  <!-- ──────────────── SIDEBAR NAVIGATION ──────────────── -->
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-icon">📡</div>
      <div class="brand-title">
        <h2>College Wi-Fi</h2>
        <p>Sentinel Network Monitor</p>
      </div>
    </div>

    <div style="font-size:0.68rem; text-transform:uppercase; letter-spacing:0.06em; color:var(--text-dim); padding:0 8px 8px; font-weight:700;">Navigation</div>
    <div class="nav-link active" onclick="switchView('dashboard')">
      <span>📊</span> <span>Monitoring Dashboard</span>
    </div>
    <div class="nav-link" onclick="switchView('analysis')">
      <span>📋</span> <span>Wi-Fi Data Analysis</span>
    </div>
    <div class="nav-link" onclick="switchView('recommendations')">
      <span>💡</span> <span>Bandwidth & Prediction</span>
    </div>

    <div class="sidebar-footer">
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="width:8px; height:8px; background:var(--success); border-radius:50%; display:inline-block;"></span>
        <span><strong>8:30 AM – 6:30 PM</strong></span>
      </div>
      <div style="color:var(--text-dim); font-size:0.72rem; margin-top:4px;">
        Mon–Fri Working Day<br>Sat & Sun Non-Working (Closed)
      </div>
    </div>
  </aside>

  <main class="main-wrap">

    <!-- ──────────────── VIEW 1: DASHBOARD ──────────────── -->
    <section id="view-dashboard" class="view-section active">
      <div class="top-bar">
        <div class="page-title">
          <h1>College Wi-Fi Load Monitoring Dashboard</h1>
          <p>Real-time campus concurrency, 8:30 AM – 6:30 PM load profile, alerts & quick overview</p>
        </div>
        <div style="display:flex; gap:10px; align-items:center;">
          <span id="dashBadge" class="badge badge-sim">● SIMULATED</span>
          <button class="btn btn-primary" onclick="simulatePeakLoad(this)">
            <span>⚡</span> <span>Simulate Peak Rush (02:30 PM)</span>
          </button>
          <button class="btn btn-secondary" onclick="simulateReset(this)">
            <span>🔄</span> <span>Reset Normal</span>
          </button>
        </div>
      </div>

      <!-- Schedule & Day Banner -->
      <div id="dayStatusBanner" class="status-banner working">
        <div id="dayStatusText">📅 College Operating Hours: Monday to Friday (8:30 AM to 6:30 PM)</div>
        <div id="quickDateNotice" style="font-size:0.78rem; color:var(--text-dim);">Pick any date below to inspect historical graphs</div>
      </div>

      <!-- KPI Summary Cards -->
      <div class="cards-grid">
        <div class="card">
          <div class="card-label">Total Connected Students</div>
          <div class="card-val" id="valTotalUsers">—</div>
          <div class="card-desc" id="valDateDesc">Across all 6 campus zones</div>
        </div>

        <div class="card">
          <div class="card-label">Busiest Peak Hour</div>
          <div class="card-val" id="valPeakHour">—</div>
          <div class="card-desc" id="valPeakUsersDesc">— students at peak concurrency</div>
        </div>

        <div class="card">
          <div class="card-label">Peak Bandwidth Load</div>
          <div class="card-val" id="valUtil">—</div>
          <div class="card-desc" id="valUtilStatus">Campus capacity (1,000 Mbps)</div>
        </div>

        <div class="card">
          <div class="card-label">Active Network Alerts</div>
          <div class="card-val" id="valAlerts">—</div>
          <div class="card-desc" id="valAlertsSub">0 Congestion · 0 Weak Signal</div>
        </div>
      </div>

      <!-- Interactive Line Graph Panel: Day / Week / Month -->
      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <span id="chartIcon">📈</span>
            <span id="chartHeading">Hourly Campus Load Profile (8:30 AM – 6:30 PM)</span>
          </div>

          <div style="display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
            <div style="display:flex; align-items:center; gap:6px;">
              <span style="font-size:0.8rem; color:var(--text-muted); font-weight:600;">Date:</span>
              <select id="dashDatePicker" class="input-field" style="padding:6px 12px; font-weight:600;" onchange="onDateChange()">
              </select>
            </div>

            <div class="sub-tabs">
              <button class="sub-tab active" id="btnRangeDay" onclick="setChartRange('day')">📈 Day (8:30-18:30)</button>
              <button class="sub-tab" id="btnRangeWeek" onclick="setChartRange('week')">📅 Week (Sunday–Saturday)</button>
              <button class="sub-tab" id="btnRangeMonth" onclick="setChartRange('month')">📊 Month (30 Days)</button>
            </div>
          </div>
        </div>

        <div style="position: relative; height: 330px; width: 100%;">
          <canvas id="mainChart"></canvas>
        </div>
      </div>

      <!-- Alerts Grid -->
      <div class="grid-2">
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title" style="color: #f87171;">
              <span>🔴</span> <span>Congestion Alerts (Capacity Exceeded)</span>
            </div>
          </div>
          <div id="congAlertsList"><p style="color:var(--text-dim); font-size:0.86rem;">Checking zone capacities...</p></div>
        </div>

        <div class="panel">
          <div class="panel-header">
            <div class="panel-title" style="color: #fbbf24;">
              <span>🟡</span> <span>Weak Signal Telemetry (&lt; -70 dBm)</span>
            </div>
          </div>
          <div id="sigAlertsList"><p style="color:var(--text-dim); font-size:0.86rem;">Monitoring AP signal strength...</p></div>
        </div>
      </div>
    </section>

    <!-- ──────────────── VIEW 2: DATA ANALYSIS ──────────────── -->
    <section id="view-analysis" class="view-section">
      <div class="top-bar">
        <div class="page-title">
          <h1>Wi-Fi Data Analysis & Historical Logs</h1>
          <p>Search, filter, inspect and export Excel-ready CSV telemetry for college access points</p>
        </div>
        <div style="display:flex; gap:10px; align-items:center;">
          <label class="btn btn-secondary" style="cursor:pointer;">
            📂 Import Excel / CSV
            <input type="file" id="analysisFileInput" accept=".csv" style="display:none;" onchange="handleAnalysisImport()">
          </label>
          <a href="/api/export/template" class="btn btn-secondary">
            <span>📄</span> CSV Template
          </a>
        </div>
      </div>

      <div id="analysisNotification" style="margin-bottom:14px;"></div>

      <div class="sub-tabs" style="margin-bottom:16px;">
        <button id="tabBtnUsage" class="sub-tab active" onclick="switchAnalysisTab('usage')">📋 Student Connection Records</button>
        <button id="tabBtnSignal" class="sub-tab" onclick="switchAnalysisTab('signal')">📶 Low Signal & Congestion Logs</button>
      </div>

      <!-- Usage Table -->
      <div id="tab-usage-wrap" class="panel">
        <div class="panel-header">
          <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
            <input type="text" id="uSearch" class="input-field" placeholder="🔍 Search zone, date, ID..." oninput="fetchUsageRecords(1)" style="width:200px;">
            <select id="uZone" class="input-field" onchange="fetchUsageRecords(1)">
              <option value="">All Zones</option>
              {zone_opts}
            </select>
            <input type="date" id="uDate" class="input-field" onchange="fetchUsageRecords(1)">
            <select id="uSrc" class="input-field" onchange="fetchUsageRecords(1)">
              <option value="">All Sources</option>
              <option value="actual">Actual</option>
              <option value="simulated">Simulated</option>
            </select>
            <button class="btn btn-secondary" onclick="resetUsageFilters()" style="padding:6px 12px; font-size:0.8rem;">Reset</button>
          </div>
          <div style="display:flex; gap:8px;">
            <a id="uExportBtn" href="/api/export/usage" class="btn btn-success">
              <span>📥</span> Download Excel File (.csv)
            </a>
          </div>
        </div>

        <div class="table-responsive">
          <table>
            <thead>
              <tr>
                <th onclick="sortUsage('record_id')">ID ⇅</th>
                <th onclick="sortUsage('date')">Date ⇅</th>
                <th>Time (8:30-18:30)</th>
                <th>Wi-Fi Zone</th>
                <th onclick="sortUsage('connected_users')">Connected Students ⇅</th>
                <th onclick="sortUsage('bandwidth_usage_mbps')">Bandwidth (Mbps) ⇅</th>
                <th onclick="sortUsage('data_source')">Source ⇅</th>
              </tr>
            </thead>
            <tbody id="usageTableBody">
              <tr><td colspan="7" style="text-align:center; color:var(--text-dim); padding:25px;">Loading records...</td></tr>
            </tbody>
          </table>
        </div>
        <div id="uPagination" style="margin-top:14px; display:flex; justify-content:space-between; align-items:center;"></div>
      </div>

      <!-- Signal Table -->
      <div id="tab-signal-wrap" class="panel" style="display:none;">
        <div class="panel-header">
          <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
            <input type="text" id="sSearch" class="input-field" placeholder="🔍 Search zone, date, ID..." oninput="fetchSignalRecords(1)" style="width:200px;">
            <select id="sZone" class="input-field" onchange="fetchSignalRecords(1)">
              <option value="">All Zones</option>
              {zone_opts}
            </select>
            <input type="date" id="sDate" class="input-field" onchange="fetchSignalRecords(1)">
            <select id="sLevel" class="input-field" onchange="fetchSignalRecords(1)">
              <option value="">All Congestion Levels</option>
              <option value="Low">Low</option>
              <option value="Moderate">Moderate</option>
              <option value="High">High</option>
              <option value="Congested">Congested</option>
            </select>
            <select id="sSrc" class="input-field" onchange="fetchSignalRecords(1)">
              <option value="">All Sources</option>
              <option value="actual">Actual</option>
              <option value="simulated">Simulated</option>
            </select>
            <button class="btn btn-secondary" onclick="resetSignalFilters()" style="padding:6px 12px; font-size:0.8rem;">Reset</button>
          </div>
          <a id="sExportBtn" href="/api/export/signal" class="btn btn-success">
            <span>📥</span> Download Excel File (.csv)
          </a>
        </div>

        <div class="table-responsive">
          <table>
            <thead>
              <tr>
                <th onclick="sortSignal('record_id')">ID ⇅</th>
                <th onclick="sortSignal('date')">Date ⇅</th>
                <th>Time</th>
                <th>Wi-Fi Zone</th>
                <th onclick="sortSignal('signal_strength_dbm')">Signal (dBm) ⇅</th>
                <th onclick="sortSignal('congestion_level')">Congestion Level ⇅</th>
                <th onclick="sortSignal('alert_status')">Alert Status ⇅</th>
                <th onclick="sortSignal('data_source')">Source ⇅</th>
              </tr>
            </thead>
            <tbody id="signalTableBody">
              <tr><td colspan="8" style="text-align:center; color:var(--text-dim); padding:25px;">Loading records...</td></tr>
            </tbody>
          </table>
        </div>
        <div id="sPagination" style="margin-top:14px; display:flex; justify-content:space-between; align-items:center;"></div>
      </div>
    </section>

    <!-- ──────────────── VIEW 3: BANDWIDTH RECOMMENDATIONS & PREDICTION ──────────────── -->
    <section id="view-recommendations" class="view-section">
      <div class="top-bar">
        <div class="page-title">
          <h1>Bandwidth Recommendations & Predictive Forecasting</h1>
          <p>Identifies exact bottleneck locations and forecasts traffic to calculate bandwidth increases</p>
        </div>
        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          <div style="display:flex; align-items:center; gap:6px;">
            <span style="font-size:0.8rem; color:var(--text-muted); font-weight:600;">Date:</span>
            <select id="recDatePicker" class="input-field" style="padding:6px 12px; font-weight:600;" onchange="onRecDateChange()">
            </select>
          </div>
          <button class="btn btn-primary" onclick="recSimulatePeak(this)">
            <span>⚡</span> <span>Simulate Peak Rush (02:30 PM)</span>
          </button>
          <button class="btn btn-secondary" onclick="recSimulateReset(this)">
            <span>🔄</span> <span>Reset Baseline</span>
          </button>
          <a id="recExportBtn" href="/api/export/recommendations" class="btn btn-success">
            <span>📥</span> <span>Export Excel Upgrade Plan</span>
          </a>
        </div>
      </div>

      <!-- Weekend Inactivity Notice (Visible on Saturday and Sunday) -->
      <div id="recWeekendBox" class="weekend-card" style="display:none;">
        <div style="font-size:2.4rem; margin-bottom:10px;">🛑</div>
        <h2 style="font-size:1.4rem; font-weight:800; color:#fbbf24; margin-bottom:8px;">
          No Bandwidth Recommendations (Weekend / Closed)
        </h2>
        <p style="font-size:0.92rem; color:#f8fafc; max-width:650px; margin:0 auto 16px; line-height:1.5;">
          The selected date is a <strong>Saturday or Sunday</strong>. College is closed and student network activity is <strong>0</strong>.
          Bandwidth recommendations and predictive forecasting are only calculated for active college working days (<strong>Monday to Friday, 8:30 AM to 6:30 PM</strong>).
        </p>
        <div style="display:inline-flex; align-items:center; gap:10px; background:rgba(0,0,0,0.3); padding:8px 16px; border-radius:20px; font-size:0.84rem; color:#cbd5e1;">
          👉 <span>Select a weekday (e.g. Friday) from the date dropdown above to see active recommendations!</span>
        </div>
      </div>

      <!-- Working Day Content (Visible Monday through Friday) -->
      <div id="recWorkingContent">
        <!-- Unified Prediction & Executive Upgrade Banner -->
        <div class="highlight-box">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; flex-wrap:wrap; gap:10px;">
            <div>
              <div style="font-size:0.75rem; text-transform:uppercase; letter-spacing:0.08em; color:var(--primary-light); font-weight:800;">
                Predictive Congestion Analysis & Upgrade Target
              </div>
              <h2 style="font-size:1.35rem; font-weight:800; color:#fff; margin-top:2px;">
                🎯 Where Should We Increase Our Wi-Fi Bandwidth?
              </h2>
            </div>
            <span id="recStatusPill" class="badge badge-yellow" style="font-size:0.84rem; padding:6px 14px;">
              Analyzing...
            </span>
          </div>

          <!-- Plain English Prediction + Recommendation Text -->
          <p id="recExecutiveSummary" style="font-size:0.92rem; color:#f8fafc; line-height:1.55; margin-bottom:14px;">
            Evaluating live student traffic and predictive Markov congestion forecasts...
          </p>

          <!-- Simple 3-Box Prediction Insight Bar -->
          <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px; padding-top:12px; border-top:1px solid rgba(255,255,255,0.08);">
            <div style="background:rgba(0,0,0,0.25); padding:10px 14px; border-radius:8px;">
              <div style="font-size:0.72rem; color:var(--text-muted); font-weight:700;">PREDICTED TRAFFIC STATE</div>
              <div style="font-size:1.1rem; font-weight:800; color:#fff; margin-top:2px;" id="predStateVal">—</div>
              <div style="font-size:0.72rem; color:var(--text-dim);" id="predConfVal">Confidence: —%</div>
            </div>

            <div style="background:rgba(0,0,0,0.25); padding:10px 14px; border-radius:8px;">
              <div style="font-size:0.72rem; color:var(--text-muted); font-weight:700;">PREDICTED BOTTLENECK TIME</div>
              <div style="font-size:1.1rem; font-weight:800; color:#fbbf24; margin-top:2px;" id="predPeakTimeVal">—</div>
              <div style="font-size:0.72rem; color:var(--text-dim);">Peak afternoon rush hour</div>
            </div>

            <div style="background:rgba(0,0,0,0.25); padding:10px 14px; border-radius:8px;">
              <div style="font-size:0.72rem; color:var(--text-muted); font-weight:700;">PREVENTIVE ACTION</div>
              <div style="font-size:1.05rem; font-weight:800; color:#34d399; margin-top:2px;" id="predActionVal">—</div>
              <div style="font-size:0.72rem; color:var(--text-dim);">To prevent student disconnects</div>
            </div>
          </div>
        </div>

        <!-- KPI Summary Cards -->
        <div class="cards-grid">
          <div class="card">
            <div class="card-label">Total Additional Bandwidth Needed</div>
            <div class="card-val" id="recValTotalAdd" style="color:#f87171;">—</div>
            <div class="card-desc">Deficit across all bottleneck zones</div>
          </div>

          <div class="card">
            <div class="card-label">Top Priority Bottleneck Zone</div>
            <div class="card-val" id="recValTopZone" style="font-size:1.35rem; color:#fbbf24; margin-top:4px;">—</div>
            <div class="card-desc" id="recValTopZoneSub">Highest deficit percentage</div>
          </div>

          <div class="card">
            <div class="card-label">Campus Gateway Bandwidth</div>
            <div class="card-val" id="recValGateway">—</div>
            <div class="card-desc" id="recValGatewaySub">Current: 1,000 Mbps Cap</div>
          </div>

          <div class="card">
            <div class="card-label">Zones Needing Upgrade</div>
            <div class="card-val" id="recValZonesCount">—</div>
            <div class="card-desc" id="recValZonesCountSub">Out of 6 campus locations</div>
          </div>
        </div>

        <!-- Visual Zone Cards Grid -->
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">
              <span>📍</span> <span>Zone-by-Zone Upgrade Breakdown</span>
            </div>
            <span style="font-size:0.8rem; color:var(--text-dim);">Prioritized by predicted congestion</span>
          </div>

          <div class="grid-3" id="recZoneCardsContainer">
            <p style="color:var(--text-dim); padding:20px;">Analyzing campus zone capacities...</p>
          </div>
        </div>

        <!-- Comprehensive Technical Table -->
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">
              <span>📊</span> <span>Complete Bandwidth Allocation & Action Strategy</span>
            </div>
            <span style="font-size:0.8rem; color:var(--text-muted);">Formula: <code>required = peak_demand ÷ 75% headroom</code></span>
          </div>

          <div class="table-responsive">
            <table>
              <thead>
                <tr>
                  <th>Wi-Fi Zone</th>
                  <th>Current Bandwidth</th>
                  <th>Peak Hour & Students</th>
                  <th>Peak Demand</th>
                  <th>Current Load %</th>
                  <th>Recommended Cap</th>
                  <th style="color:#60a5fa;">Bandwidth to Add</th>
                  <th>Priority</th>
                  <th>Recommended Network Action</th>
                </tr>
              </thead>
              <tbody id="recTableBody">
                <tr><td colspan="9" style="text-align:center; color:var(--text-dim); padding:25px;">Loading recommendations...</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>

  </main>

  <script>
    // ──────────────────────── JAVASCRIPT STATE ────────────────────────
    let currentView = 'dashboard';
    let mainChartInstance = null;
    let selectedDate = new Date().toISOString().split('T')[0];
    let chartRange = 'day';
    let uSortCol = 'record_id', uSortOrder = 'asc';
    let sSortCol = 'record_id', sSortOrder = 'asc';

    function setChartRange(range) {{
      chartRange = range;
      document.getElementById('btnRangeDay').classList.toggle('active', range === 'day');
      document.getElementById('btnRangeWeek').classList.toggle('active', range === 'week');
      document.getElementById('btnRangeMonth').classList.toggle('active', range === 'month');
      loadChartOnly();
    }}

    async function onDateChange() {{
      const picker = document.getElementById('dashDatePicker');
      if (picker) {{
        selectedDate = picker.value;
        const recP = document.getElementById('recDatePicker');
        if (recP) recP.value = selectedDate;
        loadDashboard();
      }}
    }}

    async function onRecDateChange() {{
      const picker = document.getElementById('recDatePicker');
      if (picker) {{
        selectedDate = picker.value;
        const dashP = document.getElementById('dashDatePicker');
        if (dashP) dashP.value = selectedDate;
        loadRecommendations();
      }}
    }}

    async function populateDateSelectors() {{
      try {{
        const dates = await fetch('/api/dates').then(r => r.json());
        const pickers = [document.getElementById('dashDatePicker'), document.getElementById('recDatePicker')];

        pickers.forEach(picker => {{
          if (picker && dates && dates.length > 0) {{
            const currentVal = picker.value || selectedDate;
            picker.innerHTML = dates.map(d => `<option value="${{d.date}}">${{d.display}}</option>`).join('');
            const found = dates.some(d => d.date === currentVal);
            if (found) {{
              picker.value = currentVal;
            }} else {{
              picker.value = dates[0].date;
            }}
          }}
        }});
        if (dates && dates.length > 0 && !dates.some(d => d.date === selectedDate)) {{
          selectedDate = dates[0].date;
        }}
      }} catch (err) {{
        console.error(err);
      }}
    }}

    async function loadChartOnly() {{
      const ctx = document.getElementById('mainChart');
      if (!ctx) return;

      if (chartRange === 'day') {{
        document.getElementById('chartHeading').textContent = `College Hours Wi-Fi Load (8:30 AM – 6:30 PM) on ${{selectedDate}}`;
        document.getElementById('chartIcon').textContent = '📈';
        const hourly = await fetch(`/api/usage/hourly?date=${{selectedDate}}`).then(r => r.json());
        renderLineChart(
          hourly.map(d => d.label),
          hourly.map(d => d.connected_users),
          'Connected Students',
          '#3b82f6',
          hourly.map(d => d.is_peak ? '#ef4444' : '#3b82f6'),
          hourly.map(d => d.is_peak ? 8 : 4.5)
        );
      }} else if (chartRange === 'week') {{
        document.getElementById('chartHeading').textContent = `Weekly Load Curve (Sunday to Saturday) for week of ${{selectedDate}}`;
        document.getElementById('chartIcon').textContent = '📅';
        const weekly = await fetch(`/api/usage/weekly?date=${{selectedDate}}`).then(r => r.json());
        renderLineChart(
          weekly.map(d => d.label),
          weekly.map(d => d.total_users),
          'Total Daily Active Students',
          '#38bdf8',
          weekly.map(d => d.is_weekend ? '#64748b' : '#38bdf8'),
          weekly.map(d => d.is_weekend ? 5 : 6)
        );
      }} else if (chartRange === 'month') {{
        document.getElementById('chartHeading').textContent = `30-Day College Wi-Fi Load Trend (ending ${{selectedDate}})`;
        document.getElementById('chartIcon').textContent = '📊';
        const monthly = await fetch(`/api/usage/monthly?date=${{selectedDate}}`).then(r => r.json());
        renderLineChart(
          monthly.map(d => `${{d.date.slice(5)}} (${{d.day_name}})`),
          monthly.map(d => d.total_users),
          'Daily Total Students',
          '#818cf8',
          monthly.map(d => d.is_weekend ? '#475569' : '#818cf8'),
          monthly.map(d => d.is_weekend ? 3 : 4)
        );
      }}
    }}

    function renderLineChart(labels, values, labelTitle, primaryColor, pointColors, pointRadii) {{
      const ctx = document.getElementById('mainChart');
      if (!ctx) return;

      if (mainChartInstance) mainChartInstance.destroy();

      mainChartInstance = new Chart(ctx, {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [{{
            label: labelTitle,
            data: values,
            borderColor: primaryColor,
            borderWidth: 2.5,
            pointBackgroundColor: pointColors,
            pointBorderColor: '#ffffff',
            pointBorderWidth: 1.5,
            pointRadius: pointRadii,
            pointHoverRadius: 9,
            fill: true,
            backgroundColor: (context) => {{
              const chart = context.chart;
              const {{ctx, chartArea}} = chart;
              if (!chartArea) return null;
              const gradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
              gradient.addColorStop(0, primaryColor + '40');
              gradient.addColorStop(1, primaryColor + '00');
              return gradient;
            }},
            tension: 0.35
          }}]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          plugins: {{
            legend: {{ display: true, labels: {{ color: '#94a3b8', font: {{ family: 'Plus Jakarta Sans', size: 12 }} }} }},
            tooltip: {{
              backgroundColor: '#111a2e',
              titleColor: '#fff',
              bodyColor: '#93c5fd',
              borderColor: '#223455',
              borderWidth: 1,
              padding: 12,
              callbacks: {{
                label: (item) => `${{labelTitle}}: ${{item.raw.toLocaleString()}} students`
              }}
            }}
          }},
          scales: {{
            x: {{
              grid: {{ color: 'rgba(255,255,255,0.04)' }},
              ticks: {{ color: '#64748b', font: {{ family: 'Plus Jakarta Sans', size: 10 }} }}
            }},
            y: {{
              beginAtZero: true,
              grid: {{ color: 'rgba(255,255,255,0.05)' }},
              ticks: {{ color: '#64748b', font: {{ family: 'Plus Jakarta Sans', size: 11 }} }}
            }}
          }}
        }}
      }});
    }}

    // ──────────────────────── LOAD DASHBOARD ────────────────────────
    async function loadDashboard() {{
      try {{
        await populateDateSelectors();
        const [sum, alerts] = await Promise.all([
          fetch(`/api/summary?date=${{selectedDate}}`).then(r => r.json()),
          fetch(`/api/alerts?date=${{selectedDate}}`).then(r => r.json())
        ]);

        const banner = document.getElementById('dayStatusBanner');
        const bannerText = document.getElementById('dayStatusText');
        if (sum.is_weekend) {{
          banner.className = 'status-banner weekend';
          bannerText.innerHTML = `⚠️ <strong>${{sum.date}} (${{sum.day_name}}) is a Weekend / Non-Working Day</strong> — Academic buildings closed (0 users). Click <em>Simulate Peak Rush</em> or choose a weekday from the dropdown.`;
        }} else {{
          banner.className = 'status-banner working';
          bannerText.innerHTML = `🟢 <strong>${{sum.date}} (${{sum.day_name}}) — Working Day</strong> | Active campus timings: <strong>8:30 AM to 6:30 PM</strong> across all 6 zones.`;
        }}

        const badge = document.getElementById('dashBadge');
        if (sum.data_source === 'actual') {{
          badge.textContent = '● ACTUAL DATA';
          badge.className = 'badge badge-act';
        }} else {{
          badge.textContent = '● SIMULATED DATA';
          badge.className = 'badge badge-sim';
        }}

        document.getElementById('valTotalUsers').textContent = Number(sum.total_connected_users).toLocaleString();
        document.getElementById('valDateDesc').textContent = `Total on ${{selectedDate}} (${{sum.day_name}})`;
        document.getElementById('valPeakHour').textContent = (sum.peak_users > 0) ? (sum.peak_hour === '14:30' ? '02:30 PM' : sum.peak_hour) : 'None (Closed)';
        document.getElementById('valPeakUsersDesc').textContent = `${{(sum.peak_users || 0).toLocaleString()}} students at peak concurrency`;

        const utilVal = Number(sum.utilization_percent) || 0;
        const utilColor = utilVal > 90 ? '#ef4444' : (utilVal > 70 ? '#f59e0b' : '#10b981');
        document.getElementById('valUtil').innerHTML = `<span style="color: ${{utilColor}}">${{utilVal}}%</span>`;
        document.getElementById('valUtilStatus').textContent = utilVal > 90 ? 'Critical Congestion (>90%)' : (utilVal > 70 ? 'Moderate Demand' : 'Nominal Bandwidth');

        const totalAlerts = (sum.alert_counts.congestion || 0) + (sum.alert_counts.low_signal || 0);
        document.getElementById('valAlerts').innerHTML = `<span style="color: ${{totalAlerts > 0 ? '#ef4444' : '#10b981'}}">${{totalAlerts}}</span>`;
        document.getElementById('valAlertsSub').textContent = `${{sum.alert_counts.congestion || 0}} Congestion · ${{sum.alert_counts.low_signal || 0}} Weak Signal`;

        loadChartOnly();
        renderAlerts(alerts);
      }} catch (err) {{
        console.error(err);
      }}
    }}

    function renderAlerts(alerts) {{
      const cWrap = document.getElementById('congAlertsList');
      const sWrap = document.getElementById('sigAlertsList');

      if (!alerts.congestion_alerts || alerts.congestion_alerts.length === 0) {{
        cWrap.innerHTML = '<div style="padding:14px; background:rgba(16,185,129,0.08); border-radius:8px; color:#34d399; font-size:0.84rem;">✅ No congestion detected. Zone capacities are within normal limits.</div>';
      }} else {{
        cWrap.innerHTML = alerts.congestion_alerts.map(a => `
          <div class="alert-card cong">
            <div>
              <strong style="color:#fff;">${{a.zone}}</strong>
              <div style="font-size:0.76rem; color:var(--text-muted); margin-top:2px;">Time: ${{a.time}} · Saturation Detected</div>
            </div>
            <div style="text-align:right;">
              <span class="badge badge-red">${{a.congestion_level}}</span>
            </div>
          </div>
        `).join('');
      }}

      if (!alerts.low_signal_alerts || alerts.low_signal_alerts.length === 0) {{
        sWrap.innerHTML = '<div style="padding:14px; background:rgba(16,185,129,0.08); border-radius:8px; color:#34d399; font-size:0.84rem;">✅ All access points report strong coverage (&ge; -70 dBm).</div>';
      }} else {{
        sWrap.innerHTML = alerts.low_signal_alerts.map(a => `
          <div class="alert-card sig">
            <div>
              <strong style="color:#fff;">${{a.zone}}</strong>
              <div style="font-size:0.76rem; color:var(--text-muted); margin-top:2px;">Time: ${{a.time}} · Weak Signal Area</div>
            </div>
            <div style="text-align:right;">
              <span class="badge badge-yellow">${{a.signal_strength_dbm}} dBm</span>
            </div>
          </div>
        `).join('');
      }}
    }}

    async function simulatePeakLoad(btn) {{
      btn.disabled = true;
      btn.innerHTML = '<span>⚡</span> Simulating...';
      try {{
        const res = await fetch(`/api/simulate-peak?date=${{selectedDate}}`, {{ method: 'POST' }}).then(r => r.json());
        await loadDashboard();
        alert(`⚡ Peak rush simulated at ${{res.hour}}!\nTotal simulated concurrency: ${{res.simulated_users}} students.`);
      }} catch (err) {{
        console.error(err);
      }} finally {{
        btn.disabled = false;
        btn.innerHTML = '<span>⚡</span> Simulate Peak Rush (02:30 PM)';
      }}
    }}

    async function simulateReset(btn) {{
      btn.disabled = true;
      btn.innerHTML = '<span>🔄</span> Resetting...';
      try {{
        await fetch(`/api/simulate-reset?date=${{selectedDate}}`, {{ method: 'POST' }});
        await loadDashboard();
      }} catch (err) {{
        console.error(err);
      }} finally {{
        btn.disabled = false;
        btn.innerHTML = '<span>🔄</span> Reset Normal';
      }}
    }}

    // ──────────────────────── BANDWIDTH RECOMMENDATIONS & PREDICTION LOGIC ─────────────
    async function loadRecommendations() {{
      try {{
        await populateDateSelectors();
        document.getElementById('recExportBtn').href = `/api/export/recommendations?date=${{selectedDate}}`;

        const res = await fetch(`/api/recommendations/zones?date=${{selectedDate}}`).then(r => r.json());
        const weekendBox = document.getElementById('recWeekendBox');
        const workingContent = document.getElementById('recWorkingContent');

        // STRICT CHECK: SATURDAY AND SUNDAY NO RECOMMENDATIONS
        if (res.is_weekend) {{
          weekendBox.style.display = 'block';
          workingContent.style.display = 'none';
          return;
        }}

        // Working Day Mode
        weekendBox.style.display = 'none';
        workingContent.style.display = 'block';

        const {{ campus_gateway, zones, total_additional_needed_mbps, zones_needing_increase_count, prediction }} = res;

        // Prediction boxes
        document.getElementById('predStateVal').textContent = prediction.forecast_state || 'Moderate';
        document.getElementById('predConfVal').textContent = `Confidence: ${{prediction.confidence_pct}}%`;
        document.getElementById('predPeakTimeVal').textContent = prediction.predicted_peak_time || '02:30 PM';
        document.getElementById('predActionVal').textContent = total_additional_needed_mbps > 0 
          ? `+${{total_additional_needed_mbps}} Mbps Upgrade Needed` 
          : 'Zero Deficit (Adequate)';

        // Executive summary
        const pill = document.getElementById('recStatusPill');
        const summaryText = document.getElementById('recExecutiveSummary');

        if (total_additional_needed_mbps > 0) {{
          pill.className = 'badge badge-red';
          pill.innerHTML = `🚨 Upgrade Needed: +${{total_additional_needed_mbps}} Mbps Across ${{zones_needing_increase_count}} Zones`;
          summaryText.innerHTML = `
            <strong>Prediction & Diagnosis:</strong> ${{prediction.summary}}
            <br>To maintain low-latency traffic and avoid bufferbloat during upcoming peak hours, 
            we recommend provisioning <strong>+${{total_additional_needed_mbps}} Mbps</strong> distributed across the prioritized locations below.
          `;
        }} else {{
          pill.className = 'badge badge-green';
          pill.innerHTML = `✅ All Zones Have Sufficient Headroom`;
          summaryText.innerHTML = `
            <strong>Prediction & Diagnosis:</strong> ${{prediction.summary}}
            All campus zones currently operate within the safe 75% utilization headroom. 
            Campus bandwidth of <strong>${{campus_gateway.current_capacity_mbps}} Mbps</strong> is fully adequate for peak student loads.
          `;
        }}

        // KPI cards
        const totalAddElem = document.getElementById('recValTotalAdd');
        totalAddElem.textContent = total_additional_needed_mbps > 0 ? `+${{total_additional_needed_mbps}} Mbps` : '0 Mbps';
        totalAddElem.style.color = total_additional_needed_mbps > 0 ? '#ef4444' : '#10b981';

        const topZone = zones[0];
        document.getElementById('recValTopZone').textContent = topZone ? topZone.name : 'None';
        document.getElementById('recValTopZoneSub').textContent = topZone && topZone.additional_bandwidth_needed > 0
          ? `+${{topZone.additional_bandwidth_needed}} Mbps deficit at ${{topZone.peak_time}}`
          : 'All zones within limits';

        document.getElementById('recValGateway').textContent = `${{campus_gateway.recommended_total_mbps}} Mbps`;
        document.getElementById('recValGatewaySub').textContent = `Current Cap: ${{campus_gateway.current_capacity_mbps}} Mbps (${{campus_gateway.additional_needed_mbps > 0 ? '+' + campus_gateway.additional_needed_mbps + ' Mbps gateway deficit' : 'Adequate'}})`

        document.getElementById('recValZonesCount').textContent = `${{zones_needing_increase_count}} / ${{zones.length}}`;
        document.getElementById('recValZonesCountSub').textContent = zones_needing_increase_count > 0 ? 'Locations requiring link expansion' : 'All locations nominal';

        // Render visual zone cards
        const cardsWrap = document.getElementById('recZoneCardsContainer');
        const zoneIcons = {{
          'Computer Labs': '💻',
          'Library': '📚',
          'Classrooms': '🏫',
          'Canteen': '🍽️',
          'Auditorium': '🎭',
          'Hostel Block': '🛏️'
        }};

        cardsWrap.innerHTML = zones.map(z => {{
          const isUrgent = z.additional_bandwidth_needed > 50 || z.current_utilization_pct >= 95;
          const isModerate = z.additional_bandwidth_needed > 0 || z.current_utilization_pct >= 80;
          const cardClass = isUrgent ? 'urgent' : (isModerate ? 'moderate' : 'optimal');
          const badgeClass = isUrgent ? 'badge-red' : (isModerate ? 'badge-yellow' : 'badge-green');
          const barColor = isUrgent ? '#ef4444' : (isModerate ? '#f59e0b' : '#10b981');
          const progressWidth = Math.min(100, z.current_utilization_pct);

          return `
            <div class="rec-zone-card ${{cardClass}}">
              <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <div>
                  <div style="font-size:1.15rem; font-weight:800; color:#fff; display:flex; align-items:center; gap:8px;">
                    <span>${{zoneIcons[z.name] || '📍'}}</span>
                    <span>${{z.name}}</span>
                  </div>
                  <div style="font-size:0.76rem; color:var(--text-dim); margin-top:3px;">
                    Peak Concurrency: <strong>${{z.peak_users}} Students</strong> @ <strong>${{z.peak_time}}</strong>
                  </div>
                </div>
                <span class="badge ${{badgeClass}}">${{z.priority_label}}</span>
              </div>

              <!-- Bandwidth comparison numbers -->
              <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px; background:rgba(255,255,255,0.02); padding:10px 12px; border-radius:8px;">
                <div>
                  <div style="font-size:0.72rem; color:var(--text-muted);">CURRENT CAPACITY</div>
                  <div style="font-size:1.05rem; font-weight:700; color:#fff;">${{z.current_capacity_mbps}} Mbps</div>
                </div>
                <div style="text-align:right;">
                  <div style="font-size:0.72rem; color:var(--text-muted);">RECOMMENDED</div>
                  <div style="font-size:1.05rem; font-weight:800; color:#60a5fa;">${{z.recommended_bandwidth_mbps}} Mbps</div>
                </div>
              </div>

              <!-- Progress bar -->
              <div>
                <div style="display:flex; justify-content:space-between; font-size:0.76rem; color:var(--text-dim);">
                  <span>Peak Utilization: <strong style="color:${{barColor}}">${{z.current_utilization_pct}}%</strong></span>
                  <span>Target Safe Load: <strong>&le; 75%</strong></span>
                </div>
                <div class="progress-bar-bg">
                  <div class="progress-bar-fill" style="width:${{progressWidth}}%; background:${{barColor}};"></div>
                </div>
              </div>

              <!-- Bandwidth Deficit Pill & Action -->
              <div style="display:flex; align-items:center; justify-content:space-between; padding-top:4px; border-top:1px solid rgba(255,255,255,0.05);">
                <span style="font-size:0.8rem; font-weight:700; color:var(--text-muted);">Increase Target:</span>
                <span style="font-size:1.05rem; font-weight:800; color:${{z.additional_bandwidth_needed > 0 ? '#f87171' : '#34d399'}};">
                  ${{z.additional_bandwidth_needed > 0 ? '+' + z.additional_bandwidth_needed + ' Mbps 🚨' : '0 Mbps (Sufficient) ✅'}}
                </span>
              </div>

              <div style="font-size:0.78rem; color:var(--text-muted); line-height:1.45; background:rgba(0,0,0,0.2); padding:8px 10px; border-radius:6px;">
                <strong>⚡ Network Action:</strong> ${{z.action}}
              </div>
            </div>
          `;
        }}).join('');

        // Render full technical table
        const tbody = document.getElementById('recTableBody');
        tbody.innerHTML = zones.map(z => {{
          const isUrgent = z.additional_bandwidth_needed > 50 || z.current_utilization_pct >= 95;
          const isModerate = z.additional_bandwidth_needed > 0 || z.current_utilization_pct >= 80;
          const badgeClass = isUrgent ? 'badge-red' : (isModerate ? 'badge-yellow' : 'badge-green');
          const addColor = z.additional_bandwidth_needed > 0 ? '#ef4444' : '#10b981';

          return `
            <tr>
              <td><strong style="color:#fff;">${{zoneIcons[z.name] || '📍'}} ${{z.name}}</strong></td>
              <td>${{z.current_capacity_mbps}} Mbps</td>
              <td>${{z.peak_users}} students @ <span class="badge badge-neutral">${{z.peak_time}}</span></td>
              <td>${{z.peak_demand_mbps}} Mbps</td>
              <td style="font-weight:700; color:${{z.current_utilization_pct > 80 ? '#f87171' : '#34d399'}};">${{z.current_utilization_pct}}%</td>
              <td style="font-weight:700; color:#60a5fa;">${{z.recommended_bandwidth_mbps}} Mbps</td>
              <td style="font-weight:800; font-size:0.95rem; color:${{addColor}};">
                ${{z.additional_bandwidth_needed > 0 ? '+' + z.additional_bandwidth_needed + ' Mbps' : '0 Mbps'}}
              </td>
              <td><span class="badge ${{badgeClass}}">${{z.priority_label}}</span></td>
              <td style="font-size:0.78rem; color:var(--text-muted); max-width:280px;">${{z.action}}</td>
            </tr>
          `;
        }}).join('');

      }} catch (err) {{
        console.error(err);
      }}
    }}

    async function recSimulatePeak(btn) {{
      btn.disabled = true;
      btn.innerHTML = '<span>⚡</span> Simulating...';
      try {{
        await fetch(`/api/simulate-peak?date=${{selectedDate}}`, {{ method: 'POST' }});
        await loadRecommendations();
      }} catch (e) {{
        console.error(e);
      }} finally {{
        btn.disabled = false;
        btn.innerHTML = '<span>⚡</span> Simulate Peak Rush (02:30 PM)';
      }}
    }}

    async function recSimulateReset(btn) {{
      btn.disabled = true;
      btn.innerHTML = '<span>🔄</span> Resetting...';
      try {{
        await fetch(`/api/simulate-reset?date=${{selectedDate}}`, {{ method: 'POST' }});
        await loadRecommendations();
      }} catch (e) {{
        console.error(e);
      }} finally {{
        btn.disabled = false;
        btn.innerHTML = '<span>🔄</span> Reset Baseline';
      }}
    }}

    // ──────────────────────── DATA ANALYSIS LOGIC ────────────────────────
    function switchAnalysisTab(tab) {{
      const bUsage = document.getElementById('tabBtnUsage');
      const bSignal = document.getElementById('tabBtnSignal');
      const wrapUsage = document.getElementById('tab-usage-wrap');
      const wrapSignal = document.getElementById('tab-signal-wrap');

      if (tab === 'usage') {{
        if (bUsage) bUsage.classList.add('active');
        if (bSignal) bSignal.classList.remove('active');
        if (wrapUsage) wrapUsage.style.display = 'block';
        if (wrapSignal) wrapSignal.style.display = 'none';
        fetchUsageRecords(1);
      }} else {{
        if (bUsage) bUsage.classList.remove('active');
        if (bSignal) bSignal.classList.add('active');
        if (wrapUsage) wrapUsage.style.display = 'none';
        if (wrapSignal) wrapSignal.style.display = 'block';
        fetchSignalRecords(1);
      }}
    }}

    function resetUsageFilters() {{
      const uSearch = document.getElementById('uSearch'); if (uSearch) uSearch.value = '';
      const uZone = document.getElementById('uZone'); if (uZone) uZone.value = '';
      const uDate = document.getElementById('uDate'); if (uDate) uDate.value = '';
      const uSrc = document.getElementById('uSrc'); if (uSrc) uSrc.value = '';
      fetchUsageRecords(1);
    }}

    function resetSignalFilters() {{
      const sSearch = document.getElementById('sSearch'); if (sSearch) sSearch.value = '';
      const sZone = document.getElementById('sZone'); if (sZone) sZone.value = '';
      const sDate = document.getElementById('sDate'); if (sDate) sDate.value = '';
      const sLevel = document.getElementById('sLevel'); if (sLevel) sLevel.value = '';
      const sSrc = document.getElementById('sSrc'); if (sSrc) sSrc.value = '';
      fetchSignalRecords(1);
    }}

    function sortUsage(col) {{
      uSortOrder = (uSortCol === col && uSortOrder === 'asc') ? 'desc' : 'asc';
      uSortCol = col;
      fetchUsageRecords(1);
    }}

    function sortSignal(col) {{
      sSortOrder = (sSortCol === col && sSortOrder === 'asc') ? 'desc' : 'asc';
      sSortCol = col;
      fetchSignalRecords(1);
    }}

    async function fetchUsageRecords(page) {{
      try {{
        const searchVal = document.getElementById('uSearch') ? document.getElementById('uSearch').value.trim() : '';
        const zoneVal = document.getElementById('uZone') ? document.getElementById('uZone').value.trim() : '';
        const dateVal = document.getElementById('uDate') ? document.getElementById('uDate').value.trim() : '';
        const srcVal = document.getElementById('uSrc') ? document.getElementById('uSrc').value.trim() : '';

        const params = new URLSearchParams({{
          page: page,
          size: 15,
          sort: uSortCol,
          order: uSortOrder,
          search: searchVal,
          zone: zoneVal,
          date: dateVal,
          data_source: srcVal
        }});

        const expBtn = document.getElementById('uExportBtn');
        if (expBtn) expBtn.href = '/api/export/usage?' + params.toString();

        const res = await fetch('/api/records/usage?' + params).then(r => r.json());
        const tbody = document.getElementById('usageTableBody');
        if (!tbody) return;

        if (!res.data || res.data.length === 0) {{
          tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-dim); padding: 25px;">No records match your search or filter.</td></tr>';
          const pag = document.getElementById('uPagination');
          if (pag) pag.innerHTML = '';
          return;
        }}

        tbody.innerHTML = res.data.map(r => `
          <tr>
            <td style="font-family:'JetBrains Mono', monospace; font-size:0.8rem; color:var(--text-dim);">${{r.record_id}}</td>
            <td>${{r.date}}</td>
            <td><span class="badge badge-neutral">${{r.time}}</span></td>
            <td><strong style="color:#fff;">${{r.zone}}</strong></td>
            <td>${{Number(r.connected_users).toLocaleString()}}</td>
            <td>${{Number(r.bandwidth_usage_mbps).toFixed(1)}}</td>
            <td><span class="badge ${{r.data_source === 'actual' ? 'badge-act' : 'badge-sim'}}">${{r.data_source}}</span></td>
          </tr>
        `).join('');

        renderPagination('uPagination', res, fetchUsageRecords);
      }} catch (e) {{
        console.error("fetchUsageRecords error:", e);
        const tbody = document.getElementById('usageTableBody');
        if (tbody) tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--danger); padding: 25px;">Error loading records: ${{e.message}}</td></tr>`;
      }}
    }}

    async function fetchSignalRecords(page) {{
      try {{
        const searchVal = document.getElementById('sSearch') ? document.getElementById('sSearch').value.trim() : '';
        const zoneVal = document.getElementById('sZone') ? document.getElementById('sZone').value.trim() : '';
        const dateVal = document.getElementById('sDate') ? document.getElementById('sDate').value.trim() : '';
        const levelVal = document.getElementById('sLevel') ? document.getElementById('sLevel').value.trim() : '';
        const srcVal = document.getElementById('sSrc') ? document.getElementById('sSrc').value.trim() : '';

        const params = new URLSearchParams({{
          page: page,
          size: 15,
          sort: sSortCol,
          order: sSortOrder,
          search: searchVal,
          zone: zoneVal,
          date: dateVal,
          level: levelVal,
          data_source: srcVal
        }});

        const expBtn = document.getElementById('sExportBtn');
        if (expBtn) expBtn.href = '/api/export/signal?' + params.toString();

        const res = await fetch('/api/records/signal?' + params).then(r => r.json());
        const tbody = document.getElementById('signalTableBody');
        if (!tbody) return;

        if (!res.data || res.data.length === 0) {{
          tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-dim); padding: 25px;">No records match your search or filter.</td></tr>';
          const pag = document.getElementById('sPagination');
          if (pag) pag.innerHTML = '';
          return;
        }}

        const congBadge = (lvl) => {{
          const map = {{ 'Low': 'badge-green', 'Moderate': 'badge-yellow', 'High': 'badge-yellow', 'Congested': 'badge-red' }};
          return `<span class="badge ${{map[lvl] || 'badge-neutral'}}">${{lvl}}</span>`;
        }};

        tbody.innerHTML = res.data.map(r => `
          <tr>
            <td style="font-family:'JetBrains Mono', monospace; font-size:0.8rem; color:var(--text-dim);">${{r.record_id}}</td>
            <td>${{r.date}}</td>
            <td><span class="badge badge-neutral">${{r.time}}</span></td>
            <td><strong style="color:#fff;">${{r.zone}}</strong></td>
            <td style="font-weight:700; color:${{r.signal_strength_dbm < -70 ? '#f59e0b' : '#10b981'}};">${{r.signal_strength_dbm}} dBm</td>
            <td>${{congBadge(r.congestion_level)}}</td>
            <td><span class="badge ${{r.alert_status === 'active' ? 'badge-red' : 'badge-green'}}">${{r.alert_status}}</span></td>
            <td><span class="badge ${{r.data_source === 'actual' ? 'badge-act' : 'badge-sim'}}">${{r.data_source}}</span></td>
          </tr>
        `).join('');

        renderPagination('sPagination', res, fetchSignalRecords);
      }} catch (e) {{
        console.error("fetchSignalRecords error:", e);
        const tbody = document.getElementById('signalTableBody');
        if (tbody) tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--danger); padding: 25px;">Error loading records: ${{e.message}}</td></tr>`;
      }}
    }}

    function renderPagination(elemId, data, callbackFn) {{
      const container = document.getElementById(elemId);
      if (!container) return;

      container.innerHTML = `
        <div style="font-size:0.82rem; color:var(--text-dim);">
          Page ${{data.page}} of ${{data.pages}} (${{data.total.toLocaleString()}} records)
        </div>
        <div style="display:flex; gap:6px;">
          <button class="btn btn-secondary" style="padding:5px 12px; font-size:0.8rem;" onclick="${{callbackFn.name}}(${{data.page - 1}})" ${{data.page <= 1 ? 'disabled' : ''}}>
            ◀ Prev
          </button>
          <button class="btn btn-secondary" style="padding:5px 12px; font-size:0.8rem;" onclick="${{callbackFn.name}}(${{data.page + 1}})" ${{data.page >= data.pages ? 'disabled' : ''}}>
            Next ▶
          </button>
        </div>
      `;
    }}

    async function handleAnalysisImport() {{
      const fileInput = document.getElementById('analysisFileInput');
      if (!fileInput.files || fileInput.files.length === 0) return;

      const formData = new FormData();
      formData.append('file', fileInput.files[0]);

      showNotice("Uploading & importing Excel data...", "info");
      try {{
        const r = await fetch('/api/import?table=usage', {{ method: 'POST', body: formData }}).then(r => r.json());
        if (r.error) {{
          showNotice("❌ Import error: " + r.error, "danger");
        }} else {{
          showNotice(`✅ Successfully imported ${{r.rows}} student records from Excel!`, "success");
          fetchUsageRecords(1);
        }}
      }} catch (err) {{
        showNotice("❌ Network error uploading CSV", "danger");
      }} finally {{
        fileInput.value = '';
      }}
    }}

    function showNotice(msg, type) {{
      const box = document.getElementById('analysisNotification');
      if (!box) return;
      const colors = {{
        'info': 'background: rgba(59, 130, 246, 0.15); color: #93c5fd; border: 1px solid rgba(59, 130, 246, 0.3);',
        'success': 'background: rgba(16, 185, 129, 0.15); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.3);',
        'danger': 'background: rgba(239, 68, 68, 0.15); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.3);'
      }};
      box.innerHTML = `<div style="padding:10px 14px; border-radius:8px; font-weight:600; font-size:0.86rem; ${{colors[type] || colors['info']}}">${{msg}}</div>`;
      setTimeout(() => box.innerHTML = '', 4500);
    }}

    // ──────────────────────── VIEW SWITCHING ────────────────────────
    function switchView(viewName) {{
      if (viewName === 'db') viewName = 'recommendations';
      currentView = viewName;
      document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));

      const target = document.getElementById('view-' + viewName);
      if (target) target.classList.add('active');

      const links = document.querySelectorAll('.nav-link');
      if (viewName === 'dashboard') links[0].classList.add('active');
      else if (viewName === 'analysis') links[1].classList.add('active');
      else if (viewName === 'recommendations') links[2].classList.add('active');

      window.history.pushState(null, '', viewName === 'dashboard' ? '/' : '/' + viewName);

      if (viewName === 'dashboard') loadDashboard();
      else if (viewName === 'analysis') {{ fetchUsageRecords(1); fetchSignalRecords(1); }}
      else if (viewName === 'recommendations') loadRecommendations();
    }}

    window.addEventListener('popstate', () => {{
      const path = window.location.pathname.replace('/', '') || 'dashboard';
      switchView(path);
    }});

    window.addEventListener('DOMContentLoaded', () => {{
      const path = window.location.pathname.replace('/', '') || 'dashboard';
      switchView(path);
    }});
  </script>
</body>
</html>
"""

# ─────────────────────────── STARTUP ─────────────────────────────────────────

def _ensure_db():
    """Initialize schema and seed if the DB is empty (cold start on Vercel or first local run)."""
    init_schema()
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    count = db.execute("SELECT COUNT(*) FROM zones").fetchone()[0]
    db.close()
    if count == 0:
        with app.app_context():
            with app.test_request_context('/api/seed'):
                g.db = sqlite3.connect(DB_PATH)
                g.db.row_factory = sqlite3.Row
                g.db.execute("PRAGMA foreign_keys=ON")
                try:
                    seed_database(35)
                    g.db.close()
                except Exception as e:
                    print(f"Seed error: {e}")

# Auto-seed on import (needed for Vercel serverless cold starts)
_ensure_db()

if __name__ == '__main__':
    print("🚀  College Wi-Fi System running on http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
