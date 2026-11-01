"""Football Oracle backend — Flask middleware.

POST /api/ask  {question, model?} -> {answer, sql, rows, model, latency_ms, cached}
GET  /api/schema -> {table: [columns]}
GET  /healthz   -> {status, tables}

Pipeline: Redis exact-match cache -> LiteLLM NL-to-SQL -> sqlglot
grounding check (tables/columns must exist, SELECT only) -> read-only
DuckDB with timeout -> LiteLLM rows-to-answer -> cache + return.

MOCK_LLM=1 skips LiteLLM with canned responses for plumbing tests.
"""
import hashlib
import json
import os
import queue
import threading
import time

import duckdb
import httpx
import sqlglot
import sqlglot.expressions as exp
from flask import Flask, Response, jsonify, request, send_from_directory

DUCKDB_PATH = os.environ.get(
    "DUCKDB_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "football.duckdb")),
)
LITELLM_URL = os.environ.get("LITELLM_URL", "http://localhost:4000")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-oracle-local")
REDIS_URL = os.environ.get("REDIS_URL", "")
MOCK_LLM = os.environ.get("MOCK_LLM", "") == "1"
APP_API_KEY = os.environ.get("APP_API_KEY", "")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "beam-free")
QUERY_TIMEOUT_S = 30
ROW_LIMIT = 50
# Bump on any behavior change (prompt, grounding, summarizer): old entries
# keyed under a previous version stop matching instead of serving stale logic.
PIPELINE_VERSION = "v20"
SUMMARY_ROW_CAP = 20
CACHE_TTL_S = 3600

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

FRONTEND_DIST = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
)

_schema = None
_counts = None
_coverage = None
_redis = None


def schema():
    global _schema
    if _schema is None:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        _schema = {
            t[0]: [c[1] for c in con.execute(f"PRAGMA table_info('{t[0]}')").fetchall()]
            for t in con.execute("SHOW TABLES").fetchall()
        }
        con.close()
    return _schema


def coverage():
    """Compact competition coverage string, built once from the live DB."""
    global _coverage
    if _coverage is None:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        rows = con.execute(
            "SELECT competition_id, name, country_name FROM competitions ORDER BY name"
        ).fetchall()
        con.close()
        _coverage = "; ".join(f"{c} {n} ({ct or 'intl'})" for c, n, ct in rows)
    return _coverage


def redis_client():
    global _redis
    if _redis is None and REDIS_URL:
        import redis

        try:
            _redis = redis.from_url(REDIS_URL, socket_connect_timeout=2)
            _redis.ping()
        except Exception:
            _redis = False
    return _redis or None


def cache_key(question, history=None, model=""):
    h = hashlib.sha1(
        json.dumps([model, history or []], sort_keys=True).encode()
    ).hexdigest()[:12]
    return f"ask:{PIPELINE_VERSION}:{h}:" + " ".join(question.lower().split())


FALLBACK_MODEL = "deepseek-v4.1-flash"


