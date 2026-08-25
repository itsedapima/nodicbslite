import os
import subprocess
from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = 'Restores the production dump into the backup/development database'

    def handle(self, *args, **options):
        # 1. Pull settings as defined in your prompt
        db_settings = settings.DATABASES['default']
        
        # We use BACKUP_DB_NAME as the target for the restore
        target_db = db_settings.get('DB_NAME', 'smis_db')
        db_user = db_settings['USER']
        db_password = db_settings['PASSWORD']
        db_host = db_settings['HOST'] or 'localhost'
        db_port = db_settings.get('PORT', '5432') or '5432'
        
        # Paths
        pg_restore_path = r"C:\Program Files\PostgreSQL\18\bin\pg_restore.exe"
        backup_file = r"C:\Users\ADMIN\Desktop\Python\DB_BACKUP\eastakiba_db.dump"

        # 2. Check if backup file exists
        if not os.path.exists(backup_file):
            self.stdout.write(self.style.ERROR(f"Backup file not found at: {backup_file}"))
            return

        self.stdout.write(self.style.WARNING(f"Starting restore into {target_db}..."))

        # 3. Setup Environment for password
        env = os.environ.copy()
        env['PGPASSWORD'] = db_password

        # 4. Construct pg_restore command
        # --clean: Drops existing objects before recreating
        # --if-exists: Prevents errors during clean if DB is empty
        # --no-owner: Makes you the owner of the local tables
        # --no-privileges: Skips production-specific permissions
        command = [
            pg_restore_path,
            "-U", db_user,
            "-h", db_host,
            "-p", str(db_port),
            "-d", target_db,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            backup_file
        ]

        try:
            subprocess.run(command, env=env, check=True)
            self.stdout.write(self.style.SUCCESS(f"Successfully restored {target_db} from {backup_file}"))
        except subprocess.CalledProcessError as e:
            self.stdout.write(self.style.ERROR(f"Restore failed! Error: {e}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"An unexpected error occurred: {e}"))