from django.db import connection
cursor = connection.cursor()
# First, see what loans migrations ARE recorded
cursor.execute("SELECT name FROM django_migrations WHERE app='loans' ORDER BY name")
print('Currently applied:', [r[0] for r in cursor.fetchall()])
