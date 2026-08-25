import django, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'nodicbslite.settings'
django.setup()
from django.db import connection
cursor = connection.cursor()
cols_to_add = [
    ("chama_footer", "varchar(255)", "'NODi Core Banking System ver.2.0'"),
]
for col, dtype, default in cols_to_add:
    try:
        cursor.execute(f"ALTER TABLE administration_chamainfo ADD COLUMN {col} {dtype} DEFAULT {default}")
        print(f"Added {col}")
    except Exception as e:
        if 'already exists' in str(e):
            print(f"{col} already exists, skipping")
        else:
            print(f"Error adding {col}: {e}")
print("Done")
