"""JSON log formatter for structured logging."""
import json
import logging
import traceback
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
        }
        if record.exc_info and record.exc_info[1]:
            log_entry['exception'] = traceback.format_exception(*record.exc_info)
        # Merge extra fields
        for key in ('event', 'ip', 'user', 'method', 'path', 'user_agent',
                     'status_code', 'chama'):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)
        return json.dumps(log_entry, default=str)
