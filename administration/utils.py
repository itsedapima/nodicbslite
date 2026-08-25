"""
administration/utils.py — Database Backup Utility
===================================================
Production-grade backup that works in BOTH environments:
  - Docker containers (Linux, pg_dump via apt)
  - Windows development (pg_dump via Program Files)

Sends SMS + Email notifications to all superusers.

Called by Django-Q2 scheduled task:
  Function: administration.utils.perform_database_backup
  Schedule: Every 24 hours (or as configured in BackupConfiguration)

Manual on-demand:
  Function: administration.utils.perform_database_backup_now
  (Bypasses interval check — for admin-triggered backups)
"""

import os
import shutil
import datetime
import zipfile
import subprocess
import threading
import logging
import time
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.contrib.auth import get_user_model

from .models import BackupConfiguration, BackupLog
from sms.models import SMSLog, EmailLog

logger = logging.getLogger(__name__)
User = get_user_model()

# ════════════════════════════════════════════════════════════════════════
#  ENVIRONMENT DETECTION
# ════════════════════════════════════════════════════════════════════════

IS_CONTAINER = os.path.exists("/.dockerenv")

from reportlab.platypus import Image, Spacer, Table
from reportlab.lib import colors
from .models import ChamaInfo  # Adjust import based on your app structure

