"""Rebuild data/football.duckdb from the Kaggle Transfermarkt CSVs.

Source: https://www.kaggle.com/datasets/davidcariboo/player-scores/data
Needs the 12 CSVs in data/ (download requires a Kaggle API token).
game_lineups.csv is intentionally excluded (416 MB, low demo value).

Usage:  py -3.13 build_duckdb.py
"""
import duckdb
from pathlib import Path

TABLES = [
    "competitions", "countries", "national_teams", "clubs", "players",
    "games", "club_games", "transfers", "player_valuations",
    "appearances", "game_events",
]

data = Path(__file__).parent / "data"
out = data / "football.duckdb"
if out.exists():
    out.unlink()

con = duckdb.connect(str(out))
for t in TABLES:
    csv = data / f"{t}.csv"
    if not csv.exists():
        print(f"SKIP {t} (no {csv.name})")
        continue
    con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv('{csv}', header=true)")
    print(t, con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])

# Derived: current-club truth from dated evidence. players.current_club_name
# goes stale (departed/retired players keep old clubs); last_seen dates it.
con.execute("""
CREATE TABLE player_status AS
WITH club_apps AS (
  SELECT * FROM appearances WHERE player_club_id IN (SELECT club_id FROM clubs)
),
s AS (
  SELECT player_id,
         COUNT(*) AS appearances,
         MAX(date) AS last_seen,
         arg_max(player_club_id, date) AS last_club_id
  FROM club_apps GROUP BY player_id
)
SELECT s.player_id, s.appearances, s.last_seen,
       s.last_club_id, c.name AS last_club_name
FROM s JOIN clubs c ON c.club_id = s.last_club_id
""")
print("player_status", con.execute("SELECT COUNT(*) FROM player_status").fetchone()[0])
con.close()
print("wrote", out)
