"""
sms/utils.py
-------------
Celcom Africa bulk-SMS HTTP gateway client and notification-permission helpers.

Provider: Celcom Africa (iSMS)
Docs endpoint family:
    POST https://isms.celcomafrica.com/api/services/sendsms/     -> send SMS
    POST https://isms.celcomafrica.com/api/services/getdlr/      -> delivery report
    POST https://isms.celcomafrica.com/api/services/getbalance/  -> account balance

All requests use JSON bodies with these keys:
    apikey     - API key (Partner → API → generate API)
    partnerID  - numeric partner ID (Partner → API → Partner ID)
    shortcode  - approved sender ID (e.g. "EAKIBASACCO")
    mobile     - MSISDN in 2547XXXXXXXX form (bulk = comma-separated)
    message    - UTF-8 text (GSM-7 characters only)

A successful send response looks like:
    {"responses": [
        {"response-code": 200, "response-description": "Success",
         "mobile": 254703727272, "messageid": 8290842, "networkid": "1"}
    ]}

Any response-code other than 200 (per Celcom docs) is treated as failure.

Configured via Django settings (which read from .env, see settings.py):

    SMS_API_URL     = "https://isms.celcomafrica.com/api/services/sendsms/"
    SMS_API_KEY     = "<partner api key>"
    SMS_PARTNER_ID  = "1449"
    SMS_SHORTCODE   = "EAKIBASACCO"
    SMS_TIMEOUT     = 15                # optional, seconds
    SMS_DLR_URL     = "https://isms.celcomafrica.com/api/services/getdlr/"
    SMS_BALANCE_URL = "https://isms.celcomafrica.com/api/services/getbalance/"
"""

import logging
import re
from typing import Iterable, Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Celcom Africa response codes (from API_DOCUMENTATION)
# ═══════════════════════════════════════════════════════════════════════════

CELCOM_CODE_MESSAGES = {
    200:  "Success",
    1001: "Invalid sender ID",
    1002: "Network not allowed",
    1003: "Invalid mobile number",
    1004: "Low bulk credits",
    1005: "Failed. System error",
    1006: "Invalid credentials",
    1007: "Failed. System error",
    1008: "No delivery report",
    1009: "Unsupported data type",
    1010: "Unsupported request type",
    4090: "Internal error — try again in 5 minutes",
    4091: "No Partner ID is set",
    4092: "No API key provided",
    4093: "Details not found",
}


# ═══════════════════════════════════════════════════════════════════════════
# Bulk SMS Gateway (HTTP API)
# ═══════════════════════════════════════════════════════════════════════════

class SMSGatewayError(Exception):
    """Raised when the SMS provider returns a non-success response."""


