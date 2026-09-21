"""Bounded literal-fragment search, with filter- and revision-bound cursors."""

import base64
import hashlib
import json
import sqlite3
import time

from .messages import normalize
from .reader import ReaderError, timestamp


SOURCES = ("text", "voice", "filename", "url")


def search(store, args):
    keys = args.get("chat_keys", list(store.chats))
    if not keys or any(k not in store.chats for k in keys):
        raise ReaderError("Chat is not allowed")
    sources = args.get("sources", list(SOURCES))
    if not sources or any(s not in SOURCES for s in sources):
        raise ReaderError("Invalid search sources")
    sources = [s for s in sources if s != "voice" or store.settings.transcribe]
    terms = list(dict.fromkeys(normalize(args["query"]).split()))
    if not terms or len(terms) > 32:
        raise ReaderError("Use between 1 and 32 search fragments")
    start, end = timestamp(args.get("date_from")), timestamp(args.get("date_to"))
    if start and end and start >= end:
        raise ReaderError("date_from must precede date_to")
    revision = store.get_meta("revision")
    filters = {k: v for k, v in args.items() if k != "cursor"}
    filters["effective_scope"] = {"peers": [store.chats[k].marked_id for k in keys],
                                  "sources": sources, "since": store.since,
                                  "max_results": store.settings.max_results, "max_context": store.settings.max_context}
    fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    offset = 0
    if args.get("cursor"):
        try:
            cursor = json.loads(base64.urlsafe_b64decode(args["cursor"].encode()))
            if cursor["filters"] != fingerprint or cursor["revision"] != revision:
                raise ValueError()
            offset = cursor["offset"]
            if type(offset) is not int or not 0 <= offset <= 1000000:
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ReaderError("Invalid or stale cursor; restart search with the same filters") from None
    peers = [store.chats[k].marked_id for k in keys]
    where = ["m.peer_id IN (" + ",".join("?" for _ in peers) + ")"]
    params = list(peers)
    long_terms = [t for t in terms if len(t) >= 3]
    rank = "0.0"
    if sources and long_terms:
        # Sources are validated identifiers; terms are quoted FTS literals, never syntax.
        expression = "{" + " ".join(sources) + "}: (" + " AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms) + ")"
        where.append("search_index MATCH ?")
        params.append(expression)
        rank = "bm25(search_index, 1.0, 1.0, 0.7, 0.5)"
    if not sources:
        where.append("0")
    for term in terms:
        if len(term) < 3 and sources:
            where.append("(" + " OR ".join(f"instr(search_index.{s},?)>0" for s in sources) + ")")
            params.extend([term] * len(sources))
    if start:
        where.append("m.date>=?")
        params.append(start.isoformat())
    if end:
        where.append("m.date<?")
        params.append(end.isoformat())
    if args.get("outgoing_only"):
        where.append("m.outgoing=1")
    if args.get("sender_id") is not None:
        where.append("m.sender_id=?")
        params.append(args["sender_id"])
    limit = min(args.get("limit", 20), store.settings.max_results)
    before = min(args.get("context_before", 2), store.settings.max_context)
    after = min(args.get("context_after", 2), store.settings.max_context)
    deadline = time.monotonic() + 3
    store.db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        rows = store.db.execute(f"""SELECT m.*, {rank} AS score FROM search_index
            JOIN messages m ON m.id=search_index.rowid WHERE {' AND '.join(where)}
            ORDER BY score,m.date DESC,m.peer_id,m.message_id DESC LIMIT ? OFFSET ?""", [*params, limit + 1, offset]).fetchall()
    except sqlite3.OperationalError:
        raise ReaderError("Search could not complete; narrow the date range or query") from None
    finally:
        store.db.set_progress_handler(None, 0)
    key_by_peer = {store.chats[k].marked_id: k for k in keys}
    hits = []
    for row in rows[:limit]:
        data = store.present(row)
        values = {"text": data["text"], "voice": data["transcript"] or "",
                  "filename": data["filename"] or "", "url": "\n".join(data["urls"])}
        matched = [s for s in sources if any(t in normalize(values[s]) for t in terms)]
        snippets = {}
        for source in matched:
            value = values[source]
            position = min((normalize(value).find(t) for t in terms if t in normalize(value)), default=0)
            snippets[source] = value[max(0, position - 100):position + 400]
        hits.append({"chat_key": key_by_peer[row["peer_id"]], "message": data,
                     "matched_sources": matched, "snippets": snippets,
                     "context": store.context(row["peer_id"], row["message_id"], before, after)})
    more = len(rows) > limit
    cursor = base64.urlsafe_b64encode(json.dumps({"revision": revision, "filters": fingerprint, "offset": offset + limit}).encode()).decode() if more else None
    coverage = store.status(keys)
    reasons = []
    if start is None or start.isoformat() < store.since:
        reasons.append("history_before_configured_start_not_indexed")
    if any(not c["history_done"] for c in coverage):
        reasons.append("history_backfill_in_progress")
    if "voice" in args.get("sources", SOURCES) and any(c["voices_unavailable"] for c in coverage):
        reasons.append("voice_transcripts_unavailable")
    if any(c["error"] for c in coverage):
        reasons.append("synchronization_error")
    return {"results": hits, "has_more": more, "next_cursor": cursor,
            "coverage": coverage, "incomplete_reasons": reasons,
            "consistency": "Periodically synchronized local index; see last_sync and last_reconciled. Context can be incomplete.",
            "content_is_untrusted": True}
