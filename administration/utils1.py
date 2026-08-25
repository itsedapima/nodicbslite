import os
import datetime
import zipfile
import subprocess
import threading
import logging
from django.core.mail import EmailMessage
from django.conf import settings
from django.utils import timezone
from .models import BackupConfiguration, BackupLog

# Logger setup
logger = logging.getLogger(__name__)

def perform_database_backup():
    """Creates a database backup, compresses it, and sends it via email."""
    
    # 1. Read from our Database Configuration instead of settings.py
    config = BackupConfiguration.objects.filter(is_active=True).first()
    recipient_email = config.email_recipient if config else getattr(settings, 'BACKUP_EMAIL_RECIPIENT', None)

    if not recipient_email:
        msg = "Skipping backup: No recipient email configured."
        logger.warning(msg)
        BackupLog.objects.create(status='error', message=msg)
        return False, msg

    # Get DB Settings
    db_settings = settings.DATABASES['default']
    DB_NAME = db_settings['NAME']
    DB_USER = db_settings['USER']
    DB_PASSWORD = db_settings['PASSWORD']
    DB_HOST = db_settings['HOST'] or 'localhost'
    DB_PORT = db_settings.get('PORT', '5432') or '5432'

    # Paths
    BACKUP_DIR = os.path.join(settings.BASE_DIR, 'backups')
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Use .dump extension since we are using custom format (-F c)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"{DB_NAME}_backup_{timestamp}.dump"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)
    
    zip_filename = f"{backup_filename}.zip"
    zip_path = os.path.join(BACKUP_DIR, zip_filename)

    try:
        # 2. Run pg_dump securely
        env = os.environ.copy()
        env["PGPASSWORD"] = str(DB_PASSWORD)

        # Update this to match your PostgreSQL version
        pg_dump_path = r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe"

        dump_command = [
            pg_dump_path,
            "-U", DB_USER,
            "-h", DB_HOST,
            "-p", str(DB_PORT),
            "-F", "c",  # Custom archive format (great for restores)
            "-b",       # Include large objects
            "-v",       # Verbose output
            "-f", backup_path,
            DB_NAME
        ]

        subprocess.run(dump_command, env=env, check=True)

        # 3. Zip the backup file
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(backup_path, os.path.basename(backup_path))

        # 4. Log success to Dashboard UI
        file_size_kb = os.path.getsize(zip_path) / 1024
        BackupLog.objects.create(
            file_name=zip_filename, 
            status='success', 
            file_size=f"{file_size_kb:.2f} KB",
            message="Backup created and compressed."
        )

        # Update Last Run Time
        if config:
            config.last_run = timezone.now()
            config.save()

        # 5. Send Email and Cleanup in Background Thread
        thread = threading.Thread(
            target=send_backup_email_and_cleanup, 
            args=(zip_filename, zip_path, backup_path, recipient_email)
        )
        thread.start()

        return True, "Backup successfully initiated and is sending in the background."

    except Exception as e:
        logger.error(f"Database backup failed: {e}")
        BackupLog.objects.create(status='error', file_name=backup_filename, message=str(e))
        return False, str(e)


def send_backup_email_and_cleanup(zip_filename, zip_path, backup_path, recipient_email):
    """Sends the zipped backup and cleans up files AFTER sending."""
    try:
        email = EmailMessage(
            subject=f"Database Backup - {zip_filename}",
            body="Attached is the securely compressed database backup.",
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient_email]
        )
        email.attach_file(zip_path)
        email.send()

        logger.info(f"Backup file {zip_path} sent successfully.")

    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        # Update our DB log so we see the error on the dashboard
        log = BackupLog.objects.filter(file_name=zip_filename).first()
        if log:
            log.message = f"File created but email failed: {str(e)}"
            log.save()
            
    finally:
        # CRITICAL: Clean up happens here, whether email succeeds or fails!
        if os.path.exists(backup_path):
            os.remove(backup_path)
        if os.path.exists(zip_path):
            os.remove(zip_path)