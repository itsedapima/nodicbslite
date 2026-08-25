from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SecurityEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event', models.CharField(db_index=True, max_length=64)),
                ('severity', models.CharField(choices=[('info', 'Info'), ('warning', 'Warning'), ('critical', 'Critical')], db_index=True, default='info', max_length=10)),
                ('actor', models.CharField(blank=True, help_text="Username or 'system' / 'anonymous'.", max_length=150, null=True)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.CharField(blank=True, max_length=255, null=True)),
                ('object_ref', models.CharField(blank=True, help_text="Free-form ref to the object affected, e.g. 'Customer 1024' or 'Loan LN-0042'.", max_length=120, null=True)),
                ('details', models.TextField(blank=True, null=True)),
                ('email_sent', models.BooleanField(default=False, help_text='True if an admin email log row was created for this event.')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('actor_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='security_events', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Security Event',
                'verbose_name_plural': 'Security Events',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='securityevent',
            index=models.Index(fields=['event', 'created_at'], name='audit_secur_event_e1ee9a_idx'),
        ),
        migrations.AddIndex(
            model_name='securityevent',
            index=models.Index(fields=['actor', 'created_at'], name='audit_secur_actor_b5e0d2_idx'),
        ),
    ]
