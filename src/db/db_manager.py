import sqlite3
import os
import sys
import time
import json
from datetime import datetime
from src.core.app_paths import get_jobs_db_path, get_outputs_dir

DB_PATH = str(get_jobs_db_path())

DEFAULT_APP_SETTINGS = {
    "slots_per_account": "5",
    "same_account_stagger_seconds": "1.0",
    "global_stagger_min_seconds": "0.3",
    "global_stagger_max_seconds": "0.6",
    "max_retries": "3",
    "max_auto_retries_per_job": "3",
    "retry_base_delay_seconds": "10",
    "auto_retry_base_delay_seconds": "10",
    "recaptcha_account_cooldown_seconds": "15",
    "image_execution_mode": "api_only",
    "enable_profile_clones": "1",
    "api_captcha_submit_lock": "0",
    "browser_mode": "cloakbrowser",
    "chrome_display": "headless",
    "cloak_display": "headless",
    "random_fingerprint_per_session": "0",
    "cookie_warmup": "1",
    "light_warmup": "1",
    "speed_profile": "fast",
    "api_min_submit_gap_seconds": "0",
    "api_humanized_warmup_min_seconds": "0.2",
    "api_humanized_warmup_max_seconds": "0.4",
    "api_humanized_wait_ref_min_seconds": "0.3",
    "api_humanized_wait_ref_max_seconds": "0.8",
    "api_humanized_wait_no_ref_min_seconds": "0.3",
    "api_humanized_wait_no_ref_max_seconds": "0.6",
    "auto_refresh_after_jobs": "150",
    "auto_restart_recap_fail_threshold": "3",
    "auto_restart_recap_fail_window": "10",
    "auto_restart_recap_cooldown_seconds": "30",
    "output_directory": "",
    "generation_mode": "browser_per_slot",
}

def ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        _ensure_db_schema(conn)
    finally:
        conn.close()

