import os
import subprocess
from django.core.management.base import BaseCommand
from django.conf import settings
from django.db import connection

class Command(BaseCommand):
    help = 'Wipes the local database schema and restores from a production dump.'

    def handle(self, *args, **options):
        # 1. Pull database settings
        db_settings = settings.DATABASES['default']
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

        # 3. Nuke the existing schema to clear ALL tables and constraints
        self.stdout.write(self.style.WARNING("Dropping and recreating the public schema to clear constraints..."))
        try:
            with connection.cursor() as cursor:
                # CASCADE forces the drop of all objects (tables, views, constraints) inside the schema
                cursor.execute("DROP SCHEMA public CASCADE;")
                cursor.execute("CREATE SCHEMA public;")
                # Re-grant standard permissions
                cursor.execute(f"GRANT ALL ON SCHEMA public TO {db_user};")
                cursor.execute("GRANT ALL ON SCHEMA public TO public;")
            
            # Close the Django database connection so pg_restore has full access
            connection.close()
            self.stdout.write(self.style.SUCCESS("Schema wiped successfully."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to wipe schema: {e}"))
            return

        self.stdout.write(self.style.WARNING(f"Starting fresh restore into {target_db}..."))

        # 4. Setup Environment for password
        env = os.environ.copy()
        env['PGPASSWORD'] = db_password

        # 5. Construct pg_restore command
        # We NO LONGER need --clean or --if-exists because the database is completely empty now
        command = [
            pg_restore_path,
            "-U", db_user,
            "-h", db_host,
            "-p", str(db_port),
            "-d", target_db,
            "--no-owner",
            "--no-privileges",
            backup_file
        ]

        try:
            # check=True will raise an exception if pg_restore fails
            subprocess.run(command, env=env, check=True)
            self.stdout.write(self.style.SUCCESS(f"Successfully restored {target_db} from {backup_file}"))
        except subprocess.CalledProcessError as e:
            self.stdout.write(self.style.ERROR(f"Restore failed! Error: {e}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"An unexpected error occurred: {e}"))