class SMSGateway:
    """
    Celcom Africa bulk-SMS HTTP client.

    Uses a single HTTP session for keep-alive; call close() when done
    (or use as a context manager).

    Public surface (unchanged from previous provider):
      • send(phone, message) -> str | None (provider message ID on success)
      • is_configured() -> (bool, str)
      • normalise_phone(raw) -> str          (staticmethod)

    Extras (new — safe to use ad-hoc from admin / management commands):
      • get_delivery_report(message_id) -> dict
      • get_balance() -> dict
    """

    DEFAULT_SEND_URL    = "https://isms.celcomafrica.com/api/services/sendsms/"
    DEFAULT_DLR_URL     = "https://isms.celcomafrica.com/api/services/getdlr/"
    DEFAULT_BALANCE_URL = "https://isms.celcomafrica.com/api/services/getbalance/"

    def __init__(self,
                 api_url:    Optional[str] = None,
                 api_key:    Optional[str] = None,
                 partner_id: Optional[str] = None,
                 shortcode:  Optional[str] = None,
                 timeout:    int           = 15,
                 dlr_url:    Optional[str] = None,
                 balance_url:Optional[str] = None):
        self.api_url    = api_url    or getattr(settings, 'SMS_API_URL',    self.DEFAULT_SEND_URL)
        self.api_key    = api_key    or getattr(settings, 'SMS_API_KEY',    None)
        self.partner_id = str(partner_id or getattr(settings, 'SMS_PARTNER_ID', '') or '').strip()
        self.shortcode  = shortcode  or getattr(settings, 'SMS_SHORTCODE',  None)
        self.timeout    = int(getattr(settings, 'SMS_TIMEOUT', timeout) or timeout)
        self.dlr_url    = dlr_url    or getattr(settings, 'SMS_DLR_URL',     self.DEFAULT_DLR_URL)
        self.balance_url= balance_url or getattr(settings, 'SMS_BALANCE_URL', self.DEFAULT_BALANCE_URL)

        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Eastakiba-SMSWorker/1.0',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })

    # ── Context-manager sugar ────────────────────────────────────────────
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass

    # ── Config validation ────────────────────────────────────────────────
    def is_configured(self) -> tuple[bool, str]:
        missing = [name for name, val in [
            ('SMS_API_URL',    self.api_url),
            ('SMS_API_KEY',    self.api_key),
            ('SMS_PARTNER_ID', self.partner_id),
            ('SMS_SHORTCODE',  self.shortcode),
        ] if not val]
        if missing:
            return False, f"Missing settings: {', '.join(missing)}"
        return True, ''

    # ── Phone normalisation ──────────────────────────────────────────────
    @staticmethod
    def normalise_phone(raw: str) -> str:
        """
        Return the MSISDN in Celcom Africa expected form: 2547XXXXXXXX
        (12 digits, no +, no spaces).

        Accepts:
          '+254 712 345 678' -> '254712345678'
          '0712345678'       -> '254712345678'
          '712345678'        -> '254712345678'
          '254712345678'     -> '254712345678'
        """
        p = re.sub(r'[^\d+]', '', raw or '')
        if p.startswith('+'):
            p = p[1:]
        if p.startswith('0') and len(p) == 10:
            p = '254' + p[1:]
        # Handle 9-digit local (missing leading 0)
        if len(p) == 9 and p.startswith('7'):
            p = '254' + p
        return p

    # ── Send ─────────────────────────────────────────────────────────────
    def send(self, phone: str, message: str) -> Optional[str]:
        """
        Send a single SMS through Celcom Africa.

        Returns the provider `messageid` (as str) on success, or None if the
        provider omitted it. Raises SMSGatewayError on any failure.
        """
        phone = self.normalise_phone(phone)
        if not phone:
            raise SMSGatewayError('Empty or invalid phone number')

        payload = {
            'apikey':    self.api_key,
            'partnerID': self.partner_id,
            'shortcode': self.shortcode,
            'mobile':    phone,
            'message':   message,
            'pass_type': 'plain',   # per docs; 'bm5' would base64-encode
        }

        try:
            resp = self._session.post(
                self.api_url, json=payload, timeout=self.timeout,
            )
        except requests.Timeout:
            raise SMSGatewayError('Timeout contacting Celcom Africa SMS gateway')
        except requests.ConnectionError as e:
            raise SMSGatewayError(f'Connection error: {e}')
        except requests.RequestException as e:
            raise SMSGatewayError(f'Network error: {e}')

        if resp.status_code >= 400:
            raise SMSGatewayError(
                f'HTTP {resp.status_code}: {resp.text[:300]}'
            )

        return self._parse_send_response(resp, phone)

    # ── Response parsing ─────────────────────────────────────────────────
    @staticmethod
    def _parse_send_response(resp: requests.Response, phone: str) -> Optional[str]:
        """
        Celcom returns:
            {"responses": [{"response-code": 200, "response-description": "...",
                            "mobile": 254..., "messageid": 8290842, "networkid":"1"}]}
        For a single-recipient send, we inspect the first (and only) entry.
        """
        text = (resp.text or '').strip()

        try:
            body = resp.json()
        except ValueError:
            raise SMSGatewayError(f'Non-JSON response: {text[:300]}')

        # Some providers wrap responses differently — handle a couple of shapes.
        responses = None
        if isinstance(body, dict):
            responses = body.get('responses')
        if not responses and isinstance(body, list):
            responses = body

        if not responses:
            raise SMSGatewayError(f'No responses in payload: {text[:300]}')

        entry = responses[0] if isinstance(responses, list) else responses
        if not isinstance(entry, dict):
            raise SMSGatewayError(f'Malformed response entry: {text[:300]}')

        # Celcom sometimes typos "respose-code" in their own docs — accept both.
        code = entry.get('response-code', entry.get('respose-code'))
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            raise SMSGatewayError(f'Missing response-code: {text[:300]}')

        if code_int != 200:
            desc = entry.get('response-description') \
                or CELCOM_CODE_MESSAGES.get(code_int, 'Unknown error')
            raise SMSGatewayError(f'[{code_int}] {desc} (mobile={phone})')

        # Success — return the provider message ID (may be int or str)
        msg_id = entry.get('messageid') or entry.get('messageID')
        return str(msg_id) if msg_id is not None else None

    # ── Delivery report (optional, ad-hoc) ───────────────────────────────
    def get_delivery_report(self, message_id: str) -> dict:
        """
        Query DLR for a previously-sent message. Returns the raw JSON dict.
        Raises SMSGatewayError on transport failure.
        """
        payload = {
            'apikey':    self.api_key,
            'partnerID': self.partner_id,
            'messageID': str(message_id),
        }
        try:
            resp = self._session.post(self.dlr_url, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise SMSGatewayError(f'DLR request failed: {e}')
        if resp.status_code >= 400:
            raise SMSGatewayError(f'DLR HTTP {resp.status_code}: {resp.text[:200]}')
        try:
            return resp.json()
        except ValueError:
            raise SMSGatewayError(f'DLR non-JSON: {resp.text[:200]}')

    # ── Account balance (optional, ad-hoc) ───────────────────────────────
    def get_balance(self) -> dict:
        """Return raw provider JSON for current bulk-SMS credit balance."""
        payload = {'apikey': self.api_key, 'partnerID': self.partner_id}
        try:
            resp = self._session.post(self.balance_url, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise SMSGatewayError(f'Balance request failed: {e}')
        if resp.status_code >= 400:
            raise SMSGatewayError(f'Balance HTTP {resp.status_code}: {resp.text[:200]}')
        try:
            return resp.json()
        except ValueError:
            raise SMSGatewayError(f'Balance non-JSON: {resp.text[:200]}')


# ═══════════════════════════════════════════════════════════════════════════
# Notification permission helpers  (unchanged from previous version)
# ═══════════════════════════════════════════════════════════════════════════

def is_notification_allowed(customer) -> bool:
    """Temp override wins when set; otherwise fall back to individual default."""
    if customer.temp_notifications_setting is not None:
        return customer.temp_notifications_setting
    return customer.default_notifications_setting


def build_phone_permission_map(phones: Iterable[str]) -> dict[str, bool]:
    """
    Given a batch of phone numbers, return {normalised_phone: allowed?} for
    every phone that maps to a known Customer. Numbers not in the map are
    NOT customers (e.g. staff, external) and should be treated as allowed.
    """
    from customers.models import Customer
    normalised = {SMSGateway.normalise_phone(p) for p in phones if p}
    if not normalised:
        return {}

    qs = Customer.objects.filter(phone__in=normalised).only(
        'phone', 'default_notifications_setting', 'temp_notifications_setting',
    )
    return {c.phone: is_notification_allowed(c) for c in qs}


def build_email_permission_map(emails: Iterable[str]) -> dict[str, bool]:
    """{email: allowed?} for every email that maps to a Customer."""
    from customers.models import Customer
    clean = {e.strip().lower() for e in emails if e and e.strip()}
    if not clean:
        return {}

    qs = Customer.objects.filter(reg_email__in=clean).only(
        'reg_email',
        'default_notifications_setting',
        'temp_notifications_setting',
    )
    return {
        (c.reg_email or '').lower(): is_notification_allowed(c)
        for c in qs if c.reg_email
    }