def _ensure_db_schema(conn):
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    
    # Accounts Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            session_path TEXT,
            proxy TEXT DEFAULT '',
            status TEXT DEFAULT 'idle'
        )
    ''')
    
    # Jobs Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            prompt TEXT,
            job_type TEXT DEFAULT 'image',
            model TEXT,
            aspect_ratio TEXT,
            output_count INTEGER,
            video_model TEXT DEFAULT '',
            video_sub_mode TEXT DEFAULT 'text_to_video',
            video_ratio TEXT DEFAULT '',
            video_prompt TEXT DEFAULT '',
            video_upscale TEXT DEFAULT 'none',
            video_length INTEGER DEFAULT 10,
            video_output_count INTEGER DEFAULT 1,
            ref_path TEXT,
            ref_paths TEXT,
            start_image_path TEXT,
            end_image_path TEXT,
            queue_no INTEGER,
            status TEXT DEFAULT 'pending',
            assigned_account TEXT,
            error_message TEXT,
            output_path TEXT DEFAULT '',
            output_index INTEGER,
            is_retry INTEGER DEFAULT 0,
            retry_source TEXT DEFAULT '',
            progress_step TEXT DEFAULT '',
            progress_poll_count INTEGER DEFAULT 0,
            created_at TIMESTAMP
        )
    ''')

    # Lightweight migration for existing DBs.
    cursor.execute("PRAGMA table_info(jobs)")
    existing_cols = {str(row[1]) for row in cursor.fetchall()}
    cursor.execute("PRAGMA table_info(accounts)")
    existing_account_cols = {str(row[1]) for row in cursor.fetchall()}
    if "proxy" not in existing_account_cols:
        cursor.execute("ALTER TABLE accounts ADD COLUMN proxy TEXT DEFAULT ''")
    if "queue_no" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN queue_no INTEGER")
    if "job_type" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN job_type TEXT DEFAULT 'image'")
    if "video_model" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN video_model TEXT DEFAULT ''")
    if "video_sub_mode" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN video_sub_mode TEXT DEFAULT 'text_to_video'")
    if "video_ratio" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN video_ratio TEXT DEFAULT ''")
    if "video_prompt" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN video_prompt TEXT DEFAULT ''")
    if "video_upscale" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN video_upscale TEXT DEFAULT 'none'")
    if "video_length" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN video_length INTEGER DEFAULT 10")
    if "video_output_count" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN video_output_count INTEGER DEFAULT 1")
    if "ref_paths" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN ref_paths TEXT")
    if "start_image_path" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN start_image_path TEXT")
    if "end_image_path" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN end_image_path TEXT")
    if "output_path" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN output_path TEXT DEFAULT ''")
    if "output_index" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN output_index INTEGER")
    if "is_retry" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN is_retry INTEGER DEFAULT 0")
    if "retry_source" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN retry_source TEXT DEFAULT ''")
    if "progress_step" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN progress_step TEXT DEFAULT ''")
    if "progress_poll_count" not in existing_cols:
        cursor.execute("ALTER TABLE jobs ADD COLUMN progress_poll_count INTEGER DEFAULT 0")
    if "prompt_segments" not in existing_cols:
        # Ordered [text|ref] segments for positional inline reference markers
        # (Flow-native format). JSON list; NULL for plain/legacy jobs.
        cursor.execute("ALTER TABLE jobs ADD COLUMN prompt_segments TEXT")

    cursor.execute("UPDATE jobs SET job_type = 'image' WHERE job_type IS NULL OR TRIM(job_type) = ''")
    cursor.execute("UPDATE jobs SET video_model = '' WHERE video_model IS NULL")
    cursor.execute("UPDATE jobs SET video_sub_mode = 'text_to_video' WHERE video_sub_mode IS NULL OR TRIM(video_sub_mode) = ''")
    cursor.execute("UPDATE jobs SET video_ratio = '' WHERE video_ratio IS NULL")
    cursor.execute("UPDATE jobs SET video_prompt = '' WHERE video_prompt IS NULL")
    cursor.execute("UPDATE jobs SET video_upscale = 'none' WHERE video_upscale IS NULL OR TRIM(video_upscale) = ''")
    cursor.execute("UPDATE jobs SET video_output_count = COALESCE(output_count, 1) WHERE video_output_count IS NULL OR video_output_count < 1")
    cursor.execute("UPDATE jobs SET output_path = '' WHERE output_path IS NULL")
    cursor.execute("UPDATE jobs SET output_index = queue_no WHERE output_index IS NULL")
    cursor.execute("UPDATE jobs SET is_retry = 0 WHERE is_retry IS NULL")
    cursor.execute("UPDATE jobs SET retry_source = '' WHERE retry_source IS NULL")
    cursor.execute("UPDATE jobs SET progress_step = '' WHERE progress_step IS NULL")
    cursor.execute("UPDATE jobs SET progress_poll_count = 0 WHERE progress_poll_count IS NULL")

    # Backfill queue_no for old rows (stable by created_at order).
    cursor.execute("SELECT COALESCE(MAX(queue_no), 0) FROM jobs")
    next_queue_no = int(cursor.fetchone()[0] or 0)
    cursor.execute("SELECT id FROM jobs WHERE queue_no IS NULL ORDER BY created_at ASC")
    missing_rows = cursor.fetchall()
    for row in missing_rows:
        job_id = row[0]
        next_queue_no += 1
        cursor.execute("UPDATE jobs SET queue_no = ? WHERE id = ?", (next_queue_no, job_id))

    # App Settings Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS account_flags (
            account TEXT,
            flag TEXT,
            value TEXT,
            PRIMARY KEY (account, flag)
        )
    ''')

    # Persistent cache for uploaded reference images (survives app restart)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ref_media_cache (
            project_id TEXT,
            file_path TEXT,
            media_id TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (project_id, file_path)
        )
    ''')

    # Reference library — named character/location photos the user can tag
    # (@name) in prompts for consistent image generation. `name` is the tag
    # (unique, lower-cased), `category` is 'character' or 'location', and
    # `photo_path` is the local file uploaded to Flow (via maseQ) at gen time.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reference_library (
            name TEXT PRIMARY KEY,
            category TEXT DEFAULT 'character',
            photo_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    for key, value in DEFAULT_APP_SETTINGS.items():
        cursor.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value),
        )

    legacy_default_updates = {
        # NOTE: slots_per_account migration was REMOVED here. The intent
        # was to push pre-Fluent-UI users from the old default of 2/3 to
        # the new default of 5, but ensure_db() runs on every DB
        # connection — so explicitly-chosen 2/3 from the UI slider got
        # silently overwritten to 5 right after the user saved it.
        # Result: Parallel slider was non-functional for any value of
        # 2 or 3. Removed entirely — old-default users stay at 2/3
        # (still functional), new installs get 5 via DEFAULT_APP_SETTINGS.
        "same_account_stagger_seconds": {("1.5", "3", "3.0"): "1.0"},
        "global_stagger_min_seconds": {("0.5", "1", "1.0"): "0.3"},
        "global_stagger_max_seconds": {("1.0", "3", "3.0"): "0.6"},
        "max_retries": {("2", "2.0"): "3"},
        "max_auto_retries_per_job": {("2", "2.0"): "3"},
        "retry_base_delay_seconds": {("20", "20.0"): "10"},
        "auto_retry_base_delay_seconds": {("20", "20.0"): "10"},
        "recaptcha_account_cooldown_seconds": {("20", "20.0", "45", "45.0"): "15"},
        "browser_mode": {("headless",): "real_chrome"},
        "chrome_display": {("visible",): "headless"},
        "speed_profile": {("stable",): "fast"},
        "api_humanized_warmup_min_seconds": {("0.3", "1", "1.0", "3", "3.0", "4", "4.0"): "0.2"},
        "api_humanized_warmup_max_seconds": {("0.6", "2", "2.0", "4", "4.0", "5", "5.0"): "0.4"},
        "api_humanized_wait_ref_min_seconds": {("0.8", "1.5", "2", "2.0"): "0.3"},
        "api_humanized_wait_ref_max_seconds": {("1.6", "2.5", "3", "3.0"): "0.8"},
        "api_humanized_wait_no_ref_min_seconds": {("1", "1.0", "3", "3.0", "4", "4.0"): "0.3"},
        "api_humanized_wait_no_ref_max_seconds": {("2", "2.0", "4", "4.0", "5", "5.0"): "0.6"},
    }
    for key, replacements in legacy_default_updates.items():
        cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        current_value = str(row[0]) if row and row[0] is not None else ""
        for old_values, new_value in replacements.items():
            if current_value in old_values:
                cursor.execute("UPDATE app_settings SET value = ? WHERE key = ?", (new_value, key))
                break

    conn.commit()

