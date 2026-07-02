#!/bin/bash
# trailmem-dashboard.sh — 記憶の健康状態をHTML可視化
# Usage: bash trailmem-dashboard.sh > $HOME/.trailmem/trailmem-dashboard.html

set -euo pipefail

DB="${TRAILMEM_DB:-$HOME/.trailmem/trailmem.db}"

python3 - "$DB" << 'PYEOF'
import sqlite3, json, sys
from datetime import datetime, timedelta

db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

now = datetime.utcnow()
week_ago = (now - timedelta(days=7)).isoformat() + "Z"

# Stats
total_episodes = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
total_keywords = conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
total_trails = conn.execute("SELECT COUNT(*) FROM episode_keywords WHERE is_deleted = 0").fetchone()[0]
deleted_trails = conn.execute("SELECT COUNT(*) FROM episode_keywords WHERE is_deleted = 1").fetchone()[0]
avg_strength = conn.execute("SELECT printf('%.3f', AVG(effective_strength)) FROM episode_keywords WHERE is_deleted = 0").fetchone()[0]
current_seq = int(conn.execute("SELECT COALESCE(value, '0') FROM trailmem_meta WHERE key = 'turn_seq'").fetchone()[0])

# Top memories (strongest trails)
top_memories = conn.execute("""
    SELECT e.id, e.summary, e.inner, e.sentiment_neg, e.sentiment_pos, e.created_at,
           ek.keyword, printf('%.3f', ek.effective_strength) as strength,
           ek.used, ek.shown
    FROM episode_keywords ek
    JOIN episodes e ON e.id = ek.episode_id
    WHERE ek.is_deleted = 0
    ORDER BY ek.effective_strength DESC
    LIMIT 15
""").fetchall()

# Fading memories (low strength, but once recalled)
fading = conn.execute("""
    SELECT e.id, e.summary, e.created_at,
           ek.keyword, printf('%.3f', ek.effective_strength) as strength,
           printf('%.3f', ek.decay) as decay_val
    FROM episode_keywords ek
    JOIN episodes e ON e.id = ek.episode_id
    WHERE ek.is_deleted = 0 AND ek.effective_strength < 0.15 AND ek.shown > 0
    ORDER BY ek.effective_strength ASC
    LIMIT 15
""").fetchall()

# Recent additions (last 7 days)
recent = conn.execute("""
    SELECT e.id, e.summary, e.inner, e.sentiment_neg, e.sentiment_pos, e.created_at,
           GROUP_CONCAT(ek.keyword, ', ') as keywords
    FROM episodes e
    JOIN episode_keywords ek ON ek.episode_id = e.id AND ek.is_deleted = 0
    WHERE e.created_at > ?
    GROUP BY e.id
    ORDER BY e.created_at DESC
""", (week_ago,)).fetchall()

# Keyword frequency (most connected)
kw_freq = conn.execute("""
    SELECT ek.keyword, COUNT(*) as ep_count,
           printf('%.3f', AVG(ek.effective_strength)) as avg_str
    FROM episode_keywords ek
    WHERE ek.is_deleted = 0
    GROUP BY ek.keyword
    ORDER BY ep_count DESC
    LIMIT 20
""").fetchall()

# Strength distribution
dist = conn.execute("""
    SELECT
        SUM(CASE WHEN effective_strength >= 0.5 THEN 1 ELSE 0 END) as recall_zone,
        SUM(CASE WHEN effective_strength >= 0.2 AND effective_strength < 0.5 THEN 1 ELSE 0 END) as deep_zone,
        SUM(CASE WHEN effective_strength >= 0.05 AND effective_strength < 0.2 THEN 1 ELSE 0 END) as dig_zone,
        SUM(CASE WHEN effective_strength < 0.05 THEN 1 ELSE 0 END) as forgotten
    FROM episode_keywords WHERE is_deleted = 0
""").fetchone()

conn.close()

def esc(s):
    if s is None: return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def sentiment_bar(neg, pos):
    return f'<span style="color:var(--warn)">{neg}</span>/<span style="color:var(--ok)">{pos}</span>'

