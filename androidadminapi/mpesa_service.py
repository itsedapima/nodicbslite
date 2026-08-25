"""
androidadminapi/mpesa_service.py
─────────────────────────────────
Safaricom Daraja API integration layer.

Provides:
  - get_access_token()       → OAuth token for all Daraja calls
  - initiate_b2c_payment()   → Business-to-Customer (loan disbursement)
  - initiate_stk_push()      → Lipa Na M-Pesa STK Push (collections)

All credentials come from Django settings (→ .env).
If credentials are empty, functions raise MpesaConfigError so the
caller can return a friendly error to the mobile app.
"""

import base64
import logging
from datetime import datetime
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  EXCEPTIONS
# ═══════════════════════════════════════════════════════════════════════════

class MpesaConfigError(Exception):
    """Raised when M-Pesa credentials are not configured."""


class MpesaApiError(Exception):
    """Raised when Safaricom API returns an error."""


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _get_base_url():
    env = getattr(settings, 'MPESA_ENVIRONMENT', 'sandbox')
    if env == 'production':
        return 'https://api.safaricom.co.ke'
    return 'https://sandbox.safaricom.co.ke'


def _check_credentials(*fields):
    """Verify that all required credential fields are non-empty."""
    missing = []
    for field in fields:
        val = getattr(settings, field, '')
        if not val:
            missing.append(field)
    if missing:
        raise MpesaConfigError(
            f"M-Pesa is not configured. Missing: {', '.join(missing)}. "
            f"Please contact your system administrator to set up M-Pesa "
            f"credentials in the environment configuration."
        )


def format_phone(phone):
    """
    Normalise a Kenyan phone number to 2547XXXXXXXX format.
    Accepts: 0712345678, +254712345678, 254712345678, 712345678
    """
    phone = str(phone).strip().replace(' ', '').replace('-', '')
    if phone.startswith('+'):
        phone = phone[1:]
    if phone.startswith('0'):
        phone = '254' + phone[1:]
    if len(phone) == 9 and phone[0] in '17':
        phone = '254' + phone
    return phone


# ═══════════════════════════════════════════════════════════════════════════
#  OAUTH ACCESS TOKEN
# ═══════════════════════════════════════════════════════════════════════════