def get_connection():
    ensure_db()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_output_directory():
    configured = str(get_setting("output_directory", "") or "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return str(get_outputs_dir())

def _is_lock_error(exc):
    msg = str(exc or "").lower()
    return "database is locked" in msg or "database table is locked" in msg or "database is busy" in msg

def _run_write(write_fn, retries=8, base_delay=0.12):
    last_exc = None
    for attempt in range(max(1, int(retries))):
        conn = get_connection()
        try:
            result = write_fn(conn)
            conn.commit()
            return result
        except sqlite3.OperationalError as exc:
            last_exc = exc
            try:
                conn.rollback()
            except Exception:
                pass
            if _is_lock_error(exc) and attempt < retries - 1:
                time.sleep(base_delay * (attempt + 1))
                continue
            raise
        finally:
            conn.close()
    if last_exc:
        raise last_exc

def add_account(name, session_path, proxy=""):
    try:
        _run_write(
            lambda conn: conn.execute(
                "INSERT INTO accounts (name, session_path, proxy) VALUES (?, ?, ?)",
                (name, session_path, str(proxy or "").strip()),
            )
        )
        return True
    except sqlite3.IntegrityError:
        return False

def get_accounts():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, session_path, proxy, status FROM accounts")
    accounts = cursor.fetchall()
    conn.close()
    return [
        {"id": a[0], "name": a[1], "session_path": a[2], "proxy": a[3] or "", "status": a[4]}
        for a in accounts
    ]

def remove_account(name):
    _run_write(lambda conn: conn.execute("DELETE FROM accounts WHERE name = ?", (name,)))

def remove_account_by_id(account_id):
    _run_write(lambda conn: conn.execute("DELETE FROM accounts WHERE id = ?", (int(account_id),)))

def update_account_name(old_name, new_name):
    try:
        _run_write(lambda conn: conn.execute("UPDATE accounts SET name = ? WHERE name = ?", (new_name, old_name)))
        return True
    except sqlite3.IntegrityError:
        return False

def update_account_name_by_id(account_id, new_name):
    try:
        _run_write(lambda conn: conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (new_name, account_id)))
        return True
    except sqlite3.IntegrityError:
        return False

def update_account_session_by_id(account_id, session_path, new_name=None):
    def _write(conn):
        if new_name is None:
            conn.execute(
                "UPDATE accounts SET session_path = ? WHERE id = ?",
                (session_path, int(account_id)),
            )
        else:
            conn.execute(
                "UPDATE accounts SET session_path = ?, name = ? WHERE id = ?",
                (session_path, new_name, int(account_id)),
            )

    try:
        _run_write(_write)
        return True
    except sqlite3.IntegrityError:
        return False

def update_account_proxy_by_id(account_id, proxy):
    _run_write(
        lambda conn: conn.execute(
            "UPDATE accounts SET proxy = ? WHERE id = ?",
            (str(proxy or "").strip(), int(account_id)),
        )
    )
    return True

