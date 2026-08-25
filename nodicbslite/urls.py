from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.shortcuts import redirect
from django.http import HttpResponse
from django.contrib.auth import logout as auth_logout

import logging
_honeypot_log = logging.getLogger('security')


def _admin_logout(request):
    auth_logout(request)
    return redirect('accounts:login')


def _admin_honeypot(request, path=''):
    _honeypot_log.warning(
        'admin_honeypot_triggered',
        extra={
            'event': 'admin_honeypot',
            'ip': request.META.get('HTTP_X_REAL_IP', request.META.get('REMOTE_ADDR')),
            'method': request.method,
            'path': request.get_full_path(),
        },
    )
    return HttpResponse(status=404)


# ── Dynamic admin branding from ChamaInfo ──────────────────────────────
def _get_admin_branding():
    try:
        from administration.models import ChamaInfo
        info = ChamaInfo.objects.first()
        if info:
            return info.brand_name or 'NODi Lite', info.chama_name or ''
    except Exception:
        pass
    return 'NODi Lite', ''


admin.site.site_header = 'NODi Lite'
admin.site.site_title = 'NODi Lite'
admin.site.index_title = 'Dashboard'
admin.site.enable_nav_sidebar = False

_original_each_context = admin.site.each_context


def _patched_each_context(request):
    ctx = _original_each_context(request)
    brand, chama = _get_admin_branding()
    admin.site.site_header = brand
    admin.site.site_title = brand
    ctx['site_header'] = brand
    ctx['site_title'] = brand
    ctx['chama_name'] = chama
    return ctx


admin.site.each_context = _patched_each_context

_ADMIN_PATH = settings.ADMIN_URL_PATH.strip('/')

urlpatterns = [
    # Admin on secret path
    path(f'{_ADMIN_PATH}/logout/', _admin_logout, name='admin_logout'),
    path(f'{_ADMIN_PATH}/', admin.site.urls),
    # Honeypot
    path('admin/', _admin_honeypot),
    path('admin/<path:path>', _admin_honeypot),
    # App URLs
    path('accounts/', include('accounts.urls')),
    path('administration/', include('administration.urls')),
    path('accounting/', include('accounting.urls')),
    path('approvals/', include('approvals.urls')),
    path('customers/', include('customers.urls')),
    path('statements/', include('statements.urls')),
    path('sms/', include('sms.urls')),
    path('androidapi/', include('androidapi.urls')),
    path('androidadminapi/', include('androidadminapi.urls')),
    path('loans/', include('loans.urls')),
    path('reports/', include('reports.urls')),
    path('transactions/', include('transactions.urls')),
    path('dashboard/', include('dashboard.urls')),
    path('imports/', include('data_imports.urls')),
    path('', lambda request: redirect('accounts:login')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
