# College Wi-Fi Load Monitoring and Congestion Prediction System

A professional, self-contained dashboard that monitors college Wi-Fi usage, identifies peak usage hours, detects weak-signal areas and network congestion, predicts upcoming congestion, and recommends the additional bandwidth required.

Three connected pages:
1. **Wi-Fi Monitoring Dashboard** — hourly usage graph (6 AM–10 PM) with peak highlighted, summary cards, congestion & low-signal alerts, next-hour Markov prediction, bandwidth recommendation, ACTUAL/SIMULATED badge, "Simulate peak load" button.
2. **Wi-Fi Data Analysis** — searchable/sortable/filterable/paginated tables for User Connection Records and Signal & Congestion Records, with CSV export of filtered rows.
3. **Database Management** — live table previews, counts, last-updated timestamps, ER diagram, and Generate Demo Data / Reset Demo Data / Import CSV buttons.

## Tech stack
- Backend: Python Flask + Flask-CORS
- Database: SQLite (self-contained, `wifi.db`), stdlib `sqlite3`
- Prediction: Poisson distribution + Markov chain (Python)

Two frontend options (both read from the same Flask API):

### Option A — Server-rendered pages (works immediately, single command)
`run.py` serves the API and three HTML pages using Chart.js. No Node build needed.

### Option B — React + Tailwind + Recharts SPA (per suggested spec)
A React frontend (`frontend/`) using Tailwind CSS, Recharts, React Router, and Axios. Runs on `http://localhost:5173` and proxies `/api` to Flask on `5000`.

## Local setup (clean machine)

### 1. Install backend dependencies
```bash
pip install -r requirements.txt
```
(`requirements.txt`: Flask, flask-cors)

### 2. Run the backend
```bash
python run.py
```
The app auto-seeds the demo database on first run (fixed seed `42`, so data is identical every reset) and starts at **http://localhost:5000**.

- Dashboard:        http://localhost:5000/
- Data Analysis:    http://localhost:5000/analysis
- DB Management:    http://localhost:5000/db

### 3. (Optional) Run the React frontend
```bash
cd frontend
npm install
npm run dev
```
The SPA is available at **http://localhost:5173** and proxies API calls to the Flask backend on 5000.

## Key API endpoints
| Endpoint | Method | Description |
|---|---|---|
| `/api/summary` | GET | Connected users, peak hour, utilization, active alerts |
| `/api/usage/hourly` | GET | 16-hour (6am–10pm) connected-users series with peak flag |
| `/api/alerts` | GET | Congestion alerts + low-signal alerts |
| `/api/prediction` | GET | Markov next-hour state + transition matrix |
| `/api/recommendation` | GET | Bandwidth recommendation (formula from spec) |
| `/api/records/usage` | GET | User connection records (search/sort/filter/page) |
| `/api/records/signal` | GET | Signal & congestion records (search/sort/filter/page) |
| `/api/export/usage` | GET | CSV of filtered usage rows |
| `/api/export/signal` | GET | CSV of filtered signal rows |
| `/api/db/overview` | GET | Table counts, last-updated, previews |
| `/api/settings` | GET/PUT | Configurable settings |
| `/api/seed` | POST | Generate demo data |
| `/api/reset` | POST | Reset demo data (identical values, seed=42) |
| `/api/simulate-peak` | POST | Raise load for demo (updates graph/alerts/recommendation live) |
| `/api/import` | POST | CSV import (`?table=usage\|signal`, multipart `file`) |

## Defaults (editable via `/api/settings`)
- Total capacity: 1000 Mbps
- Target utilization: 75%
- Avg Mbps per user: 2.0
- Congestion: Green < 70%, Yellow 70–90%, Red > 90%
- Signal: Good ≥ −60 dBm, Fair −60…−70 dBm, Weak < −70 dBm

## Acceptance (all verified)
- Peak hour is identical on the graph, summary card, and recommendation panel (14:00, 661 users).
- Alert counts match the rows in the Signal & Congestion table (1 congestion + 8 low signal).
- Recommendation numbers match the formula: `estimated = peak_users × avg_mbps`; `required = estimated ÷ target_util`; `additional = max(0, required − current)`.
- Reset demo data restores identical values every time (seed `42`).
- CSV export matches the currently filtered table rows.
- Page 3 record counts match the rows shown on Page 2.
- All three pages load and all API endpoints return 200.

## Project layout
- `run.py` — self-contained Flask app (API + 3 server-rendered pages)
- `requirements.txt` — Python dependencies
- `wifi.db` — SQLite database (created on first run)
- `frontend/` — React + Tailwind + Recharts SPA (builds with `npm run build`, dev server on 5173)
- `backend/` — alternate Flask-SQLAlchemy implementation (not the running entry point)