def _normalize_ref_paths(ref_path=None, ref_paths=None):
    normalized = []

    raw_values = []
    if isinstance(ref_paths, str):
        stripped = ref_paths.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    raw_values.extend(parsed)
                elif isinstance(parsed, str):
                    raw_values.append(parsed)
            except Exception:
                raw_values.append(stripped)
    elif isinstance(ref_paths, (list, tuple, set)):
        raw_values.extend(list(ref_paths))
    elif ref_paths:
        raw_values.append(ref_paths)

    if ref_path:
        raw_values.insert(0, ref_path)

    seen = set()
    for value in raw_values:
        path = str(value or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return normalized

def _normalize_job_payload(
    job_id,
    prompt,
    model,
    aspect_ratio,
    output_count,
    ref_path,
    ref_paths=None,
    job_type="image",
    video_model="",
    video_sub_mode="text_to_video",
    video_ratio="",
    video_prompt="",
    video_upscale="none",
    video_length=10,
    video_output_count=1,
    start_image_path=None,
    end_image_path=None,
    output_index=None,
    is_retry=False,
    retry_source="",
    prompt_segments=None,
):
    normalized_job_type = str(job_type or "image").strip().lower()
    if normalized_job_type not in ("image", "video", "pipeline"):
        normalized_job_type = "image"
    normalized_output_count = max(1, int(output_count or 1))
    normalized_video_output_count = max(1, int(video_output_count or normalized_output_count))
    normalized_ref_paths = _normalize_ref_paths(ref_path=ref_path, ref_paths=ref_paths)
    normalized_ref_path = normalized_ref_paths[0] if normalized_ref_paths else None
    normalized_ref_paths_json = json.dumps(normalized_ref_paths) if normalized_ref_paths else None
    normalized_video_model = str(video_model or (model if normalized_job_type in ("video", "pipeline") else "")).strip()
    normalized_video_sub_mode = str(video_sub_mode or "text_to_video").strip().lower() or "text_to_video"
    normalized_video_ratio = str(video_ratio or (aspect_ratio if normalized_job_type == "video" else "")).strip()
    normalized_video_prompt = str(video_prompt or "").strip()
    normalized_video_upscale = str(video_upscale or "none").strip().lower() or "none"
    try:
        normalized_video_length = int(video_length or 10)
    except (TypeError, ValueError):
        normalized_video_length = 10
    if normalized_video_length <= 0:
        normalized_video_length = 10
    normalized_start_image_path = start_image_path if start_image_path else None
    normalized_end_image_path = end_image_path if end_image_path else None
    try:
        normalized_output_index = max(1, int(output_index)) if output_index is not None else None
    except Exception:
        normalized_output_index = None
    return {
        "job_id": str(job_id),
        "prompt": str(prompt),
        "job_type": normalized_job_type,
        "model": model,
        "aspect_ratio": aspect_ratio,
        "output_count": normalized_output_count,
        "video_model": normalized_video_model,
        "video_sub_mode": normalized_video_sub_mode,
        "video_ratio": normalized_video_ratio,
        "video_prompt": normalized_video_prompt,
        "video_upscale": normalized_video_upscale,
        "video_length": normalized_video_length,
        "video_output_count": normalized_video_output_count,
        "ref_path": normalized_ref_path,
        "ref_paths_json": normalized_ref_paths_json,
        "start_image_path": normalized_start_image_path,
        "end_image_path": normalized_end_image_path,
        "output_index": normalized_output_index,
        "is_retry": 1 if is_retry else 0,
        "retry_source": str(retry_source or "").strip(),
        "prompt_segments_json": (
            json.dumps(prompt_segments)
            if prompt_segments else None
        ),
    }


def add_job(
    job_id,
    prompt,
    model,
    aspect_ratio,
    output_count,
    ref_path,
    ref_paths=None,
    job_type="image",
    video_model="",
    video_sub_mode="text_to_video",
    video_ratio="",
    video_prompt="",
    video_upscale="none",
    video_length=10,
    video_output_count=1,
    start_image_path=None,
    end_image_path=None,
    output_index=None,
    is_retry=False,
    retry_source="",
    prompt_segments=None,
):
    assigned_queue_no = 0
    job_payload = _normalize_job_payload(
        job_id,
        prompt,
        model,
        aspect_ratio,
        output_count,
        ref_path,
        ref_paths=ref_paths,
        job_type=job_type,
        video_model=video_model,
        video_sub_mode=video_sub_mode,
        video_ratio=video_ratio,
        video_prompt=video_prompt,
        video_upscale=video_upscale,
        video_length=video_length,
        video_output_count=video_output_count,
        start_image_path=start_image_path,
        end_image_path=end_image_path,
        output_index=output_index,
        is_retry=is_retry,
        retry_source=retry_source,
        prompt_segments=prompt_segments,
    )

    def _op(conn):
        nonlocal assigned_queue_no
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(MAX(queue_no), 0) FROM jobs")
        assigned_queue_no = int(cursor.fetchone()[0] or 0) + 1
        cursor.execute(
            '''
            INSERT INTO jobs (
                id, prompt, job_type, model, aspect_ratio, output_count,
                video_model, video_sub_mode, video_ratio, video_prompt, video_upscale, video_length, video_output_count,
                ref_path, ref_paths, start_image_path, end_image_path,
                queue_no, output_index, is_retry, retry_source, prompt_segments, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                job_payload["job_id"],
                job_payload["prompt"],
                job_payload["job_type"],
                job_payload["model"],
                job_payload["aspect_ratio"],
                job_payload["output_count"],
                job_payload["video_model"],
                job_payload["video_sub_mode"],
                job_payload["video_ratio"],
                job_payload["video_prompt"],
                job_payload["video_upscale"],
                job_payload["video_length"],
                job_payload["video_output_count"],
                job_payload["ref_path"],
                job_payload["ref_paths_json"],
                job_payload["start_image_path"],
                job_payload["end_image_path"],
                assigned_queue_no,
                job_payload["output_index"] if job_payload["output_index"] is not None else assigned_queue_no,
                job_payload["is_retry"],
                job_payload["retry_source"],
                job_payload["prompt_segments_json"],
                datetime.now(),
            ),
        )

    _run_write(_op)
    return assigned_queue_no


def add_jobs_bulk(job_specs, progress_cb=None, should_stop=None):
    normalized_jobs = []
    for spec in list(job_specs or []):
        normalized_jobs.append(
            _normalize_job_payload(
                spec.get("job_id"),
                spec.get("prompt"),
                spec.get("model"),
                spec.get("aspect_ratio"),
                spec.get("output_count"),
                spec.get("ref_path"),
                ref_paths=spec.get("ref_paths"),
                job_type=spec.get("job_type", "image"),
                video_model=spec.get("video_model", ""),
                video_sub_mode=spec.get("video_sub_mode", "text_to_video"),
                video_ratio=spec.get("video_ratio", ""),
                video_prompt=spec.get("video_prompt", ""),
                video_upscale=spec.get("video_upscale", "none"),
                video_length=spec.get("video_length", 10),
                video_output_count=spec.get("video_output_count", 1),
                start_image_path=spec.get("start_image_path"),
                end_image_path=spec.get("end_image_path"),
                output_index=spec.get("output_index"),
                is_retry=spec.get("is_retry", False),
                retry_source=spec.get("retry_source", ""),
                prompt_segments=spec.get("prompt_segments"),
            )
        )

    if not normalized_jobs:
        return 0

    total = len(normalized_jobs)

    def _op(conn):
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(MAX(queue_no), 0) FROM jobs")
        next_queue_no = int(cursor.fetchone()[0] or 0)
        inserted = 0

        for index, job_payload in enumerate(normalized_jobs, start=1):
            if should_stop and should_stop():
                break

            next_queue_no += 1
            cursor.execute(
                '''
                INSERT INTO jobs (
                    id, prompt, job_type, model, aspect_ratio, output_count,
                    video_model, video_sub_mode, video_ratio, video_prompt, video_upscale, video_length, video_output_count,
                    ref_path, ref_paths, start_image_path, end_image_path,
                    queue_no, output_index, is_retry, retry_source, prompt_segments, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    job_payload["job_id"],
                    job_payload["prompt"],
                    job_payload["job_type"],
                    job_payload["model"],
                    job_payload["aspect_ratio"],
                    job_payload["output_count"],
                    job_payload["video_model"],
                    job_payload["video_sub_mode"],
                    job_payload["video_ratio"],
                    job_payload["video_prompt"],
                    job_payload["video_upscale"],
                    job_payload["video_length"],
                    job_payload["video_output_count"],
                    job_payload["ref_path"],
                    job_payload["ref_paths_json"],
                    job_payload["start_image_path"],
                    job_payload["end_image_path"],
                    next_queue_no,
                    job_payload["output_index"] if job_payload["output_index"] is not None else next_queue_no,
                    job_payload["is_retry"],
                    job_payload["retry_source"],
                    job_payload["prompt_segments_json"],
                    datetime.now(),
                ),
            )
            inserted += 1

            if progress_cb and (index == total or index % 25 == 0):
                progress_cb(index, total)

        return inserted

    return _run_write(_op)

def get_all_jobs():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, prompt, job_type, model, video_model, queue_no, status, assigned_account, error_message, output_count, video_output_count, "
        "output_path, output_index, is_retry, retry_source, progress_step, progress_poll_count, "
        "aspect_ratio, video_sub_mode, ref_path, ref_paths, start_image_path, video_prompt, "
        "end_image_path, video_ratio, video_upscale, video_length, prompt_segments "
        "FROM jobs ORDER BY queue_no ASC, created_at ASC"
    )
    jobs = cursor.fetchall()
    conn.close()
    return [
        {
            "id": j[0],
            "prompt": j[1],
            "job_type": j[2],
            "model": j[3],
            "video_model": j[4],
            "queue_no": j[5],
            "status": j[6],
            "account": j[7],
            "error": j[8],
            "output_count": j[9],
            "video_output_count": j[10],
            "output_path": j[11],
            "output_index": j[12],
            "is_retry": bool(j[13] or 0),
            "retry_source": j[14],
            "progress_step": j[15],
            "progress_poll_count": int(j[16] or 0),
            "aspect_ratio": j[17],
            "video_sub_mode": j[18],
            "ref_path": j[19],
            "ref_paths": j[20],
            "start_image_path": j[21],
            "video_prompt": j[22],
            "end_image_path": j[23],
            "video_ratio": j[24],
            "video_upscale": j[25],
            "video_length": int(j[26]) if (len(j) > 26 and j[26] is not None) else 10,
            "prompt_segments": j[27] if len(j) > 27 else None,
        }
        for j in jobs
    ]