print(f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrailMem Dashboard</title>
<style>
  :root {{ --bg: #0a0e14; --card: #131920; --border: #1e2a35; --accent: #63d6ff; --warn: #ff7676; --ok: #7deb9a; --watch: #ffc85c; --text: #e0dcd6; --dim: #8b9bb0; --purple: #c88cff; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--text); font: 14px/1.6 'Inter', system-ui, sans-serif; padding: 20px; max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 1.4em; color: var(--accent); margin-bottom: 4px; }}
  .sub {{ color: var(--dim); font-size: 0.85em; margin-bottom: 20px; }}
  h2 {{ font-size: 1em; color: var(--accent); border-bottom: 1px solid var(--border); padding-bottom: 4px; margin: 20px 0 10px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-bottom: 20px; }}
  .stat {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; text-align: center; }}
  .stat .num {{ font-size: 1.5em; font-weight: 700; color: var(--accent); }}
  .stat .label {{ font-size: 0.75em; color: var(--dim); }}
  .bar-row {{ display: flex; gap: 2px; height: 24px; margin-bottom: 16px; border-radius: 4px; overflow: hidden; }}
  .bar-row div {{ display: flex; align-items: center; justify-content: center; font-size: 0.7em; font-weight: 600; }}
  .bar-recall {{ background: rgba(125,235,154,0.3); color: var(--ok); }}
  .bar-deep {{ background: rgba(99,214,255,0.3); color: var(--accent); }}
  .bar-dig {{ background: rgba(255,200,92,0.3); color: var(--watch); }}
  .bar-forgot {{ background: rgba(255,118,118,0.2); color: var(--warn); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82em; }}
  th {{ text-align: left; color: var(--dim); font-weight: 500; padding: 4px 8px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 6px 8px; border-bottom: 1px solid rgba(30,42,53,0.5); vertical-align: top; }}
  .str {{ font-family: monospace; color: var(--accent); }}
  .kw {{ display: inline-block; font-size: 0.8em; padding: 1px 6px; border-radius: 4px; background: rgba(99,214,255,0.1); color: var(--accent); margin: 1px 2px; }}
  .summary {{ max-width: 400px; }}
  .inner {{ font-size: 0.85em; color: var(--dim); font-style: italic; }}
  .footer {{ text-align: center; color: var(--dim); font-size: 0.7em; margin-top: 30px; padding-top: 12px; border-top: 1px solid var(--border); }}
</style>
</head>
<body>

<h1>TrailMem Dashboard</h1>
<p class="sub">{now.strftime('%Y-%m-%d %H:%M')} UTC / turn_seq: {current_seq}</p>

<div class="stats">
  <div class="stat"><div class="num">{total_episodes}</div><div class="label">episodes</div></div>
  <div class="stat"><div class="num">{total_keywords}</div><div class="label">keywords</div></div>
  <div class="stat"><div class="num">{total_trails}</div><div class="label">active trails</div></div>
  <div class="stat"><div class="num">{deleted_trails}</div><div class="label">deleted trails</div></div>
  <div class="stat"><div class="num">{avg_strength}</div><div class="label">avg strength</div></div>
  <div class="stat"><div class="num">{len(recent)}</div><div class="label">this week</div></div>
</div>

<h2>Strength Distribution</h2>
""")

total_active = sum(dist)
if total_active > 0:
    pcts = [d / total_active * 100 for d in dist]
    print(f"""<div class="bar-row">
  <div class="bar-recall" style="width:{pcts[0]:.0f}%">Recall {dist[0]}</div>
  <div class="bar-deep" style="width:{pcts[1]:.0f}%">Deep {dist[1]}</div>
  <div class="bar-dig" style="width:{pcts[2]:.0f}%">Dig {dist[2]}</div>
  <div class="bar-forgot" style="width:{pcts[3]:.0f}%">Fading {dist[3]}</div>
</div>""")

print("""<h2>Top Memories (Strongest Trails)</h2>
<table>
<tr><th>Strength</th><th>Keyword</th><th>Summary</th><th>Used/Shown</th></tr>""")
seen_eps = set()
for row in top_memories:
    if row['id'] in seen_eps:
        continue
    seen_eps.add(row['id'])
    print(f"""<tr>
  <td class="str">{row['strength']}</td>
  <td><span class="kw">{esc(row['keyword'])}</span></td>
  <td class="summary">{esc(row['summary'][:120])}</td>
  <td>{row['used']}/{row['shown']}</td>
</tr>""")
print("</table>")

print("""<h2>Fading Memories (Forgetting Candidates)</h2>
<table>
<tr><th>Strength</th><th>Keyword</th><th>Summary</th><th>Decay</th></tr>""")
seen_eps = set()
for row in fading:
    if row['id'] in seen_eps:
        continue
    seen_eps.add(row['id'])
    print(f"""<tr>
  <td class="str">{row['strength']}</td>
  <td><span class="kw">{esc(row['keyword'])}</span></td>
  <td class="summary">{esc(row['summary'][:120])}</td>
  <td>{row['decay_val']}</td>
</tr>""")
print("</table>")

print(f"""<h2>Recent ({len(recent)} episodes this week)</h2>
<table>
<tr><th>Date</th><th>Summary</th><th>Keywords</th><th>Neg/Pos</th></tr>""")
for row in recent:
    date_str = row['created_at'][:10] if row['created_at'] else ""
    kws = "".join(f'<span class="kw">{esc(k.strip())}</span>' for k in (row['keywords'] or "").split(','))
    print(f"""<tr>
  <td>{date_str}</td>
  <td class="summary">{esc(row['summary'][:100])}</td>
  <td>{kws}</td>
  <td>{sentiment_bar(row['sentiment_neg'], row['sentiment_pos'])}</td>
</tr>""")
print("</table>")

print("""<h2>Keyword Network (Top 20)</h2>
<table>
<tr><th>Keyword</th><th>Episodes</th><th>Avg Strength</th></tr>""")
for row in kw_freq:
    bar_w = int(float(row['avg_str']) * 200)
    print(f"""<tr>
  <td><span class="kw">{esc(row['keyword'])}</span></td>
  <td>{row['ep_count']}</td>
  <td><span class="str">{row['avg_str']}</span> <span style="display:inline-block;width:{bar_w}px;height:6px;background:var(--accent);border-radius:3px;vertical-align:middle;opacity:0.5"></span></td>
</tr>""")
print("</table>")

print(f"""
<div class="footer">
  TrailMem v0.3 + ACT-R Phase 1 / Generated {now.strftime('%Y-%m-%d %H:%M:%S')} UTC
</div>
</body>
</html>""")
PYEOF