def llm_chat(model, messages):
    """One chat completion via LiteLLM, with one escalation on failure.

    Tries the requested model first, then the strong model. Returns
    (content, model_that_answered). Raises on total failure.
    """
    errors = []
    for m in dict.fromkeys([model, FALLBACK_MODEL]):
        try:
            if MOCK_LLM:
                user_text = messages[-1]["content"]
                if "Rows (" in user_text:
                    return f"Mock answer for: {user_text[:80]}", m
                return ("SELECT player_name, SUM(goals) AS g FROM appearances "
                        "GROUP BY player_name ORDER BY g DESC LIMIT 5"), m
            r = httpx.post(
                f"{LITELLM_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                json={"model": m, "messages": messages, "temperature": 0},
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"].get("content")
            if content and content.strip():
                return content, data.get("model", m)
            errors.append(f"{m}: empty response")
        except Exception as e:
            errors.append(f"{m}: {e}")
    raise RuntimeError("; ".join(errors))


NL2SQL_SYSTEM = (
    "You translate football questions to DuckDB SQL. Schema:\n{schema}\n"
    "Data coverage: games and game_events 2006-2026, appearances 2012-2026, "
    "player_valuations 2000-2026, transfers 1993-2030. Questions outside a "
    "table's range legitimately return no rows.\n"
    "Covered competitions:\n{coverage}\n"
    "If the question names a country, competition, or club outside this list, "
    "do NOT guess. Reply with exactly: NOT_COVERED: <one sentence saying what is missing>.\n"
    "If the question is not about football at all, reply with exactly: "
    "NOT_COVERED: I only answer football questions.\n"
    "Season (games.season) is an integer year the season started: 2025 means "
    "the 2025/26 season. Never write it as a 'YYYY/YYYY' string.\n"
    "A bare year in the question usually means the club season ending in that "
    "year: use season = year - 1 for leagues ('La Liga 2026' -> season 2025). "
    "One-off summer tournaments (World Cup, Euros) are tagged with the prior "
    "year's season (the July 2014 final is season 2013) — filter those by date "
    "(June–July YYYY), not by season.\n"
    "Rules: SELECT queries only, one statement, no semicolons inside. "
    "Quote \" minute\" (it has a leading space in game_events). "
    "Return informative result sets: for superlatives ('most', 'best', 'top') "
    "default to the top 5-10 rows; LIMIT 1 only when exactly one is asked for. "
    "Alias computed columns with readable names ('Goals per 90'). "
    "For a player's clubs, use transfers and appearances history "
    "(from_club/to_club, player_club_id per season) — players.current_club_name "
    "is only the current club and goes stale for departed or retired players. "
    "For current-squad questions (squad value, 'currently plays for', "
    "least/most valuable at a club), use player_status (last_club_name, "
    "last_seen) instead: it derives the last club from dated club appearances. "
    "If a player is missing from player_status, there is no appearance record "
    "at all — say so and do not trust the players-table club label. "
    "A LEFT JOIN that yields NULL last_club_name/last_seen means the same "
    "thing: never evidenced at any club, so answer No to 'currently plays for X'. "
    "If last_seen is 2+ seasons old, say the listing looks stale. "
    "This applies to bio/profile questions too ('who is X'): verify the club "
    "against player_status and never present a stale current_club_name as "
    "current fact. "
    "For filtering by competition, club, or player, use the _id columns "
    "(competition_id, club_id, player_id): they match the foreign keys in "
    "games/appearances/transfers. The _code columns are URL slugs with "
    "different values — never compare them to short ids. "
    "Reply with SQL only, no markdown, no explanation."
)

ANSWER_SYSTEM = (
    "Answer the user's football question in a few plain sentences, "
    "using ONLY the rows below. For rankings or lists, name each entry "
    "with its value instead of only the winner. If the rows are empty, "
    "say so plainly. "
    "If the question also asks for something unanswerable from football "
    "data (recipes, general knowledge), answer the football part and say "
    "plainly you can't do the rest."
)


def ground_sql(sql):
    """Parse + validate. Returns (clean_sql, error)."""
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except Exception as e:
        return None, f"parse error: {e}"
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return None, "only single SELECT statements allowed"
    stmt = statements[0]
    sch = schema()
    ctes = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    for cond in list(stmt.find_all(exp.EQ)) + list(stmt.find_all(exp.NEQ)) + list(stmt.find_all(exp.Like)):
        for col in cond.find_all(exp.Column):
            if col.name.lower().endswith("_code"):
                return None, (
                    f"filter on slug column '{col.name}' — "
                    f"use the _id column instead"
                )
    tables = {t.name.lower() for t in stmt.find_all(exp.Table)}
    unknown_tables = tables - {k.lower() for k in sch} - ctes
    if unknown_tables:
        return None, f"unknown tables: {sorted(unknown_tables)}"
    cols = set()
    for c in stmt.find_all(exp.Column):
        if c.name != "*":
            cols.add((c.table.lower(), c.name.lower()))
    by_table = {}
    for t in stmt.find_all(exp.Table):
        alias = (t.alias_or_name or t.name).lower()
        if t.name.lower() in ctes:
            by_table[alias] = None  # CTE: columns not validatable against the schema
            continue
        by_table[alias] = {c.lower() for c in sch.get(t.name, sch.get(t.name.lower(), []))}
    bad = [
        f"{t}.{c}" if t else c
        for t, c in cols
        if t and by_table.get(t) is not None and c not in by_table[t]
    ]
    if bad:
        return None, f"unknown columns: {bad}"
    return stmt.sql(dialect="duckdb"), None


def run_sql(sql):
    """Execute on a read-only connection with a hard timeout."""
    out = {}

    def _run():
        try:
            con = duckdb.connect(DUCKDB_PATH, read_only=True)
            out["rows"] = con.execute(sql).fetchall()
            out["names"] = [d[0] for d in con.description]
            con.close()
        except Exception as e:
            out["error"] = str(e)

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(QUERY_TIMEOUT_S)
    if th.is_alive():
        return None, None, "query timeout"
    if "error" in out:
        return None, None, out["error"]
    return out["names"], out["rows"], None


@app.before_request
def gate():
    if request.method == "OPTIONS":
        return "", 204
    if request.path.startswith("/api/"):
        if APP_API_KEY and request.headers.get("X-Api-Key") != APP_API_KEY:
            return jsonify({"error": "unauthorized"}), 401


@app.get("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(os.path.join(FRONTEND_DIST, "assets"), filename)


@app.get("/")
def index():
    if os.path.exists(os.path.join(FRONTEND_DIST, "index.html")):
        return send_from_directory(FRONTEND_DIST, "index.html")
    return (
        "Frontend not built. Run: cd frontend && npm ci && npm run build",
        404,
    )


@app.get("/api/models")
def api_models():
    try:
        r = httpx.get(
            f"{LITELLM_URL}/v1/models",
            headers={"Authorization": f"Bearer {LITELLM_KEY}"},
            timeout=15,
        )
        r.raise_for_status()
        return jsonify([m["id"] for m in r.json().get("data", [])])
    except Exception as e:
        return jsonify({"error": str(e), "fallback": [DEFAULT_MODEL]}), 502


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "tables": sorted(schema()), "mock": MOCK_LLM})