def update_job_status(job_id, status, account=None, error=None):
    def _op(conn):
        normalized_status = str(status or "").strip().lower()
        set_clauses = ["status = ?"]
        params = [status]

        if account is not None and error is not None:
            set_clauses.extend(["assigned_account = ?", "error_message = ?"])
            params.extend([account, error])
        elif account is not None:
            set_clauses.append("assigned_account = ?")
            params.append(account)
        elif error is not None:
            set_clauses.append("error_message = ?")
            params.append(error)
        elif normalized_status in ("pending", "running", "completed"):
            set_clauses.append("error_message = ''")

        if normalized_status in ("pending", "running", "failed"):
            set_clauses.extend([
                "output_path = ''",
                "progress_step = ''",
                "progress_poll_count = 0",
            ])
        elif normalized_status == "completed":
            set_clauses.extend([
                "progress_step = ''",
                "progress_poll_count = 0",
            ])

        params.append(job_id)
        conn.execute(
            f"UPDATE jobs SET {', '.join(set_clauses)} WHERE id = ?",
            tuple(params),
        )
    _run_write(_op)

def reassign_account_jobs(account_name):
    """Reassign all pending/running jobs from an account back to unassigned.
    Returns count of reassigned jobs."""
    count = [0]
    def _op(conn):
        cursor = conn.execute(
            "UPDATE jobs SET assigned_account = NULL, status = 'pending', "
            "error_message = '' "
            "WHERE assigned_account = ? AND status IN ('pending', 'running')",
            (account_name,),
        )
        count[0] = cursor.rowcount
    _run_write(_op)
    return count[0]


def update_job_runtime_state(
    job_id,
    *,
    output_path=None,
    progress_step=None,
    progress_poll_count=None,
    clear_output=False,
    clear_progress=False,
):
    def _op(conn):
        set_clauses = []
        params = []

        if clear_output:
            set_clauses.append("output_path = ''")
        elif output_path is not None:
            set_clauses.append("output_path = ?")
            params.append(str(output_path or ""))

        if clear_progress:
            set_clauses.extend([
                "progress_step = ''",
                "progress_poll_count = 0",
            ])
        else:
            if progress_step is not None:
                set_clauses.append("progress_step = ?")
                params.append(str(progress_step or ""))
            if progress_poll_count is not None:
                try:
                    poll_value = max(0, int(progress_poll_count))
                except Exception:
                    poll_value = 0
                set_clauses.append("progress_poll_count = ?")
                params.append(poll_value)

        if not set_clauses:
            return

        params.append(job_id)
        conn.execute(
            f"UPDATE jobs SET {', '.join(set_clauses)} WHERE id = ?",
            tuple(params),
        )

    _run_write(_op)


def update_job_prompt(job_id, prompt):
    _run_write(
        lambda conn: conn.execute(
            "UPDATE jobs SET prompt = ? WHERE id = ?",
            (str(prompt or ""), job_id),
        )
    )