def add_company_info(elements, width):
    """
    Adds company logo and details to the report.
    
    :param elements: List to which the elements will be appended.
    :param width: The total width of the report (to align text correctly).
    """
    # Fetch company Info
    company = ChamaInfo.objects.first()  # Assuming one company info record exists
    
    # Add company Logo (if available)
    if company and company.company_logo:
        logo_path = company.company_logo.path  # Get file path
        elements.append(Image(logo_path, width=80, height=80))
        elements.append(Spacer(1, 10))  # Add space after logo

    # company Details
    if company:
        company_details = f"{company.company_name}\n{company.company_address}\nTelephone: {company.company_contact}"
        elements.append(Table([[company_details]], colWidths=[width - 80], style=[
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 14),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
def _find_pg_dump() -> str:
    """
    Dynamically locate pg_dump binary across environments.

    Search order:
      1. PG_DUMP_PATH environment variable (explicit override)
      2. System PATH (Linux containers, Homebrew, etc.)
      3. Common Windows installation paths
      4. Common Linux package paths

    Returns:
        Full path to pg_dump binary

    Raises:
        FileNotFoundError if pg_dump cannot be found
    """
    # 1. Explicit env var override
    env_path = os.environ.get("PG_DUMP_PATH")
    if env_path and os.path.isfile(env_path):
        logger.info(f"[BACKUP] Using PG_DUMP_PATH: {env_path}")
        return env_path

    # 2. Check system PATH (works in Docker, Linux, Mac with postgres installed)
    pg_dump_on_path = shutil.which("pg_dump")
    if pg_dump_on_path:
        logger.info(f"[BACKUP] Found pg_dump on PATH: {pg_dump_on_path}")
        return pg_dump_on_path

    # 3. Common installation paths
    candidate_paths = []

    if os.name == "nt":
        # Windows: scan Program Files for PostgreSQL installations
        for base in [
            r"C:\Program Files\PostgreSQL",
            r"C:\Program Files (x86)\PostgreSQL",
        ]:
            if os.path.isdir(base):
                # Find all version folders (18, 17, 16, etc.) and pick newest
                versions = sorted(os.listdir(base), reverse=True)
                for ver in versions:
                    candidate = os.path.join(base, ver, "bin", "pg_dump.exe")
                    candidate_paths.append(candidate)
    else:
        # Linux/Mac: common package manager locations
        candidate_paths.extend([
            "/usr/bin/pg_dump",
            "/usr/local/bin/pg_dump",
            "/usr/lib/postgresql/16/bin/pg_dump",
            "/usr/lib/postgresql/15/bin/pg_dump",
            "/usr/lib/postgresql/14/bin/pg_dump",
        ])

    for path in candidate_paths:
        if os.path.isfile(path):
            logger.info(f"[BACKUP] Found pg_dump at: {path}")
            return path

    raise FileNotFoundError(
        "pg_dump not found. Install PostgreSQL client tools or set "
        "PG_DUMP_PATH environment variable. "
        "In Docker: apt-get install -y postgresql-client-16"
    )


def _get_db_settings() -> dict:
    """
    Get database connection settings, handling Docker networking.

    In Docker containers:
      - DB_HOST might be 'postgres' or 'db' (service name)
      - We connect directly to the postgres container, NOT via PgBouncer

    On Windows/local:
      - DB_HOST is usually 'localhost' or '127.0.0.1'

    Returns:
        dict with keys: NAME, USER, PASSWORD, HOST, PORT
    """
    db = settings.DATABASES["default"]

    host = db.get("HOST") or "localhost"
    port = db.get("PORT") or "5432"

    # In Docker: if using PgBouncer (port 6432), connect directly to
    # postgres container instead for pg_dump (PgBouncer doesn't support
    # the replication protocol pg_dump uses)
    if IS_CONTAINER:
        # Use BACKUP_DB_HOST env var if set, otherwise try common names
        host = os.environ.get("BACKUP_DB_HOST", "postgres")
        port = os.environ.get("BACKUP_DB_PORT", "5432")
        logger.info(f"[BACKUP] Docker mode: connecting to {host}:{port}")

    return {
        "NAME": db["NAME"],
        "USER": db["USER"],
        "PASSWORD": str(db.get("PASSWORD", "")),
        "HOST": host,
        "PORT": str(port),
    }


# ════════════════════════════════════════════════════════════════════════
#  NOTIFICATION HELPERS
# ════════════════════════════════════════════════════════════════════════

def _get_superusers():
    """
    Fetch all active superusers with their contact details.

    Returns:
        list of dicts: [{"username": ..., "email": ..., "phone": ...}, ...]
    """
    superusers = User.objects.filter(
        is_superuser=True, is_active=True
    ).values("username", "email", "phone")

    return list(superusers)


def _notify_admins_success(zip_filename, file_size_kb, duration_seconds):
    """
    Send SMS + Email to all superusers on successful backup.
    """
    superusers = _get_superusers()
    if not superusers:
        logger.warning("[BACKUP] No active superusers found for notification")
        return

    timestamp = timezone.now().strftime("%Y-%m-%d %H:%M")

    for admin in superusers:
        # ─── SMS Notification ───
        phone = admin.get("phone")
        if phone:
            sms_msg = (
                f"[NODiCBS BACKUP] Database backup completed successfully. "
                f"File: {zip_filename} ({file_size_kb:.1f} KB). "
                f"Duration: {duration_seconds:.1f}s. "
                f"Time: {timestamp}. "
                f"Retained on server for 7 days."
            )
            try:
                SMSLog.objects.create(
                    phone=phone,
                    message=sms_msg,
                    status="pending",
                    created_by="backup_scheduler",
                )
            except Exception as e:
                logger.error(f"[BACKUP] SMS queue failed for {phone}: {e}")

        # ─── Email Notification ───
        email = admin.get("email")
        if email:
            email_subject = f"[NODiCBS] Database Backup Successful — {zip_filename}"
            email_body = (
                f"Dear Administrator,\n\n"
                f"A scheduled database backup has been completed successfully.\n\n"
                f"BACKUP DETAILS:\n"
                f"  File: {zip_filename}\n"
                f"  Size: {file_size_kb:.2f} KB\n"
                f"  Duration: {duration_seconds:.1f} seconds\n"
                f"  Timestamp: {timestamp}\n"
                f"  Environment: {'Docker Container' if IS_CONTAINER else 'Windows/Local'}\n"
                f"  Retention: 7 days (auto-deleted after)\n\n"
                f"The backup is stored on the server and will be automatically "
                f"purged after the retention period.\n\n"
                f"Regards,\n"
                f"NODiCBS Backup System"
            )
            try:
                EmailLog.objects.create(
                    recipient_to=email,
                    subject=email_subject,
                    message_body=email_body,
                    is_html=False,
                    status="pending",
                    created_by="backup_scheduler",
                )
            except Exception as e:
                logger.error(f"[BACKUP] Email queue failed for {email}: {e}")

    logger.info(
        f"[BACKUP] Notifications sent to {len(superusers)} superuser(s)"
    )


def _notify_admins_failure(error_message):
    """
    Send SMS + Email to all superusers on backup FAILURE.
    """
    superusers = _get_superusers()
    if not superusers:
        return

    timestamp = timezone.now().strftime("%Y-%m-%d %H:%M")
    short_error = str(error_message)[:200]

    for admin in superusers:
        # ─── SMS Alert ───
        phone = admin.get("phone")
        if phone:
            sms_msg = (
                f"[NODiCBS ALERT] Database backup FAILED at {timestamp}. "
                f"Error: {short_error}. "
                f"Check server logs immediately."
            )
            try:
                SMSLog.objects.create(
                    phone=phone,
                    message=sms_msg,
                    status="pending",
                    created_by="backup_scheduler",
                )
            except Exception as e:
                logger.error(f"[BACKUP] SMS alert failed for {phone}: {e}")

        # ─── Email Alert ───
        email = admin.get("email")
        if email:
            email_subject = f"[NODiCBS ALERT] Database Backup FAILED — {timestamp}"
            email_body = (
                f"Dear Administrator,\n\n"
                f"⚠️ A scheduled database backup has FAILED.\n\n"
                f"ERROR DETAILS:\n"
                f"  Timestamp: {timestamp}\n"
                f"  Environment: {'Docker Container' if IS_CONTAINER else 'Windows/Local'}\n"
                f"  Error: {error_message}\n\n"
                f"RECOMMENDED ACTIONS:\n"
                f"  1. Check server logs: docker compose logs web | tail -50\n"
                f"  2. Verify database connectivity: docker compose ps\n"
                f"  3. Check disk space: df -h\n"
                f"  4. Verify pg_dump availability\n\n"
                f"Regards,\n"
                f"NODiCBS Backup System"
            )
            try:
                EmailLog.objects.create(
                    recipient_to=email,
                    subject=email_subject,
                    message_body=email_body,
                    is_html=False,
                    status="pending",
                    created_by="backup_scheduler",
                )
            except Exception as e:
                logger.error(f"[BACKUP] Email alert failed for {email}: {e}")


# ════════════════════════════════════════════════════════════════════════
#  CLEANUP & RETENTION
# ════════════════════════════════════════════════════════════════════════

def _cleanup_old_backups(backup_dir, retention_days=7):
    """
    Delete backup files older than retention period.

    Args:
        backup_dir: Path to backup directory
        retention_days: Number of days to keep backups (default 7)
    """
    if not os.path.exists(backup_dir):
        return

    cutoff_timestamp = time.time() - (retention_days * 86400)
    deleted_count = 0

    for filename in os.listdir(backup_dir):
        filepath = os.path.join(backup_dir, filename)

        if not os.path.isfile(filepath) or not filename.endswith(".zip"):
            continue

        if os.path.getmtime(filepath) < cutoff_timestamp:
            try:
                os.remove(filepath)
                deleted_count += 1
                logger.info(
                    f"[BACKUP] Retention: deleted '{filename}' "
                    f"(older than {retention_days} days)"
                )

                # Update BackupLog record
                BackupLog.objects.filter(file_name=filename).update(
                    message=(
                        f"Backup file automatically deleted from server "
                        f"after {retention_days}-day retention period."
                    )
                )
            except Exception as e:
                logger.error(f"[BACKUP] Failed to delete '{filename}': {e}")

    if deleted_count:
        logger.info(
            f"[BACKUP] Retention cleanup: {deleted_count} old backup(s) removed"
        )


# ════════════════════════════════════════════════════════════════════════
#  FILE-ON-DISK HELPER
# ════════════════════════════════════════════════════════════════════════

def backup_file_exists(file_name):
    """
    Check whether a backup zip file is still present on disk.

    Args:
        file_name: The zip filename stored in BackupLog.file_name

    Returns:
        bool
    """
    if not file_name:
        return False
    backup_dir = os.path.join(settings.BASE_DIR, "backups")
    file_path = os.path.join(backup_dir, file_name)
    return os.path.isfile(file_path)


# ════════════════════════════════════════════════════════════════════════
#  CORE BACKUP ENGINE  (shared by scheduled + on-demand callers)
# ════════════════════════════════════════════════════════════════════════

def _run_backup(config):
    """
    Internal engine: actually runs pg_dump, compresses, logs, notifies.

    This is the workhorse called by both the scheduled path and the
    on-demand path.  It never checks the interval — callers decide.

    Args:
        config: BackupConfiguration instance (used to stamp last_run)

    Returns:
        tuple: (success: bool, message: str)
    """
    start_time = time.time()

    # ─── Locate pg_dump ───
    try:
        pg_dump_path = _find_pg_dump()
    except FileNotFoundError as e:
        msg = str(e)
        logger.error(f"[BACKUP] {msg}")
        BackupLog.objects.create(status="error", message=msg)
        _notify_admins_failure(msg)
        return False, msg

    # ─── Database connection settings ───
    db = _get_db_settings()

    # ─── Prepare backup directory ───
    backup_dir = os.path.join(settings.BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_filename = f"{db['NAME']}_backup_{timestamp}.dump"
    dump_path = os.path.join(backup_dir, dump_filename)
    zip_filename = f"{dump_filename}.zip"
    zip_path = os.path.join(backup_dir, zip_filename)

    try:
        # ─── Run pg_dump ───
        env = os.environ.copy()
        env["PGPASSWORD"] = db["PASSWORD"]

        dump_command = [
            pg_dump_path,
            "-U", db["USER"],
            "-h", db["HOST"],
            "-p", db["PORT"],
            "-F", "c",        # Custom format (compressed, restorable)
            "-b",              # Include large objects
            "-v",              # Verbose
            "-f", dump_path,
            db["NAME"],
        ]

        logger.info(
            f"[BACKUP] Running: pg_dump -U {db['USER']} "
            f"-h {db['HOST']} -p {db['PORT']} {db['NAME']}"
        )

        result = subprocess.run(
            dump_command,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
        )

        if result.returncode != 0:
            error_output = result.stderr[:500] if result.stderr else "Unknown error"
            raise subprocess.CalledProcessError(
                result.returncode, "pg_dump", stderr=error_output
            )

        # ─── Compress to ZIP ───
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(dump_path, os.path.basename(dump_path))

        # ─── Calculate stats ───
        file_size_kb = os.path.getsize(zip_path) / 1024
        duration = time.time() - start_time

        # ─── Log success ───
        BackupLog.objects.create(
            file_name=zip_filename,
            status="success",
            file_size=f"{file_size_kb:.2f} KB",
            message=(
                f"Backup completed in {duration:.1f}s. "
                f"Environment: {'Docker' if IS_CONTAINER else 'Local'}. "
                f"Host: {db['HOST']}:{db['PORT']}."
            ),
        )

        # ─── Update last_run ───
        config.last_run = timezone.now()
        config.save(update_fields=["last_run"])

        logger.info(
            f"[BACKUP] ✅ Success: {zip_filename} "
            f"({file_size_kb:.1f} KB, {duration:.1f}s)"
        )

        # ─── Post-backup tasks (non-blocking) ───
        thread = threading.Thread(
            target=_post_backup_tasks,
            args=(
                zip_filename, zip_path, dump_path,
                file_size_kb, duration, backup_dir,
            ),
            daemon=True,
        )
        thread.start()

        return True, f"Backup successful: {zip_filename} ({file_size_kb:.1f} KB)"

    except subprocess.TimeoutExpired:
        msg = "pg_dump timed out after 600 seconds"
        logger.error(f"[BACKUP] {msg}")
        BackupLog.objects.create(
            status="error", file_name=dump_filename, message=msg,
        )
        _notify_admins_failure(msg)
        return False, msg

    except subprocess.CalledProcessError as e:
        msg = f"pg_dump failed (exit code {e.returncode}): {e.stderr}"
        logger.error(f"[BACKUP] {msg}")
        BackupLog.objects.create(
            status="error", file_name=dump_filename, message=msg[:500],
        )
        _notify_admins_failure(msg)
        return False, msg

    except Exception as e:
        msg = f"Backup failed: {e}"
        logger.error(f"[BACKUP] {msg}")
        BackupLog.objects.create(
            status="error", file_name=dump_filename, message=str(e)[:500],
        )
        _notify_admins_failure(msg)
        return False, msg

    finally:
        # Always clean up raw dump file
        if os.path.exists(dump_path):
            try:
                os.remove(dump_path)
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════════════
#  PUBLIC API — SCHEDULED  (called by Django-Q2)
# ════════════════════════════════════════════════════════════════════════

def perform_database_backup():
    """
    Scheduled entry-point.  Respects the interval gate so Django-Q2 can
    call this every minute / every hour without creating duplicate backups.

    Called by Django-Q2 scheduled task:
      Function: administration.utils.perform_database_backup

    Returns:
        tuple: (success: bool, message: str)
    """
    config = BackupConfiguration.objects.filter(is_active=True).first()

    if not config:
        msg = "Skipping backup: No active backup configuration found."
        logger.info(f"[BACKUP] {msg}")
        return False, msg

    # ─── Interval gate (scheduler only) ───
    now = timezone.now()
    interval_hours = config.interval_hours or 24

    if config.last_run:
        elapsed = now - config.last_run
        required = datetime.timedelta(hours=interval_hours)

        if elapsed < required:
            remaining = required - elapsed
            hours_left = int(remaining.total_seconds() // 3600)
            mins_left = int((remaining.total_seconds() % 3600) // 60)

            msg = (
                f"Backup skipped: interval not met. "
                f"Last run: {config.last_run.strftime('%Y-%m-%d %H:%M')}. "
                f"Next due in {hours_left}h {mins_left}m."
            )
            logger.info(f"[BACKUP] {msg}")
            return False, msg

    logger.info("[BACKUP] Interval condition met. Starting scheduled backup...")
    return _run_backup(config)


# ════════════════════════════════════════════════════════════════════════
#  PUBLIC API — ON-DEMAND  (called by admin "Run Backup Now" button)
# ════════════════════════════════════════════════════════════════════════

def perform_database_backup_now():
    """
    On-demand entry-point.  Skips the interval gate entirely so an admin
    can trigger a backup at any time from the dashboard.

    Returns:
        tuple: (success: bool, message: str)
    """
    config = BackupConfiguration.objects.filter(is_active=True).first()

    if not config:
        msg = "Cannot run backup: No active backup configuration found."
        logger.info(f"[BACKUP] {msg}")
        return False, msg

    logger.info("[BACKUP] On-demand backup requested by admin. Starting...")
    return _run_backup(config)


# ════════════════════════════════════════════════════════════════════════
#  POST-BACKUP BACKGROUND TASKS
# ════════════════════════════════════════════════════════════════════════

def _post_backup_tasks(
    zip_filename, zip_path, dump_path,
    file_size_kb, duration, backup_dir,
):
    """
    Post-backup tasks running in a background thread:
      1. Send success notifications to all superusers (SMS + Email)
      2. Remove raw dump file
      3. Clean up old backups (retention policy)
    """
    try:
        # 1. Notify admins
        _notify_admins_success(zip_filename, file_size_kb, duration)

        # 2. Remove raw dump (if not already removed in finally block)
        if os.path.exists(dump_path):
            os.remove(dump_path)
            logger.info(f"[BACKUP] Cleaned up raw dump: {dump_path}")

        # 3. Retention cleanup (delete backups older than 7 days)
        _cleanup_old_backups(backup_dir, retention_days=7)

    except Exception as e:
        logger.error(f"[BACKUP] Post-backup task error: {e}")
