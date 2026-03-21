"""
AutoEQ MCP Server
==================
MCP server for the AutoEQ project (github.com/jaakkopasanen/AutoEq).
Indexes 8800+ headphone/IEM frequency response measurements into a SQLite database
and provides tools for searching, comparing, and getting EQ settings with
AI-friendly sound signature analysis.

Tools:
  - eq_search    : Search headphones/IEMs by name, type, or sound signature
  - eq_profile   : Get detailed EQ profile with per-band sound analysis
  - eq_compare   : Compare two headphones band-by-band
  - eq_recommend : Get recommendations by sound preference
  - eq_ranking   : Harman preference score rankings
  - eq_targets   : List available target curves
  - eq_sync      : Manually trigger database sync
"""

import csv
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from pydantic import Field
from mcp.server.fastmcp import FastMCP

# ── Paths (configurable via env vars) ────────────────────────────────────────
DATA_DIR = Path(os.getenv("AUTOEQ_DATA_DIR", Path.home() / ".autoeq-mcp"))
REPO_DIR = DATA_DIR / "autoeq_repo"
DB_PATH = DATA_DIR / "autoeq.db"
RESULTS_DIR = REPO_DIR / "results"
TARGETS_DIR = REPO_DIR / "targets"
REPO_URL = "https://github.com/jaakkopasanen/AutoEq.git"

# ── Frequency band definitions (Hz) ─────────────────────────────────────────
FREQ_BANDS = {
    "sub_bass":   (20, 60),
    "bass":       (60, 250),
    "low_mid":    (250, 500),
    "mid":        (500, 1000),
    "upper_mid":  (1000, 2000),
    "presence":   (2000, 4000),
    "brilliance": (4000, 8000),
    "air":        (8000, 20000),
}

BAND_LABELS = {
    "sub_bass":   "Sub-bass (20-60Hz)",
    "bass":       "Bass (60-250Hz)",
    "low_mid":    "Low-mid (250-500Hz)",
    "mid":        "Mid (500-1kHz)",
    "upper_mid":  "Upper-mid (1k-2kHz)",
    "presence":   "Presence (2k-4kHz)",
    "brilliance": "Brilliance (4k-8kHz)",
    "air":        "Air (8k-20kHz)",
}

SIGNATURE_THRESHOLDS = {
    "bass_boost": 2.0,
    "bass_cut": -2.0,
    "treble_boost": 2.0,
    "treble_cut": -2.0,
    "mid_cut": -1.5,
}