def update_job_model(job_id, model):
    """Overwrite the `model` field on a job. Used by the auto-fallback
    path that swaps Nano Banana ↔ Nano Banana Pro when a daily quota
    is hit on the current model."""
    _run_write(
        lambda conn: conn.execute(
            "UPDATE jobs SET model = ? WHERE id = ?",
            (str(model or ""), job_id),
        )
    )


def get_job_model(job_id):
    """Read the current `model` field for a job. Returns empty string
    if not found or on error."""
    try:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT model FROM jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            if row:
                return str(row[0] or "")
        finally:
            conn.close()
    except Exception:
        pass
    return ""


def get_job_by_id(job_id):
    """Return a single job's editable fields as a dict, or None."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, prompt, ref_paths, prompt_segments, status, "
            "output_index, queue_no, job_type FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "prompt": row[1], "ref_paths": row[2],
            "prompt_segments": row[3], "status": row[4],
            "output_index": row[5], "queue_no": row[6], "job_type": row[7],
        }
    finally:
        conn.close()


def regenerate_job(job_id, new_prompt=None, new_ref_paths=None,
                   new_segments=None, replace_prompt=False, move_to_top=True):
    """Reset a job to 'pending' so it re-runs, PRESERVING its output_index so the
    new image overwrites the same file (e.g. 14.jpg). When `replace_prompt` is
    True, also swap in an edited prompt with freshly-resolved references and
    positional segments. Unlike the failed-tab retry this does NOT flag the job
    as a retry, so it stays a normal queue task."""
    def _op(conn):
        cur = conn.cursor()
        cur.execute("SELECT queue_no, output_index FROM jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        if not row:
            return 0
        out_idx = row[1] if row[1] is not None else row[0]
        sets = [
            "status = 'pending'", "assigned_account = ''", "error_message = ''",
            "output_path = ''", "output_index = ?",
            "progress_step = ''", "progress_poll_count = 0",
        ]
        params = [out_idx]
        if move_to_top:
            cur.execute("SELECT COALESCE(MIN(queue_no), 1) FROM jobs")
            sets.append("queue_no = ?")
            params.append(int(cur.fetchone()[0] or 1) - 1)
        if replace_prompt:
            sets.append("prompt = ?")
            params.append(str(new_prompt or ""))
            sets.append("ref_paths = ?")
            params.append(json.dumps(new_ref_paths) if new_ref_paths else None)
            sets.append("prompt_segments = ?")
            params.append(json.dumps(new_segments) if new_segments else None)
        params.append(job_id)
        cur.execute("UPDATE jobs SET " + ", ".join(sets) + " WHERE id = ?", params)
        return 1
    return int(_run_write(_op) or 0)


def retry_failed_jobs_to_top(job_updates, retry_source="failed_tab"):
    updates = []
    for item in list(job_updates or []):
        job_id = str(item.get("job_id") or "").strip()
        if not job_id:
            continue
        updates.append({
            "job_id": job_id,
            "prompt": str(item.get("prompt") or "").strip(),
            "retry_source": str(item.get("retry_source") or retry_source or "").strip(),
        })

    if not updates:
        return 0

    def _op(conn):
        cursor = conn.cursor()
        updated = 0

        for item in updates:
            cursor.execute(
                "SELECT queue_no, output_index FROM jobs WHERE id = ?",
                (item["job_id"],),
            )
            row = cursor.fetchone()
            if not row:
                continue

            original_queue_no = row[0]
            original_output_index = row[1]
            # The job's canonical NUMBER = its output_index (e.g. 82). Retrying
            # sends it back to that exact slot: same queue position (so it shows
            # as #82) AND same output_index (so the saved file is 82.jpg).
            preserved_output_index = original_output_index if original_output_index is not None else original_queue_no
            preserved_queue_no = preserved_output_index

            cursor.execute(
                """
                UPDATE jobs
                SET prompt = ?,
                    status = 'pending',
                    assigned_account = '',
                    error_message = '',
                    output_path = '',
                    output_index = ?,
                    is_retry = 1,
                    retry_source = ?,
                    progress_step = '',
                    progress_poll_count = 0,
                    queue_no = ?
                WHERE id = ?
                """,
                (
                    item["prompt"],
                    preserved_output_index,
                    item["retry_source"],
                    preserved_queue_no,
                    item["job_id"],
                ),
            )
            updated += 1

        return updated

    return int(_run_write(_op) or 0)


def get_failed_jobs():
    conn = get_connection()
    cursor = conn.cursor()
    # Include failed jobs AND retrying jobs (is_retry=1, status pending/running)
    # so they stay visible in Failed tab until successfully completed.
    cursor.execute(
        '''
        SELECT id, queue_no, prompt, job_type, model, error_message,
               COALESCE(output_index, queue_no) as original_sno, status, is_retry
        FROM jobs
        WHERE status = 'failed'
           OR (is_retry = 1 AND status IN ('pending', 'running'))
        ORDER BY COALESCE(output_index, queue_no) ASC, created_at ASC
        '''
    )
    jobs = cursor.fetchall()
    conn.close()
    return [
        {
            "id": j[0],
            "queue_no": j[2 + 4],       # original_sno (output_index or queue_no)
            "prompt": j[2],
            "job_type": j[3],
            "model": j[4],
            "error": j[5],
            "status": j[7],
            "is_retry": bool(j[8] or 0),
        }
        for j in jobs
    ]

def clear_failed_jobs():
    """Delete only permanently failed jobs. Don't touch retrying jobs."""
    deleted_count = 0
    def _op(conn):
        nonlocal deleted_count
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'failed'")
        deleted_count = int(cursor.fetchone()[0] or 0)
        conn.execute("DELETE FROM jobs WHERE status = 'failed'")
    _run_write(_op)
    return deleted_count