@app.get("/api/schema")
def api_schema():
    return jsonify(schema())


def table_counts():
    global _counts
    if _counts is None:
        con = duckdb.connect(DUCKDB_PATH, read_only=True)
        _counts = {
            t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for t in schema()
        }
        con.close()
    return _counts


@app.get("/api/schema/full")
def api_schema_full():
    sch = schema()
    cnt = table_counts()
    return jsonify({t: {"columns": cols, "count": cnt[t]} for t, cols in sch.items()})


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def pipeline(question, model, emit=None, history=None):
    """Shared NL-to-answer pipeline. emit(event, data) streams progress.

    history: recent [{question, sql}] turns used to resolve follow-up
    references ('he', 'that club'). Part of the cache key.

    Returns (response_dict, error_dict, http_status). error_dict is None
    on success.
    """
    def say(event, data):
        if emit:
            emit(event, data)

    key = cache_key(question, history, model)
    t0p = time.time()
    marks = {}

    def mark(name):
        marks[name] = int((time.time() - t0p) * 1000)
    r = redis_client()
    if r:
        try:
            hit = r.get(key)
            if hit:
                mark("cache")
                d = json.loads(hit)
                d["cached"] = True
                d["timings"] = {"cache_ms": marks["cache"]}
                say("stage", "Cache hit — no LLM calls needed.")
                return d, None, 200
        except Exception:
            pass
    mark("cache")
    say("stage", "Cache miss — translating to SQL…")

    schema_txt = "\n".join(f"{t}({', '.join(c)})" for t, c in schema().items())
    hist_txt = ""
    if history:
        pairs = "\n".join(
            f"Q: {h.get('question')}\nSQL: {h.get('sql')}"
            for h in history[-5:] if h.get("sql")
        )
        if pairs:
            hist_txt = (
                "Earlier in this conversation "
                "(resolve 'he/she/it/that club/them' against this):\n"
                + pairs + "\n"
            )
    try:
        sql_raw, _ = llm_chat(model, [
            {"role": "system", "content": NL2SQL_SYSTEM.format(schema=schema_txt, coverage=coverage())},
            {"role": "user", "content": f"{hist_txt}Question: {question}"},
        ])
    except Exception as e:
        return None, {"error": f"nl-to-sql failed: {e}"}, 502
    mark("nl2sql")
    if not sql_raw or not sql_raw.strip():
        return None, {"error": "model returned an empty response, try again"}, 502
    if sql_raw.strip().startswith("NOT_COVERED:"):
        reason = sql_raw.strip()[len("NOT_COVERED:"):].strip()
        resp = {
            "answer": f"{reason} This dataset covers 65 competitions — see 'What can I ask?' below for the full list.",
            "sql": "-- not covered by this dataset",
            "rows": [],
            "model": model,
            "cached": False,
            "timings": {
                "cache_ms": marks.get("cache", 0),
                "nl2sql_ms": marks.get("nl2sql", 0) - marks.get("cache", 0),
            },
        }
        if r:
            try:
                r.setex(key, CACHE_TTL_S, json.dumps(resp, default=str))
            except Exception:
                pass
        return resp, None, 200
    say("sql", sql_raw)

    sql, err = ground_sql(sql_raw)
    if err:
        return None, {"error": err, "sql": sql_raw}, 422
    mark("ground")
    say("stage", "SQL checks out — running query…")

    names, rows, err = run_sql(sql)
    if err:
        return None, {"error": err, "sql": sql}, 422
    rows = rows[:ROW_LIMIT]
    mark("sql")
    say("stage", f"Got {len(rows)} rows — writing answer…")

    summary_rows = [dict(zip(names, r)) for r in rows[:SUMMARY_ROW_CAP]]
    try:
        answer, used_model = llm_chat(model, [
            {"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user", "content": (
                f"Question: {question}\nRows ({len(rows)} total, showing {len(summary_rows)}):\n"
                + json.dumps(summary_rows, default=str))},
        ])
    except Exception as e:
        return None, {"error": f"summarizer failed: {e}", "sql": sql}, 502

    resp = {
        "answer": answer.strip(),
        "sql": sql,
        "rows": [dict(zip(names, r)) for r in rows],
        "model": used_model,
        "cached": False,
        "timings": {
            "cache_ms": marks.get("cache", 0),
            "nl2sql_ms": marks.get("nl2sql", 0) - marks.get("cache", 0),
            "ground_ms": marks.get("ground", 0) - marks.get("nl2sql", 0),
            "sql_ms": marks.get("sql", 0) - marks.get("ground", 0),
            "summarize_ms": int((time.time() - t0p) * 1000) - marks.get("sql", 0),
        },
    }
    if r:
        try:
            r.setex(key, CACHE_TTL_S, json.dumps(resp, default=str))
        except Exception:
            pass
    return resp, None, 200


