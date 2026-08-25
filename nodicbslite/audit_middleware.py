"""Audit middleware — logs every authenticated request for security forensics."""
import logging
import time

logger = logging.getLogger('audit')


class AuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.monotonic()
        response = self.get_response(request)
        duration_ms = (time.monotonic() - start) * 1000

        if request.user.is_authenticated and not request.path.startswith('/static'):
            logger.info(
                'request_audit',
                extra={
                    'event': 'request',
                    'user': request.user.username,
                    'method': request.method,
                    'path': request.get_full_path(),
                    'status_code': response.status_code,
                    'ip': request.META.get('HTTP_X_REAL_IP',
                                           request.META.get('REMOTE_ADDR')),
                    'duration_ms': round(duration_ms, 1),
                },
            )
        return response