def delete_job(job_id):
    _run_write(lambda conn: conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,)))

def clear_all_pending_jobs():
    _run_write(lambda conn: conn.execute("DELETE FROM jobs WHERE status = 'pending'"))

def clear_full_queue():
    # Clears pending and running (stuck) jobs
    _run_write(lambda conn: conn.execute("DELETE FROM jobs WHERE status IN ('pending', 'running')"))


def update_pending_jobs_generation_settings(
    model,
    aspect_ratio,
    output_count,
    ref_path,
    ref_paths=None,
    job_type="image",
    video_model="",
    video_sub_mode="text_to_video",
    video_ratio="",
    video_prompt="",
    video_upscale="none",
    video_length=10,
    video_output_count=1,
    start_image_path=None,
    end_image_path=None,
    filter_job_type=None,
    filter_video_sub_mode=None,
    include_references=False,
):
    updated_count = 0
    normalized_job_type = str(job_type or "image").strip().lower()
    if normalized_job_type not in ("image", "video", "pipeline"):
        normalized_job_type = "image"
    normalized_ref_paths = _normalize_ref_paths(ref_path=ref_path, ref_paths=ref_paths)
    normalized_ref_path = normalized_ref_paths[0] if normalized_ref_paths else None
    normalized_ref_paths_json = json.dumps(normalized_ref_paths) if normalized_ref_paths else None
    normalized_video_model = str(video_model or (model if normalized_job_type in ("video", "pipeline") else "")).strip()
    normalized_video_sub_mode = str(video_sub_mode or "text_to_video").strip().lower() or "text_to_video"
    normalized_video_ratio = str(video_ratio or (aspect_ratio if normalized_job_type == "video" else "")).strip()
    normalized_video_prompt = str(video_prompt or "").strip()
    normalized_video_upscale = str(video_upscale or "none").strip().lower() or "none"
    try:
        normalized_video_length = int(video_length or 10)
    except (TypeError, ValueError):
        normalized_video_length = 10
    if normalized_video_length <= 0:
        normalized_video_length = 10
    normalized_video_output_count = max(1, int(video_output_count or output_count or 1))
    normalized_start_image_path = start_image_path if start_image_path else None
    normalized_end_image_path = end_image_path if end_image_path else None

    def _op(conn):
        nonlocal updated_count
        cursor = conn.cursor()
        # Per-job reference/start/end images (ref_path, ref_paths,
        # start_image_path, end_image_path) are OWNED BY EACH JOB — for
        # bulk image→video every job carries a DIFFERENT image. A blanket
        # settings-sync (fired on queue start / settings change) must NOT
        # overwrite them with the single UI reference (which is usually
        # empty → None), or it silently wipes every job's attached image.
        # Only propagate the reference columns when the caller explicitly
        # asks for it (include_references=True).
        if include_references:
            query = [
                '''
                UPDATE jobs
                SET job_type = ?, model = ?, aspect_ratio = ?, output_count = ?,
                    ref_path = ?, ref_paths = ?, video_model = ?, video_sub_mode = ?, video_ratio = ?, video_prompt = ?, video_upscale = ?, video_length = ?, video_output_count = ?,
                    start_image_path = ?, end_image_path = ?
                WHERE status = 'pending'
                '''
            ]
            params = [
                normalized_job_type,
                model,
                aspect_ratio,
                int(output_count),
                normalized_ref_path,
                normalized_ref_paths_json,
                normalized_video_model,
                normalized_video_sub_mode,
                normalized_video_ratio,
                normalized_video_prompt,
                normalized_video_upscale,
                normalized_video_length,
                normalized_video_output_count,
                normalized_start_image_path,
                normalized_end_image_path,
            ]
        else:
            query = [
                '''
                UPDATE jobs
                SET job_type = ?, model = ?, aspect_ratio = ?, output_count = ?,
                    video_model = ?, video_sub_mode = ?, video_ratio = ?, video_prompt = ?, video_upscale = ?, video_length = ?, video_output_count = ?
                WHERE status = 'pending'
                '''
            ]
            params = [
                normalized_job_type,
                model,
                aspect_ratio,
                int(output_count),
                normalized_video_model,
                normalized_video_sub_mode,
                normalized_video_ratio,
                normalized_video_prompt,
                normalized_video_upscale,
                normalized_video_length,
                normalized_video_output_count,
            ]

        normalized_filter_job_type = str(filter_job_type or "").strip().lower()
        if normalized_filter_job_type:
            query.append("AND job_type = ?")
            params.append(normalized_filter_job_type)

        normalized_filter_sub_mode = str(filter_video_sub_mode or "").strip().lower()
        if normalized_filter_sub_mode:
            query.append("AND video_sub_mode = ?")
            params.append(normalized_filter_sub_mode)

        cursor.execute(" ".join(query), tuple(params))
        updated_count = cursor.rowcount if cursor.rowcount is not None else 0
    _run_write(_op)
    return max(0, int(updated_count))

def clear_all_jobs():
    deleted_count = 0
    def _op(conn):
        nonlocal deleted_count
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM jobs")
        deleted_count = cursor.fetchone()[0]
        conn.execute("DELETE FROM jobs")
    _run_write(_op)
    return deleted_count

