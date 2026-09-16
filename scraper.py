import argparse
import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

import aiohttp

API = "https://qbank-api.collegeboard.org/msreportingquestionbank-prod/questionbank"
ASSESSMENT_ID = 99
SECTIONS = {1: ("Reading and Writing", "R&W", "readingLiveItems"), 2: ("Math", "Math", "mathLiveItems")}

BLOCKED_STATUSES = {403, 429, 503}

log = logging.getLogger("scraper")

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    question_id TEXT PRIMARY KEY,
    external_id TEXT,
    ibn TEXT,
    section_id INTEGER NOT NULL,
    section TEXT NOT NULL,
    domain_code TEXT NOT NULL,
    domain TEXT NOT NULL,
    skill_code TEXT NOT NULL,
    skill TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    score_band INTEGER,
    answer_type TEXT,
    active INTEGER NOT NULL DEFAULT 0,
    legacy INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER,
    updated_at INTEGER,
    metadata TEXT NOT NULL,
    content TEXT
);
CREATE INDEX IF NOT EXISTS idx_questions_filters ON questions (section_id, domain_code, skill_code, difficulty);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def answer_type(content):
    if content is None:
        return None
    if "type" in content:
        return content["type"]
    style = (content.get("answer") or {}).get("style", "")
    return "mcq" if style.lower() == "multiple choice" else "spr"


class Blocked(Exception):
    pass


async def request_json(session, method, url, payload=None, attempts=6):
    for attempt in range(attempts):
        try:
            async with session.request(method, url, json=payload) as response:
                if response.status in BLOCKED_STATUSES:
                    raise Blocked(f"HTTP {response.status}")
                response.raise_for_status()
                return await response.json(content_type=None)
        except Blocked as error:
            if attempt == attempts - 1:
                raise
            delay = min(60 * 2 ** attempt, 900)
            log.warning("College Board is rate limiting requests (%s), waiting %ss", error, delay)
            await asyncio.sleep(delay)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as error:
            if attempt == attempts - 1:
                raise
            delay = 2 ** attempt
            log.warning("Request to %s failed (%s), retrying in %ss", url, error, delay)
            await asyncio.sleep(delay)


async def fetch_listing(session):
    lookup = await request_json(session, "GET", f"{API}/lookup")
    domains = lookup["lookupData"]["domain"]
    rows = []
    for section_id, (section_name, lookup_key, live_key) in SECTIONS.items():
        codes = [domain["primaryClassCd"] for domain in domains[lookup_key]]
        live = set(lookup.get(live_key) or [])
        payload = {"asmtEventId": ASSESSMENT_ID, "test": section_id, "domain": ",".join(codes)}
        items = await request_json(session, "POST", f"{API}/digital/get-questions", payload)
        if not items:
            raise RuntimeError(f"College Board returned no questions for {section_name}")
        for item in items:
            rows.append((section_id, section_name, item, item.get("external_id") in live))
    return rows


async def sync(db_path, full=False, concurrency=2, delay=0.4):
    started = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=60)
    headers = {"User-Agent": "SAT-Bot (+https://github.com/woopers6/SAT-Bot)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        listing = await fetch_listing(session)
        conn = connect(db_path)
        try:
            existing = {
                row[0]: (row[1], row[2] is not None)
                for row in conn.execute("SELECT question_id, updated_at, content FROM questions")
            }
            seen = set()
            pending = []
            for section_id, section_name, item, active in listing:
                question_id = item["questionId"]
                seen.add(question_id)
                previous = existing.get(question_id)
                conn.execute(
                    """
                    INSERT INTO questions (question_id, external_id, ibn, section_id, section, domain_code, domain,
                        skill_code, skill, difficulty, score_band, active, legacy, created_at, updated_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(question_id) DO UPDATE SET
                        external_id = excluded.external_id, ibn = excluded.ibn, section_id = excluded.section_id,
                        section = excluded.section, domain_code = excluded.domain_code, domain = excluded.domain,
                        skill_code = excluded.skill_code, skill = excluded.skill, difficulty = excluded.difficulty,
                        score_band = excluded.score_band, active = excluded.active, legacy = excluded.legacy,
                        created_at = excluded.created_at, metadata = excluded.metadata
                    """,
                    (
                        question_id,
                        item.get("external_id"),
                        item.get("ibn"),
                        section_id,
                        section_name,
                        item["primary_class_cd"],
                        item["primary_class_cd_desc"],
                        item["skill_cd"],
                        item["skill_desc"],
                        item["difficulty"],
                        item.get("score_band_range_cd"),
                        int(active),
                        int(not item.get("external_id")),
                        item.get("createDate"),
                        previous[0] if previous else None,
                        json.dumps(item),
                    ),
                )
                if full or previous is None or not previous[1] or previous[0] != item.get("updateDate"):
                    pending.append(item)
            removed = set(existing) - seen
            conn.executemany("DELETE FROM questions WHERE question_id = ?", [(question_id,) for question_id in removed])
            conn.commit()

            log.info("%s questions listed, %s to download, %s removed", len(seen), len(pending), len(removed))
            queue = asyncio.Queue()
            for item in pending:
                queue.put_nowait(item)
            failures = 0
            done = 0

            async def worker():
                nonlocal failures, done
                while not queue.empty():
                    item = queue.get_nowait()
                    payload = {"external_id": item.get("external_id") or item.get("ibn")}
                    try:
                        content = await request_json(session, "POST", f"{API}/digital/get-question", payload)
                    except Blocked:
                        failures += queue.qsize() + 1
                        while not queue.empty():
                            queue.get_nowait()
                        log.error("Still rate limited, stopping this sync early")
                        return
                    except Exception as error:
                        failures += 1
                        log.error("Failed to download question %s: %s", item["questionId"], error)
                        continue
                    conn.execute(
                        "UPDATE questions SET content = ?, answer_type = ?, updated_at = ? WHERE question_id = ?",
                        (json.dumps(content), answer_type(content), item.get("updateDate"), item["questionId"]),
                    )
                    done += 1
                    if done % 25 == 0:
                        conn.commit()
                    if done % 100 == 0:
                        log.info("Downloaded %s/%s questions", done, len(pending))
                    await asyncio.sleep(delay)

            await asyncio.gather(*(worker() for _ in range(max(1, concurrency))))
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('last_sync', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(int(time.time())),),
            )
            conn.commit()
        finally:
            conn.close()
    stats = {
        "total": len(seen),
        "downloaded": done,
        "failed": failures,
        "removed": len(removed),
        "seconds": round(time.monotonic() - started, 1),
    }
    log.info("Sync finished: %s", stats)
    return stats


def export_json(db_path, output):
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        questions = []
        for row in conn.execute("SELECT * FROM questions ORDER BY section_id, domain_code, skill_code, question_id"):
            record = dict(row)
            record["metadata"] = json.loads(record["metadata"])
            record["content"] = json.loads(record["content"]) if record["content"] else None
            questions.append(record)
    finally:
        conn.close()
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(questions, handle, ensure_ascii=False, indent=2)
    return len(questions)


def main():
    parser = argparse.ArgumentParser(description="Download the SAT Suite Question Bank into SQLite")
    parser.add_argument("--db", default="data/questions.db")
    parser.add_argument("--full", action="store_true", help="re-download every question")
    parser.add_argument("--export", metavar="FILE", help="also write every question to a JSON file")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.4, help="seconds to wait between requests")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(sync(args.db, full=args.full, concurrency=args.concurrency, delay=args.delay))
    if args.export:
        count = export_json(args.db, args.export)
        log.info("Exported %s questions to %s", count, args.export)


if __name__ == "__main__":
    main()