def get_access_token():
    """
    Obtain an OAuth access token from Safaricom Daraja.
    Uses MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET.
    """
    _check_credentials('MPESA_CONSUMER_KEY', 'MPESA_CONSUMER_SECRET')

    consumer_key = settings.MPESA_CONSUMER_KEY
    consumer_secret = settings.MPESA_CONSUMER_SECRET
    base_url = _get_base_url()

    url = f'{base_url}/oauth/v1/generate?grant_type=client_credentials'

    try:
        response = requests.get(
            url,
            auth=(consumer_key, consumer_secret),
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        token = result.get('access_token')
        if not token:
            raise MpesaApiError(f"No access_token in response: {result}")
        return token
    except requests.RequestException as e:
        logger.error("M-Pesa OAuth failed: %s", e)
        raise MpesaApiError(f"Failed to obtain M-Pesa access token: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  B2C — BUSINESS TO CUSTOMER (Disbursement)
# ═══════════════════════════════════════════════════════════════════════════

def initiate_b2c_payment(phone, amount, remarks='Loan Disbursement',
                         occasion='LoanDisbursement'):
    """
    Send money from the organization's M-Pesa account to a customer's phone.

    Args:
        phone: Customer phone number (any Kenyan format)
        amount: Amount to send (Decimal or int, whole KES — no decimals)
        remarks: Transaction description (max 100 chars)
        occasion: Occasion field (max 100 chars)

    Returns:
        dict with Safaricom response including ConversationID,
        OriginatorConversationID, ResponseDescription

    Raises:
        MpesaConfigError: If B2C credentials are not configured
        MpesaApiError: If Safaricom returns an error
    """
    _check_credentials(
        'MPESA_CONSUMER_KEY', 'MPESA_CONSUMER_SECRET',
        'MPESA_B2C_SHORTCODE', 'MPESA_B2C_INITIATOR_NAME',
        'MPESA_B2C_SECURITY_CREDENTIAL',
        'MPESA_B2C_RESULT_URL', 'MPESA_B2C_TIMEOUT_URL',
    )

    token = get_access_token()
    base_url = _get_base_url()

    formatted_phone = format_phone(phone)
    # B2C amount must be integer (whole KES)
    int_amount = int(Decimal(str(amount)))

    payload = {
        'InitiatorName': settings.MPESA_B2C_INITIATOR_NAME,
        'SecurityCredential': settings.MPESA_B2C_SECURITY_CREDENTIAL,
        'CommandID': 'BusinessPayment',
        'Amount': int_amount,
        'PartyA': settings.MPESA_B2C_SHORTCODE,
        'PartyB': formatted_phone,
        'Remarks': remarks[:100],
        'QueueTimeOutURL': settings.MPESA_B2C_TIMEOUT_URL,
        'ResultURL': settings.MPESA_B2C_RESULT_URL,
        'Occasion': occasion[:100],
    }

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    url = f'{base_url}/mpesa/b2c/v3/paymentrequest'

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        result = response.json()

        logger.info("B2C response for %s (KES %s): %s",
                     formatted_phone, int_amount, result)

        # Safaricom returns ResponseCode "0" on success
        resp_code = result.get('ResponseCode', '')
        if str(resp_code) != '0':
            error_desc = result.get('ResponseDescription',
                         result.get('errorMessage', 'Unknown error'))
            raise MpesaApiError(f"B2C failed: {error_desc}")

        return result

    except requests.RequestException as e:
        logger.error("B2C request failed: %s", e)
        raise MpesaApiError(f"B2C request failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  STK PUSH — LIPA NA M-PESA (Collections)
# ═══════════════════════════════════════════════════════════════════════════

def initiate_stk_push(phone, amount, account_reference='Collection',
                      transaction_desc='Payment'):
    """
    Send an STK Push prompt to the customer's phone for payment collection.

    Args:
        phone: Customer phone number (any Kenyan format)
        amount: Amount to collect (Decimal or int, whole KES)
        account_reference: Account reference shown on customer's phone (max 12 chars)
        transaction_desc: Description (max 13 chars)

    Returns:
        dict with Safaricom response including MerchantRequestID,
        CheckoutRequestID, ResponseDescription, CustomerMessage

    Raises:
        MpesaConfigError: If STK credentials are not configured
        MpesaApiError: If Safaricom returns an error
    """
    _check_credentials(
        'MPESA_CONSUMER_KEY', 'MPESA_CONSUMER_SECRET',
        'MPESA_SHORTCODE', 'MPESA_PASSKEY',
        'MPESA_STK_CALLBACK_URL',
    )

    token = get_access_token()
    base_url = _get_base_url()

    formatted_phone = format_phone(phone)
    int_amount = int(Decimal(str(amount)))
    shortcode = settings.MPESA_SHORTCODE
    passkey = settings.MPESA_PASSKEY

    # Generate password: base64(shortcode + passkey + timestamp)
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    raw_password = f'{shortcode}{passkey}{timestamp}'
    password = base64.b64encode(raw_password.encode()).decode('utf-8')

    payload = {
        'BusinessShortCode': shortcode,
        'Password': password,
        'Timestamp': timestamp,
        'TransactionType': 'CustomerPayBillOnline',
        'Amount': int_amount,
        'PartyA': formatted_phone,
        'PartyB': shortcode,
        'PhoneNumber': formatted_phone,
        'CallBackURL': settings.MPESA_STK_CALLBACK_URL,
        'AccountReference': account_reference[:12],
        'TransactionDesc': transaction_desc[:13],
    }

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    url = f'{base_url}/mpesa/stkpush/v1/processrequest'

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        result = response.json()

        logger.info("STK Push response for %s (KES %s): %s",
                     formatted_phone, int_amount, result)

        resp_code = result.get('ResponseCode', '')
        if str(resp_code) != '0':
            error_desc = result.get('ResponseDescription',
                         result.get('errorMessage', 'Unknown error'))
            raise MpesaApiError(f"STK Push failed: {error_desc}")

        return result

    except requests.RequestException as e:
        logger.error("STK Push request failed: %s", e)
        raise MpesaApiError(f"STK Push request failed: {e}")