def clear_completed_jobs():
    deleted_count = 0
    def _op(conn):
        nonlocal deleted_count
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'completed'")
        deleted_count = int(cursor.fetchone()[0] or 0)
        conn.execute("DELETE FROM jobs WHERE status = 'completed'")
    _run_write(_op)
    return deleted_count

def reset_running_jobs_to_pending():
    count = 0
    def _op(conn):
        nonlocal count
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'")
        count = cursor.fetchone()[0]
        conn.execute(
            "UPDATE jobs SET status = 'pending', assigned_account = NULL, error_message = NULL, "
            "output_path = '', progress_step = '', progress_poll_count = 0 WHERE status = 'running'"
        )
    _run_write(_op)
    return count


def set_setting(key, value):
    _run_write(
        lambda conn: conn.execute(
            '''
            INSERT INTO app_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            ''',
            (str(key), str(value)),
        )
    )


def get_setting(key, default=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return default
    return row[0]


def get_int_setting(key, default=0):
    raw = get_setting(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def get_float_setting(key, default=0.0):
    raw = get_setting(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def get_bool_setting(key, default=False):
    raw = get_setting(key, default)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def get_account_flag(account_name, flag):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT value FROM account_flags WHERE account = ? AND flag = ?",
        (str(account_name or "").strip(), str(flag or "").strip()),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def set_account_flag(account_name, flag, value):
    _run_write(
        lambda conn: conn.execute(
            """
            INSERT INTO account_flags (account, flag, value)
            VALUES (?, ?, ?)
            ON CONFLICT(account, flag) DO UPDATE SET value = excluded.value
            """,
            (
                str(account_name or "").strip(),
                str(flag or "").strip(),
                str(value),
            ),
        )
    )


def clear_account_flags(account_name):
    _run_write(
        lambda conn: conn.execute(
            "DELETE FROM account_flags WHERE account = ?",
            (str(account_name or "").strip(),),
        )
    )

# ── Reference media upload cache ──────────────────────────────────────────

def get_cached_media_id(project_id, file_path):
    """Look up a previously uploaded reference image media_id."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT media_id FROM ref_media_cache WHERE project_id = ? AND file_path = ?",
            (str(project_id or ""), str(file_path or "")),
        )
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_cached_media_id(project_id, file_path, media_id):
    """Store an uploaded reference image media_id in persistent cache."""
    _run_write(
        lambda conn: conn.execute(
            "INSERT OR REPLACE INTO ref_media_cache (project_id, file_path, media_id) VALUES (?, ?, ?)",
            (str(project_id or ""), str(file_path or ""), str(media_id or "")),
        )
    )


def clear_ref_media_cache(project_id=None):
    """Clear reference media cache. If project_id given, only that project."""
    if project_id:
        _run_write(
            lambda conn: conn.execute(
                "DELETE FROM ref_media_cache WHERE project_id = ?",
                (str(project_id),),
            )
        )
    else:
        _run_write(lambda conn: conn.execute("DELETE FROM ref_media_cache"))


# ── Reference library (named @character / @location photos) ────────────────

def _normalize_ref_name(name):
    """Tag names are matched case-insensitively and stored lower-cased,
    with any leading '@' stripped. Whitespace collapses to single spaces."""
    n = str(name or "").strip().lstrip("@").strip().lower()
    return " ".join(n.split())


def add_reference(name, category, photo_path):
    """Add or update a named reference. `category` is 'character' or
    'location'. Returns the normalized name that was stored."""
    norm = _normalize_ref_name(name)
    if not norm:
        raise ValueError("Reference name cannot be empty")
    cat = str(category or "character").strip().lower()
    if cat not in ("character", "location"):
        cat = "character"
    _run_write(
        lambda conn: conn.execute(
            '''
            INSERT INTO reference_library (name, category, photo_path)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                category = excluded.category,
                photo_path = excluded.photo_path
            ''',
            (norm, cat, str(photo_path or "")),
        )
    )
    return norm


def get_references(category=None):
    """List references (optionally filtered by category) as dicts:
    {name, category, photo_path}."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if category:
            cursor.execute(
                "SELECT name, category, photo_path FROM reference_library "
                "WHERE category = ? ORDER BY name ASC",
                (str(category).strip().lower(),),
            )
        else:
            cursor.execute(
                "SELECT name, category, photo_path FROM reference_library "
                "ORDER BY category ASC, name ASC"
            )
        return [
            {"name": r[0], "category": r[1], "photo_path": r[2]}
            for r in cursor.fetchall()
        ]
    finally:
        conn.close()


def get_reference_by_name(name):
    """Return one reference dict by (normalized) name, or None."""
    norm = _normalize_ref_name(name)
    if not norm:
        return None
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, category, photo_path FROM reference_library WHERE name = ?",
            (norm,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"name": row[0], "category": row[1], "photo_path": row[2]}
    finally:
        conn.close()


def delete_reference(name):
    """Delete a reference by (normalized) name."""
    norm = _normalize_ref_name(name)
    if not norm:
        return
    _run_write(
        lambda conn: conn.execute(
            "DELETE FROM reference_library WHERE name = ?", (norm,)
        )
    )


def delete_all_references():
    """Remove every reference from the library. Returns the count deleted."""
    def _op(conn):
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM reference_library")
        n = int(cur.fetchone()[0] or 0)
        cur.execute("DELETE FROM reference_library")
        return n
    return _run_write(_op)


# Initialize on import
ensure_db()