# ── Database ─────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS headphones (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            coupler TEXT DEFAULT '',
            form_factor TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            score REAL,
            std_db REAL,
            slope REAL,
            signature TEXT DEFAULT '',
            sub_bass_avg REAL, bass_avg REAL, low_mid_avg REAL, mid_avg REAL,
            upper_mid_avg REAL, presence_avg REAL, brilliance_avg REAL, air_avg REAL
        );
        CREATE TABLE IF NOT EXISTS parametric_eq (
            id INTEGER PRIMARY KEY,
            headphone_id INTEGER NOT NULL REFERENCES headphones(id) ON DELETE CASCADE,
            preamp_db REAL NOT NULL DEFAULT 0,
            filter_num INTEGER NOT NULL,
            filter_type TEXT NOT NULL,
            fc_hz REAL NOT NULL,
            q REAL NOT NULL,
            gain_db REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fixed_band_eq (
            id INTEGER PRIMARY KEY,
            headphone_id INTEGER NOT NULL REFERENCES headphones(id) ON DELETE CASCADE,
            preamp_db REAL NOT NULL DEFAULT 0,
            filter_num INTEGER NOT NULL,
            fc_hz REAL NOT NULL,
            q REAL NOT NULL,
            gain_db REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_hp_name ON headphones(name);
        CREATE INDEX IF NOT EXISTS idx_hp_source ON headphones(source);
        CREATE INDEX IF NOT EXISTS idx_hp_form ON headphones(form_factor);
        CREATE INDEX IF NOT EXISTS idx_hp_sig ON headphones(signature);
        CREATE INDEX IF NOT EXISTS idx_hp_score ON headphones(score);
        CREATE INDEX IF NOT EXISTS idx_peq_hp ON parametric_eq(headphone_id);
        CREATE INDEX IF NOT EXISTS idx_fbeq_hp ON fixed_band_eq(headphone_id);
    """)
    conn.commit()
    conn.close()


# ── Parsing ──────────────────────────────────────────────────────────────────
def parse_form_factor(subfolder: str) -> tuple[str, str]:
    """Extract (coupler, form_factor) from subfolder name."""
    subfolder = subfolder.strip()
    for ff in ("over-ear", "in-ear", "earbud"):
        if subfolder.endswith(ff):
            coupler = subfolder[: -len(ff)].strip()
            return coupler, ff
    return "", subfolder


def parse_index(index_path: Path) -> list[dict]:
    """Parse INDEX.md into a list of headphone entries."""
    entries = []
    pattern = re.compile(
        r"^- \[(.+?)\]\(\./(.+?)/(.+?)/(.+?)\)\s+by\s+(.+?)(?:\s+on\s+(.+))?$"
    )
    with open(index_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            m = pattern.match(line)
            if not m:
                continue
            name = m.group(1)
            source = unquote(m.group(2))
            subfolder = unquote(m.group(3))
            coupler, form_factor = parse_form_factor(subfolder)
            rel_path = f"{source}/{subfolder}/{unquote(m.group(4))}"
            entries.append({
                "name": name,
                "source": source,
                "coupler": coupler,
                "form_factor": form_factor,
                "path": rel_path,
            })
    return entries


def parse_ranking(ranking_path: Path) -> dict[str, dict]:
    """Parse RANKING.md into {path: {score, std, slope}}."""
    rankings = {}
    row_pattern = re.compile(
        r"\|\s*\[(.+?)\]\(\./(.+?)\)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([-\d.]+)\s*\|"
    )
    with open(ranking_path, "r", encoding="utf-8") as f:
        for line in f:
            m = row_pattern.search(line)
            if not m:
                continue
            path = unquote(m.group(2))
            rankings[path] = {
                "score": float(m.group(3)),
                "std": float(m.group(4)),
                "slope": float(m.group(5)),
            }
    return rankings


def parse_parametric_eq(filepath: Path) -> tuple[float, list[dict]]:
    """Parse ParametricEQ.txt → (preamp, filters)."""
    if not filepath.exists():
        return 0.0, []
    preamp = 0.0
    filters = []
    type_map = {"PK": "Peaking", "LSC": "LowShelf", "HSC": "HighShelf"}
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("Preamp:"):
                preamp = float(re.search(r"[-\d.]+", line).group())
            elif line.startswith("Filter"):
                m = re.match(
                    r"Filter\s+(\d+):\s+ON\s+(\w+)\s+Fc\s+([\d.]+)\s+Hz\s+Gain\s+([-\d.]+)\s+dB\s+Q\s+([\d.]+)",
                    line,
                )
                if m:
                    filters.append({
                        "num": int(m.group(1)),
                        "type": type_map.get(m.group(2), m.group(2)),
                        "fc": float(m.group(3)),
                        "gain": float(m.group(4)),
                        "q": float(m.group(5)),
                    })
    return preamp, filters


def parse_fixed_band_eq(filepath: Path) -> tuple[float, list[dict]]:
    """Parse FixedBandEQ.txt → (preamp, filters)."""
    if not filepath.exists():
        return 0.0, []
    preamp = 0.0
    filters = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("Preamp:"):
                preamp = float(re.search(r"[-\d.]+", line).group())
            elif line.startswith("Filter"):
                m = re.match(
                    r"Filter\s+(\d+):\s+ON\s+\w+\s+Fc\s+([\d.]+)\s+Hz\s+Gain\s+([-\d.]+)\s+dB\s+Q\s+([\d.]+)",
                    line,
                )
                if m:
                    filters.append({
                        "num": int(m.group(1)),
                        "fc": float(m.group(2)),
                        "gain": float(m.group(3)),
                        "q": float(m.group(4)),
                    })
    return preamp, filters


def compute_signature(csv_path: Path) -> dict:
    """Compute per-band average error from CSV and classify sound signature."""
    result = {band: None for band in FREQ_BANDS}
    result["signature"] = ""

    if not csv_path.exists():
        return result

    band_values = {band: [] for band in FREQ_BANDS}
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if "error" not in (reader.fieldnames or []):
                return result
            for row in reader:
                try:
                    freq = float(row["frequency"])
                    error = float(row["error"])
                except (ValueError, KeyError):
                    continue
                for band, (lo, hi) in FREQ_BANDS.items():
                    if lo <= freq < hi:
                        band_values[band].append(error)
                        break
    except Exception:
        return result

    for band, values in band_values.items():
        if values:
            result[band] = round(sum(values) / len(values), 2)

    bass_avg = _avg(result["sub_bass"], result["bass"])
    mid_avg = _avg(result["low_mid"], result["mid"], result["upper_mid"])
    treble_avg = _avg(result["presence"], result["brilliance"], result["air"])

    if bass_avg is None or mid_avg is None or treble_avg is None:
        return result

    signatures = []
    th = SIGNATURE_THRESHOLDS

    if bass_avg > th["bass_boost"] and treble_avg > th["treble_boost"] and mid_avg < th["mid_cut"]:
        signatures.append("V-shaped")
    elif bass_avg > th["bass_boost"] and treble_avg > th["treble_boost"]:
        signatures.append("U-shaped")
    elif bass_avg > th["bass_boost"] and treble_avg < 1.0:
        signatures.append("Warm")
    elif treble_avg > th["treble_boost"] and bass_avg < 1.0:
        signatures.append("Bright")
    elif treble_avg < th["treble_cut"]:
        signatures.append("Dark")
    elif abs(bass_avg) < 2.0 and abs(mid_avg) < 2.0 and abs(treble_avg) < 2.0:
        signatures.append("Neutral")
    elif bass_avg > 3.0:
        signatures.append("Bass-heavy")
    elif mid_avg > 2.0 and bass_avg < 1.0 and treble_avg < 1.0:
        signatures.append("Mid-forward")
    else:
        signatures.append("Neutral")

    total_deviation = sum(abs(v) for v in [bass_avg, mid_avg, treble_avg]) / 3
    if total_deviation < 1.5:
        signatures.append("Harman-like")

    result["signature"] = ", ".join(signatures)
    return result


def _avg(*values) -> Optional[float]:
    valid = [v for v in values if v is not None]
    return sum(valid) / len(valid) if valid else None


# ── Sync ─────────────────────────────────────────────────────────────────────
def sync_db(progress_callback=None):
    """Clone/pull AutoEQ repo and rebuild SQLite database."""
    start = time.time()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Clone or pull
    if (REPO_DIR / ".git").exists():
        if progress_callback:
            progress_callback("Pulling latest changes...")
        subprocess.run(
            ["git", "-C", str(REPO_DIR), "pull", "--ff-only"],
            capture_output=True, timeout=120,
        )
    else:
        if progress_callback:
            progress_callback("Cloning AutoEQ repository (this may take a few minutes)...")
        subprocess.run(
            ["git", "clone", "--depth", "1", REPO_URL, str(REPO_DIR)],
            capture_output=True, timeout=600,
        )

    if not RESULTS_DIR.exists():
        return "Error: AutoEQ repository not found. Run with --sync first."

    init_db()
    conn = get_db()

    # Targets
    conn.execute("DELETE FROM targets")
    for f in sorted(TARGETS_DIR.glob("*.csv")):
        conn.execute("INSERT INTO targets (name, filename) VALUES (?, ?)", (f.stem, f.name))

    # INDEX.md
    entries = parse_index(RESULTS_DIR / "INDEX.md")
    if progress_callback:
        progress_callback(f"Parsed INDEX.md: {len(entries)} entries")

    # RANKING.md
    rankings = parse_ranking(RESULTS_DIR / "RANKING.md")
    if progress_callback:
        progress_callback(f"Parsed RANKING.md: {len(rankings)} rankings")

    # Reset
    conn.execute("DELETE FROM fixed_band_eq")
    conn.execute("DELETE FROM parametric_eq")
    conn.execute("DELETE FROM headphones")

    count = 0
    errors = 0
    for entry in entries:
        try:
            hp_dir = RESULTS_DIR / entry["path"]
            if not hp_dir.is_dir():
                errors += 1
                continue

            peq_files = list(hp_dir.glob("*ParametricEQ.txt"))
            preamp_peq, peq_filters = parse_parametric_eq(peq_files[0]) if peq_files else (0.0, [])

            fbeq_files = list(hp_dir.glob("*FixedBandEQ.txt"))
            preamp_fb, fb_filters = parse_fixed_band_eq(fbeq_files[0]) if fbeq_files else (0.0, [])

            csv_files = list(hp_dir.glob("*.csv"))
            sig = compute_signature(csv_files[0]) if csv_files else {b: None for b in FREQ_BANDS}
            sig.setdefault("signature", "")

            rank = rankings.get(entry["path"], {})

            cur = conn.execute(
                """INSERT INTO headphones
                   (name, source, coupler, form_factor, path, score, std_db, slope,
                    signature, sub_bass_avg, bass_avg, low_mid_avg, mid_avg,
                    upper_mid_avg, presence_avg, brilliance_avg, air_avg)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry["name"], entry["source"], entry["coupler"],
                    entry["form_factor"], entry["path"],
                    rank.get("score"), rank.get("std"), rank.get("slope"),
                    sig.get("signature", ""),
                    sig.get("sub_bass"), sig.get("bass"), sig.get("low_mid"),
                    sig.get("mid"), sig.get("upper_mid"), sig.get("presence"),
                    sig.get("brilliance"), sig.get("air"),
                ),
            )
            hp_id = cur.lastrowid

            for flt in peq_filters:
                conn.execute(
                    """INSERT INTO parametric_eq
                       (headphone_id, preamp_db, filter_num, filter_type, fc_hz, q, gain_db)
                       VALUES (?,?,?,?,?,?,?)""",
                    (hp_id, preamp_peq, flt["num"], flt["type"], flt["fc"], flt["q"], flt["gain"]),
                )

            for flt in fb_filters:
                conn.execute(
                    """INSERT INTO fixed_band_eq
                       (headphone_id, preamp_db, filter_num, fc_hz, q, gain_db)
                       VALUES (?,?,?,?,?,?)""",
                    (hp_id, preamp_fb, flt["num"], flt["fc"], flt["q"], flt["gain"]),
                )

            count += 1
            if progress_callback and count % 500 == 0:
                progress_callback(f"  {count}/{len(entries)} processed...")

        except Exception:
            errors += 1
            continue

    conn.execute(
        "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('last_sync', ?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    conn.commit()
    conn.close()

    elapsed = time.time() - start
    return f"Sync complete: {count} headphones indexed, {errors} errors, {elapsed:.1f}s"


# ── Formatters ───────────────────────────────────────────────────────────────
def format_profile(row: sqlite3.Row, peq_rows: list, fbeq_rows: list) -> str:
    """Format a headphone profile as readable text."""
    lines = []
    lines.append(f"# {row['name']}")
    lines.append(f"- Source: {row['source']}")
    if row["coupler"]:
        lines.append(f"- Coupler: {row['coupler']}")
    lines.append(f"- Type: {row['form_factor']}")

    if row["score"]:
        lines.append(f"- Harman preference score: {row['score']}")
        lines.append(f"- Standard deviation: {row['std_db']} dB")
        slope_desc = "warm" if (row["slope"] or 0) < 0 else "bright"
        lines.append(f"- Slope: {row['slope']} ({slope_desc} tendency)")

    if row["signature"]:
        lines.append(f"- Sound signature: {row['signature']}")

    lines.append("\n## Per-band analysis (deviation from target, dB)")
    for band, label in BAND_LABELS.items():
        col = f"{band}_avg"
        val = row[col]
        if val is not None:
            bar = _bar(val)
            desc = _describe_band(band, val)
            lines.append(f"  {label}: {val:+.1f} dB {bar} {desc}")

    if peq_rows:
        preamp = peq_rows[0]["preamp_db"]
        lines.append(f"\n## Parametric EQ (Preamp: {preamp} dB)")
        lines.append(f"{'#':>3}  {'Type':<10} {'Fc (Hz)':>8}  {'Q':>5}  {'Gain (dB)':>9}")
        lines.append(f"{'─'*3}  {'─'*10} {'─'*8}  {'─'*5}  {'─'*9}")
        for r in peq_rows:
            lines.append(
                f"{r['filter_num']:>3}  {r['filter_type']:<10} {r['fc_hz']:>8.0f}  "
                f"{r['q']:>5.2f}  {r['gain_db']:>+9.1f}"
            )

    if fbeq_rows:
        preamp = fbeq_rows[0]["preamp_db"]
        lines.append(f"\n## Fixed Band EQ (Preamp: {preamp} dB)")
        lines.append(f"{'#':>3}  {'Fc (Hz)':>8}  {'Q':>5}  {'Gain (dB)':>9}")
        lines.append(f"{'─'*3}  {'─'*8}  {'─'*5}  {'─'*9}")
        for r in fbeq_rows:
            lines.append(
                f"{r['filter_num']:>3}  {r['fc_hz']:>8.0f}  "
                f"{r['q']:>5.2f}  {r['gain_db']:>+9.1f}"
            )

    return "\n".join(lines)


def _bar(val: float) -> str:
    if val is None:
        return ""
    clamped = max(-10, min(10, val))
    mid = 10
    pos = int(mid + clamped)
    bar = ["·"] * 21
    bar[mid] = "|"
    if pos < mid:
        for i in range(pos, mid):
            bar[i] = "▓"
    elif pos > mid:
        for i in range(mid + 1, pos + 1):
            bar[i] = "▓"
    return "[" + "".join(bar) + "]"


def _describe_band(band: str, val: float) -> str:
    if abs(val) < 1.0:
        return "close to target"
    descriptions = {
        "sub_bass": ("deep bass emphasis", "sub-bass lacking"),
        "bass": ("full/rich bass", "lean/thin bass"),
        "low_mid": ("thick lower mids", "thin lower mids"),
        "mid": ("prominent mids", "recessed mids"),
        "upper_mid": ("forward vocals / aggressive", "recessed vocals"),
        "presence": ("detail emphasis / potential fatigue", "smooth detail"),
        "brilliance": ("bright detail", "rolled-off detail"),
        "air": ("airy / open", "closed / lacking air"),
    }
    pos, neg = descriptions.get(band, ("elevated", "recessed"))
    return pos if val > 0 else neg


def format_comparison(row1, row2) -> str:
    lines = []
    lines.append(f"# Comparison: {row1['name']} vs {row2['name']}")
    lines.append(f"  Source: {row1['source']} vs {row2['source']}")

    if row1["score"] or row2["score"]:
        s1 = f"{row1['score']}" if row1["score"] else "N/A"
        s2 = f"{row2['score']}" if row2["score"] else "N/A"
        lines.append(f"  Harman score: {s1} vs {s2}")

    lines.append(f"  Signature: {row1['signature'] or 'N/A'} vs {row2['signature'] or 'N/A'}")

    lines.append(f"\n## Per-band comparison (deviation from target, dB)")
    lines.append(f"  {'Band':<25} {'Model 1':>8} {'Model 2':>8} {'Diff':>8}")
    lines.append(f"  {'─'*25} {'─'*8} {'─'*8} {'─'*8}")
    for band, label in BAND_LABELS.items():
        col = f"{band}_avg"
        v1 = row1[col]
        v2 = row2[col]
        if v1 is not None and v2 is not None:
            diff = v1 - v2
            lines.append(f"  {label:<25} {v1:>+8.1f} {v2:>+8.1f} {diff:>+8.1f}")
        else:
            lines.append(f"  {label:<25} {'N/A':>8} {'N/A':>8} {'N/A':>8}")

    lines.append("\n## Summary")
    for row in [row1, row2]:
        sig = row["signature"] or "unclassified"
        lines.append(f"- **{row['name']}**: {sig}")

    return "\n".join(lines)


# ── Search Helper ────────────────────────────────────────────────────────────
def _find_headphone(conn: sqlite3.Connection, name: str, source: str = "") -> Optional[sqlite3.Row]:
    """Find a headphone by name with smart matching.

    Priority:
    1. Exact match (name = ?)
    2. Word-boundary match ("E500" → "Final Audio E500", not "ME500")
    3. Substring match (LIKE %name%)
    4. Flexible match (letter-digit boundary: "HD650" → "HD 650")
    5. Multi-word AND match (fallback)
    Within each level, shorter names (more precise) and higher scores come first.
    """
    source_cond = " AND source LIKE ?" if source else ""
    source_params = [f"%{source}%"] if source else []

    # 1: Exact match
    rows = conn.execute(
        f"SELECT * FROM headphones WHERE name = ?{source_cond} ORDER BY score DESC NULLS LAST LIMIT 1",
        [name] + source_params,
    ).fetchall()
    if rows:
        return rows[0]

    # 2: Word-boundary match
    rows = conn.execute(
        f"""SELECT * FROM headphones
            WHERE (name LIKE ? OR name LIKE ? OR name LIKE ?){source_cond}
            ORDER BY LENGTH(name) ASC, score DESC NULLS LAST LIMIT 1""",
        [f"% {name}", f"% {name} %", name] + source_params,
    ).fetchall()
    if rows:
        return rows[0]

    # 3: Substring match
    rows = conn.execute(
        f"""SELECT * FROM headphones WHERE name LIKE ?{source_cond}
            ORDER BY LENGTH(name) ASC, score DESC NULLS LAST LIMIT 1""",
        [f"%{name}%"] + source_params,
    ).fetchall()
    if rows:
        return rows[0]

    # 4: Flexible match (letter→digit boundary allows optional space/hyphen)
    flexible = re.sub(r'([A-Za-z])(\d)', r'\1%\2', name)
    if flexible != name:
        rows = conn.execute(
            f"""SELECT * FROM headphones WHERE name LIKE ?{source_cond}
                ORDER BY LENGTH(name) ASC, score DESC NULLS LAST LIMIT 1""",
            [f"%{flexible}%"] + source_params,
        ).fetchall()
        if rows:
            return rows[0]

    # 5: Multi-word AND match
    words = name.split()
    if len(words) > 1:
        like_parts = " AND ".join(["name LIKE ?"] * len(words))
        params = [f"%{w}%" for w in words] + source_params
        rows = conn.execute(
            f"""SELECT * FROM headphones WHERE {like_parts}{source_cond}
                ORDER BY LENGTH(name) ASC, score DESC NULLS LAST LIMIT 1""",
            params,
        ).fetchall()
        if rows:
            return rows[0]

    return None


# ── Server ───────────────────────────────────────────────────────────────────
mcp_server = FastMCP("autoeq_mcp")


@mcp_server.tool(
    name="eq_search",
    annotations={
        "title": "Search headphones/IEMs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def eq_search(
    query: str = Field(default="", description="Search term (model name, brand, etc.)"),
    form_factor: str = Field(
        default="",
        description="Type filter: over-ear, in-ear, earbud",
    ),
    signature: str = Field(
        default="",
        description="Sound signature filter: Neutral, Warm, Bright, Dark, V-shaped, U-shaped, Bass-heavy, Mid-forward, Harman-like",
    ),
    source: str = Field(
        default="",
        description="Measurement source filter: oratory1990, crinacle, Rtings, etc.",
    ),
    limit: int = Field(default=20, description="Max results (up to 50)"),
) -> str:
    """Search the AutoEQ database for headphones/IEMs. Filter by name, type, sound signature, or measurement source."""
    conn = get_db()
    conditions = []
    params = []

    if query:
        conditions.append("name LIKE ?")
        params.append(f"%{query}%")
    if form_factor:
        conditions.append("form_factor = ?")
        params.append(form_factor)
    if signature:
        conditions.append("signature LIKE ?")
        params.append(f"%{signature}%")
    if source:
        conditions.append("source LIKE ?")
        params.append(f"%{source}%")

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT name, source, coupler, form_factor, signature, score
        FROM headphones
        WHERE {where}
        ORDER BY score DESC NULLS LAST, name
        LIMIT ?
    """
    params.append(min(limit, 50))
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if not rows:
        return "No results found."

    lines = [f"## Search results ({len(rows)} found)"]
    for r in rows:
        score = f" [score:{r['score']}]" if r["score"] else ""
        sig = f" ({r['signature']})" if r["signature"] else ""
        coupler = f" [{r['coupler']}]" if r["coupler"] else ""
        lines.append(
            f"- **{r['name']}** — {r['source']}{coupler} | {r['form_factor']}{sig}{score}"
        )
    return "\n".join(lines)


@mcp_server.tool(
    name="eq_profile",
    annotations={
        "title": "Get EQ profile",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def eq_profile(
    name: str = Field(..., description="Headphone/IEM model name (e.g., HIFIMAN HE400se, Sony WF-1000XM5)"),
    source: str = Field(
        default="",
        description="Measurement source (e.g., oratory1990). Empty = best scored source",
    ),
) -> str:
    """Get detailed EQ profile for a headphone. Includes parametric EQ, fixed band EQ, and per-band sound analysis."""
    conn = get_db()
    hp = _find_headphone(conn, name, source)

    if not hp:
        conn.close()
        return f"'{name}' not found. Try eq_search to find the correct name."

    peq = conn.execute(
        "SELECT * FROM parametric_eq WHERE headphone_id = ? ORDER BY filter_num",
        (hp["id"],),
    ).fetchall()
    fbeq = conn.execute(
        "SELECT * FROM fixed_band_eq WHERE headphone_id = ? ORDER BY filter_num",
        (hp["id"],),
    ).fetchall()
    conn.close()

    return format_profile(hp, peq, fbeq)


@mcp_server.tool(
    name="eq_compare",
    annotations={
        "title": "Compare headphones",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def eq_compare(
    name1: str = Field(..., description="First model name"),
    name2: str = Field(..., description="Second model name"),
) -> str:
    """Compare two headphones band-by-band with sound signature analysis."""
    conn = get_db()
    hp1 = _find_headphone(conn, name1)
    hp2 = _find_headphone(conn, name2)
    conn.close()

    if not hp1:
        return f"'{name1}' not found."
    if not hp2:
        return f"'{name2}' not found."

    return format_comparison(hp1, hp2)


@mcp_server.tool(
    name="eq_recommend",
    annotations={
        "title": "Recommend headphones",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def eq_recommend(
    preference: str = Field(
        default="neutral",
        description="Sound preference: neutral, warm, bright, bass, vocal, analytical, fun. Or free text.",
    ),
    form_factor: str = Field(default="", description="Type: over-ear, in-ear, earbud"),
    limit: int = Field(default=10, description="Number of recommendations"),
) -> str:
    """Recommend headphones based on sound preference and type. Sorted by Harman preference score."""
    conn = get_db()

    pref_map = {
        "neutral": ["Neutral", "Harman-like"],
        "warm": ["Warm"],
        "bright": ["Bright"],
        "bass": ["Bass-heavy", "Warm"],
        "vocal": ["Mid-forward", "Neutral"],
        "analytical": ["Bright", "Neutral"],
        "fun": ["V-shaped", "U-shaped"],
    }

    pref_lower = preference.lower().strip()
    target_sigs = pref_map.get(pref_lower, [])

    if target_sigs:
        sig_conditions = " OR ".join(["signature LIKE ?"] * len(target_sigs))
        params = [f"%{s}%" for s in target_sigs]
        where = f"({sig_conditions})"
    else:
        where = "signature LIKE ?"
        params = [f"%{preference}%"]

    if form_factor:
        where += " AND form_factor = ?"
        params.append(form_factor)

    params.append(min(limit, 30))
    rows = conn.execute(
        f"""SELECT name, source, coupler, form_factor, signature, score, std_db, slope
            FROM headphones
            WHERE {where} AND signature != ''
            ORDER BY score DESC NULLS LAST, std_db ASC NULLS LAST
            LIMIT ?""",
        params,
    ).fetchall()
    conn.close()

    if not rows:
        return f"No headphones found for '{preference}' preference."

    lines = [f"## Recommendations ({preference}, {form_factor or 'all types'})"]
    for i, r in enumerate(rows, 1):
        score = f" score:{r['score']}" if r["score"] else ""
        std = f" STD:{r['std_db']}dB" if r["std_db"] else ""
        lines.append(
            f"{i}. **{r['name']}** — {r['source']} | {r['form_factor']} | {r['signature']}{score}{std}"
        )
    return "\n".join(lines)


@mcp_server.tool(
    name="eq_ranking",
    annotations={
        "title": "Harman preference ranking",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def eq_ranking(
    form_factor: str = Field(default="over-ear", description="Type: over-ear, in-ear"),
    limit: int = Field(default=20, description="Number of entries"),
) -> str:
    """Get headphone rankings by Harman headphone listener preference score."""
    conn = get_db()
    rows = conn.execute(
        """SELECT name, source, form_factor, signature, score, std_db, slope
           FROM headphones
           WHERE score IS NOT NULL AND form_factor LIKE ?
           ORDER BY score DESC
           LIMIT ?""",
        (f"%{form_factor}%", min(limit, 50)),
    ).fetchall()
    conn.close()

    if not rows:
        return "No ranking data available."

    lines = [f"## Harman preference ranking ({form_factor}, top {len(rows)})"]
    lines.append(f"{'Rank':>4}  {'Score':>5}  {'STD':>5}  {'Slope':>6}  Model")
    lines.append(f"{'─'*4}  {'─'*5}  {'─'*5}  {'─'*6}  {'─'*30}")
    for i, r in enumerate(rows, 1):
        slope_mark = "↓" if (r["slope"] or 0) < -0.1 else ("↑" if (r["slope"] or 0) > 0.1 else "→")
        sig = f" ({r['signature']})" if r["signature"] else ""
        lines.append(
            f"{i:>4}  {r['score']:>5.0f}  {r['std_db']:>5.2f}  {r['slope']:>+6.2f}{slope_mark}  {r['name']}{sig}"
        )
    return "\n".join(lines)


@mcp_server.tool(
    name="eq_targets",
    annotations={
        "title": "List target curves",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def eq_targets() -> str:
    """List all available EQ target curves (Harman, Diffuse Field, etc.)."""
    conn = get_db()
    rows = conn.execute("SELECT name FROM targets ORDER BY name").fetchall()
    conn.close()

    if not rows:
        return "No target data. Run eq_sync first."

    lines = ["## Available target curves"]
    categories = {"Harman": [], "Diffuse Field": [], "AutoEq": [], "Other": []}
    for r in rows:
        name = r["name"]
        if "Harman" in name or "harman" in name:
            categories["Harman"].append(name)
        elif "Diffuse" in name:
            categories["Diffuse Field"].append(name)
        elif "AutoEq" in name:
            categories["AutoEq"].append(name)
        else:
            categories["Other"].append(name)

    for cat, names in categories.items():
        if names:
            lines.append(f"\n### {cat}")
            for n in names:
                lines.append(f"- {n}")

    return "\n".join(lines)


@mcp_server.tool(
    name="eq_sync",
    annotations={
        "title": "Sync database",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def eq_sync() -> str:
    """Pull latest AutoEQ data from GitHub and rebuild the database. May take a few minutes."""
    try:
        result = sync_db(progress_callback=lambda msg: None)
        return result
    except Exception as e:
        return f"Sync error: {type(e).__name__} – {e}"


# ── Entry Point ──────────────────────────────────────────────────────────────
def main():
    if "--sync" in sys.argv:
        print("AutoEQ database sync starting...")
        result = sync_db(progress_callback=print)
        print(result)
    elif "--sse" in sys.argv:
        port = int(os.getenv("AUTOEQ_MCP_PORT", os.getenv("MCP_PORT", "3008")))
        host = os.getenv("AUTOEQ_MCP_HOST", "0.0.0.0")
        mcp_server.settings.host = host
        mcp_server.settings.port = port

        allowed = os.getenv("AUTOEQ_MCP_ALLOWED_HOSTS", "")
        if allowed:
            from mcp.server.fastmcp.server import TransportSecuritySettings
            mcp_server.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[h.strip() for h in allowed.split(",")],
            )

        mcp_server.run(transport="sse")
    else:
        mcp_server.run()


if __name__ == "__main__":
    main()
