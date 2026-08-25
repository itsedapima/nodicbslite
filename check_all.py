from django.db import connection
cursor = connection.cursor()
for app in ['accounting', 'accounts', 'customers', 'loans', 'administration']:
    cursor.execute("SELECT name FROM django_migrations WHERE app=%s ORDER BY name", [app])
    print(f'{app}: {[r[0] for r in cursor.fetchall()]}')