@app.post("/api/ask")
def ask():
    t0 = time.time()
    body = request.get_json(force=True)
    question = (body.get("question") or "").strip()
    model = body.get("model") or DEFAULT_MODEL
    history = body.get("history") or []
    if not question:
        return jsonify({"error": "empty question"}), 400

    resp, err, status = pipeline(question, model, history=history)
    if err:
        return jsonify(err), status
    resp["latency_ms"] = int((time.time() - t0) * 1000)
    return jsonify(resp)


@app.post("/api/ask/stream")
def ask_stream():
    body = request.get_json(force=True)
    question = (body.get("question") or "").strip()
    model = body.get("model") or DEFAULT_MODEL
    history = body.get("history") or []
    if not question:
        return jsonify({"error": "empty question"}), 400

    def gen():
        t0 = time.time()
        q = queue.Queue()
        done = threading.Event()
        outcome = {}

        def run():
            resp, err, _ = pipeline(question, model, emit, history)
            outcome["resp"] = resp
            outcome["err"] = err
            done.set()

        def emit(event, data):
            q.put(sse(event, data))

        threading.Thread(target=run, daemon=True).start()
        while not done.is_set():
            try:
                yield q.get(timeout=0.2)
            except queue.Empty:
                yield ": ping\n\n"
        while not q.empty():
            yield q.get_nowait()
        if outcome.get("err"):
            yield sse("error", outcome["err"])
        else:
            outcome["resp"]["latency_ms"] = int((time.time() - t0) * 1000)
            yield sse("result", outcome["resp"])

    return Response(gen(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8000,
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )
