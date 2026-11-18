# health_monitoring_for_Nasa_competition

AERO-MED / ORION-IV — simulated astronaut bio-telemetry dashboard.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --port 8001 --reload
```

Port **8000 is blocked on some Windows machines**, so this project uses 8001.
If you must use a different port, append `?api=http://localhost:YOUR_PORT` to the
page URL so the frontend knows where the API lives.

Open <http://localhost:8001/> for the astronaut dashboard and
<http://localhost:8001/admin> for the flattened admin view. Do **not** open
`index.html` as a file (`file://`) — the API calls need an HTTP origin.

The SQLite database is created automatically at `data/orion_iv.sqlite3` on startup.

## Login gate (index.html)

`index.html` is blocked by a **LOGIN / NEW USER** gate. Nothing in the dashboard
loads until a session is established. The session token is kept in
`sessionStorage` and sent as `Authorization: Bearer <token>` to protected APIs.

- **LOGIN** — for astronauts who already have an account: enter the astronaut ID
  and the numeric password.
- **NEW USER** — for astronauts in the roster who have not registered yet. The
  ID, the exact roster name, and the roster-assigned numeric password must all
  match; otherwise registration is rejected.

The demo roster (also visible in the login panel) is:

| ID | Name | Numeric password | Initial state |
| --- | --- | --- | --- |
| AST-001 | Alex Morgan | 482913 | registered |
| AST-002 | Emily Carter | 739156 | registered |
| AST-003 | James Wilson | 264807 | registered |
| AST-004 | Olivia Bennett | 915342 | registered |
| AST-005 | Noah Davis | 357921 | registered |
| AST-006 | Sophia Martinez | 684015 | new user |
| AST-007 | Liam Anderson | 829374 | new user |
| AST-008 | Ava Thompson | 147258 | new user |
| AST-009 | Ethan Clark | 503861 | new user |
| AST-010 | Isabella Rivera | 970416 | new user |

Passwords are stored as salted PBKDF2-SHA256 hashes, never in plain text. The
plaintext values above exist only for this competition demo.

## Auth API

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/auth/roster` | no | Demo roster with account status |
| POST | `/api/auth/login` | no | Start a session |
| POST | `/api/auth/register` | no | Register a roster astronaut |
| GET | `/api/auth/me` | yes | Validate current session |
| POST | `/api/auth/logout` | yes | End current session |

Protected data endpoints: `/api/state`, `/api/telemetry`, `/api/alerts`,
`/api/alerts/acknowledge-all`, `/api/expert/status`.

## admin.html

Flattened to a zeroed read-only layout. All fabricated crew readings, fake logs,
clocks and the Electrocardiogram/oscilloscope panel were removed. Backend data
integration will be added later.
