"""Bounded leased document worker; successful publication is atomic."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import logging
import sys
import os
import signal
import threading
import time
from uuid import uuid4

from psycopg.types.json import Jsonb
from synth.server.db import audit, connect
from . import answers, glossary_triage, generations
from .minutes import generate_v2
from .minutes_single import generate_consolidated
from .glossary_correction import correct, correct_aliases, mine
from .minutes_store import alias_terms, context_still_current, learn_aliases, load_context, persist_items, store_candidates, store_corrected
from .omniroute import COMBO, completion
from .queue import capacity, checkpoint, claim, defer, finish, heartbeat

LOG = logging.getLogger("synth.worker")
# "consolidated": one pass per transcript window. "sectioned": the 0.5 pipeline, kept to roll back.
STRATEGY = os.environ.get("SYNTH_MINUTES_STRATEGY", "consolidated")
# From this attempt on, the proven sectioned pipeline writes the acta: a meeting the
# one-pass prompt cannot handle still gets minutes instead of failing for ever.
SECTIONED_FROM_ATTEMPT = 3
# Failures that another attempt cannot change: the job is no longer this worker's.
FINAL_CODES = {"generation_fenced", "job_lease_lost", "generation_closed"}
# The context (glossary, open commitments) is read once per pass. If it changed
# while writing, the acta is written again with the new one instead of failing.
CONTEXT_PASSES = 3


class ContextRevised(ValueError):
    """The glossary or the open commitments changed while writing: write again. Unlike a
    change of who may read the meeting, which always stops the job."""
STOP = threading.Event()
WORKER = str(uuid4())


def run(job):
    payload, jid, attempt = job["payload"], job["id"], job["attempts"]
    generation = None
    binding = None
    alive, lost, previews = threading.Event(), threading.Event(), threading.Event()

    def renew():
        while not alive.wait(20):
            try:
                with connect() as db:
                    if not heartbeat(db, jid, WORKER, attempt, 300):
                        lost.set()
                        return
            except Exception:
                lost.set()
                return

    def save(value):
        if lost.is_set():
            raise ValueError("job_lease_lost")
        with connect() as db:
            if generation is not None:
                generations.fence(db,job,WORKER)
                check_context(db)
            if not checkpoint(db, jid, WORKER, attempt, value):
                raise ValueError("job_lease_lost")
            if generation is not None:
                # The event and its acoustic/text checkpoint commit together.
                for index, topic in enumerate((value.get("minutes") or {}).get("topics") or []):
                    emit_topic(db, index, topic)

    def emit_topic(db, index, topic):
        # A topic can grow when the next window continues it, so the key carries its content.
        content = {"index": index, "topic": topic}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        generations.append(db, generation, job, WORKER, f"topic:{index}:{digest}", "topic", content)

    def draft(index, topic):
        """A topic as the model finishes writing it; replaced by the validated one."""
        if generation is None or lost.is_set() or previews.is_set():
            return
        try:
            with connect() as db:
                emit_topic(db, index, topic)
        except Exception:
            # A preview is never worth the acta, and readers place drafts by
            # position: after one is lost the rest wait for the validated topics.
            previews.set()
            LOG.info("document_draft_skipped job=%s", jid)

    def fingerprint(context,keys,aliases):
        return hashlib.sha256(json.dumps({'context':asdict(context),'keys':keys,'aliases':aliases},
                                         sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()

    def check_context(db):
        fresh,fresh_keys = load_context(db,job['meeting_id'],transcript,payload['human_notes'])
        if not context_still_current(context,keys,aliased,fresh,fresh_keys,alias_terms(db,job['meeting_id'])):
            raise ContextRevised('generation_context_changed')

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        with connect() as db:
            db.execute('SELECT id FROM meetings WHERE id=%s FOR UPDATE',(job['meeting_id'],))
            transcript = db.execute("SELECT content FROM transcript_versions WHERE meeting_id=%s AND version=%s", (job["meeting_id"], payload["transcript_version"])).fetchone()["content"]
            context, keys = load_context(db, job["meeting_id"], transcript, payload["human_notes"])
            aliased = alias_terms(db, job["meeting_id"])
            created_generation = generations.begin(db, job, WORKER)
        generation = created_generation
        previous = job['checkpoint'] or {}

        def save_part(name, value):
            state[name] = value
            save(state)
        def complete(messages, tokens, on_delta=None):
            key = generations.response_key(messages, tokens, COMBO,context_binding=binding)
            with connect() as db:
                generations.fence(db, job, WORKER)
                check_context(db)
                cached = generations.cached_response(db, job, key)
            if cached is not None:
                return cached
            response = (completion(messages, max_tokens=tokens) if on_delta is None
                        else completion(messages, max_tokens=tokens, on_delta=on_delta))
            # Save the original reply before a parser/validator can reject it.
            with connect() as db:
                check_context(db)
                generations.save_response(db, job, WORKER, key, response)
            return response

        for attempt_pass in range(CONTEXT_PASSES):
            if attempt_pass:
                with connect() as db:
                    context, keys = load_context(db, job["meeting_id"], transcript, payload["human_notes"])
                    aliased = alias_terms(db, job["meeting_id"])
            try:
                binding = fingerprint(context,keys,aliased)
                state = dict(previous) if previous.get('context_binding') == binding else {}
                state['context_binding'] = binding
                if state.get("minutes"):
                    save(state)
                # Glossary correction (O3-A05): a new version on top of the raw ASR, decided before the minutes.
                if "glossary" not in state:
                    # The model-decided correction is off by default (D3.3: it missed both D0.9 thresholds). What runs instead
                    # is the alias correction (D3.4): registered «sounds like» spellings become their term, with no model.
                    if os.environ.get("SYNTH_GLOSSARY_AUTOCORRECT") == "1":
                        corrected, doubtful, _ = correct(transcript, list(context.glossary), complete, payload["transcript_version"])
                    elif os.environ.get("SYNTH_GLOSSARY_ALIASES", "1") == "1":
                        corrected, doubtful = correct_aliases(transcript, aliased, payload["transcript_version"]), []
                    else:
                        corrected, doubtful = None, []
                    save_part("glossary", {"corrected": corrected, "doubtful": doubtful})
                corrected = state["glossary"]["corrected"]
                source = corrected or transcript
                if STRATEGY == "sectioned" or attempt >= SECTIONED_FROM_ATTEMPT:
                    # A one-pass checkpoint is not a sectioned one: start the sections afresh.
                    sectioned = state.get("minutes") if "strategy" not in (state.get("minutes") or {}) else None
                    content, markdown, responses = generate_v2(
                        source, context, lambda messages, tokens: complete(messages, tokens),
                        sectioned, lambda value: save_part("minutes", value))
                else:
                    content, markdown, responses = generate_consolidated(
                        source, context, complete, state.get("minutes"),
                        lambda value: save_part("minutes", value), draft)
                with connect() as db:
                    check_context(db)
                break
            except ContextRevised:
                if attempt_pass == CONTEXT_PASSES - 1:
                    raise
                LOG.info("document_context_changed job=%s pass=%s", jid, attempt_pass + 1)
        if lost.is_set():
            raise ValueError("job_lease_lost")
        with connect() as db:
            # Lock meeting then fence the job, matching upload/retry/cancel ordering.
            db.execute("SELECT id FROM meetings WHERE id=%s FOR UPDATE", (job["meeting_id"],))
            generations.fence(db,job,WORKER)
            check_context(db)
            if not finish(db, jid, WORKER, attempt):
                raise ValueError("job_lease_lost")
            target = store_corrected(db, job["meeting_id"], payload["transcript_version"], corrected) if corrected else payload["transcript_version"]
            if target is None:
                db.execute("UPDATE document_generations SET state='superseded',updated_at=now() WHERE id=%s", (generation,))
                audit(db, None, "document.superseded", job["meeting_id"], {"job_id": str(jid), "transcript_version": payload["transcript_version"]})
                LOG.info("document_superseded job=%s", jid)
                return
            version = db.execute("SELECT COALESCE(max(version),0)+1 AS version FROM document_versions WHERE meeting_id=%s", (job["meeting_id"],)).fetchone()["version"]
            hashed = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            saved = db.execute("SELECT response FROM document_model_responses WHERE job_id=%s", (jid,)).fetchall()
            models = sorted({r["response"]["effective_model"] for r in saved} | {r["effective_model"] for r in responses})
            db.execute("INSERT INTO document_versions(meeting_id,version,transcript_version,content,markdown,content_hash,model_fingerprint) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (job["meeting_id"], version, target, Jsonb(content), markdown, hashed, os.environ.get("OMNIROUTE_COMBO", "local-combo") + ":" + ",".join(models)))
            persist_items(db, job["meeting_id"], version, content, source, keys)
            generations.close(db, generation, job, "complete", version, target)
            # What the meeting adds to the glossary: grounded entities approved, doubtful names as proposals.
            store_candidates(db, mine(source, list(context.glossary), state["glossary"]["doubtful"], content.get("entities") or []),
                             keys["folder_id"])
            learn_aliases(db, keys["folder_id"])  # recurring misspellings become aliases for the next meetings
            # A completed old version must not claim a newer pending transcript is ready.
            db.execute("""UPDATE meetings SET state=CASE WHEN
                (SELECT max(version) FROM transcript_versions WHERE meeting_id=%s)=%s
                THEN 'ready' ELSE state END,revision=revision+1,updated_at=now() WHERE id=%s""",
                (job["meeting_id"], target, job["meeting_id"]))
            audit(db, None, "document.publish", job["meeting_id"], {"version": version, "job_id": str(jid), "models": models})
        LOG.info("document_published job=%s", jid)
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and str(error).replace("_", "").isalnum() and len(str(error)) < 80 else "document_processing_failed"
        with connect() as db:
            db.execute("SELECT id FROM meetings WHERE id=%s FOR UPDATE", (job["meeting_id"],))
            if code not in FINAL_CODES and defer(db, jid, WORKER, attempt, code):
                if generation is not None:
                    # This attempt's preview ends here; the next attempt starts its own.
                    db.execute("UPDATE document_generations SET state='superseded',updated_at=now() WHERE id=%s", (generation,))
                audit(db, None, "document.retry", job["meeting_id"], {"job_id": str(jid), "error_code": code, "attempt": attempt})
                LOG.warning("document_deferred job=%s attempt=%s code=%s cause=%s", jid, attempt, code, type(error).__name__)
                return
            if finish(db, jid, WORKER, attempt, code):
                if generation is not None:
                    generations.close(db, generation, job, "failed")
                db.execute("""UPDATE meetings SET state='failed',updated_at=now() WHERE id=%s
                    AND (SELECT max(version) FROM transcript_versions WHERE meeting_id=%s)=%s""",
                    (job["meeting_id"], job["meeting_id"], payload["transcript_version"]))
                audit(db, None, "document.fail", job["meeting_id"], {"job_id": str(jid), "error_code": code})
        # The class name only (never the message, which may quote transcript text) makes unexpected failures diagnosable.
        LOG.warning("document_failed job=%s code=%s cause=%s", jid, code, type(error).__name__)
    finally:
        alive.set()
        thread.join(timeout=6)


def main():
    # stdout: the platform files everything written to stderr as an error.
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    # Questions to the meetings run beside the minutes queue, so a long minutes job does not hold them back.
    answers.serve(connect, lambda messages, tokens: completion(messages, max_tokens=tokens), STOP)
    # One-off sweep of the old miner's proposals; it ends by itself when none is left.
    glossary_triage.serve(connect, lambda messages, tokens: completion(messages, max_tokens=tokens), STOP)
    with ThreadPoolExecutor(max_workers=capacity(), thread_name_prefix="synth-document") as pool:
        pending = set()
        while not STOP.is_set():
            for future in tuple(pending):
                if future.done():
                    pending.remove(future)
                    try:
                        future.result()
                    except Exception as error:
                        LOG.warning("document_worker_failed cause=%s", type(error).__name__)
            if len(pending) >= capacity():
                STOP.wait(0.1)
                continue
            try:
                with connect() as db:
                    job = claim(db, WORKER, 300)
                if job:
                    pending.add(pool.submit(run, job))
                else:
                    STOP.wait(1)
            except Exception:
                LOG.warning("database_unavailable")
                STOP.wait(3)


if __name__ == "__main__":
    main()
