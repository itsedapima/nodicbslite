import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.timezone import make_aware, get_current_timezone
from .models import MpesaNotification

logger = logging.getLogger(__name__)

# The expected Mpesa time format (e.g., 20260513160205)
MPESA_TIME_FORMAT = "%Y%m%d%H%M%S"

@csrf_exempt
def mpesa_integration(request):
    """
    Secure, production-grade API endpoint for raw M-Pesa C2B Validation/Confirmation hooks.
    """
    if request.method != "POST":
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Method Not Allowed"}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8"))
        # M-Pesa can send data raw or nested under an object depending on your API gateway setup
        payload = data.get("payload", data)

        # 1. Safe Datetime Parsing with Timezone Awareness
        trans_time_str = payload.get("TransTime", "")
        trans_time_dt = None
        
        if trans_time_str:
            try:
                # FIXED: Called directly on the imported class
                naive_dt = datetime.strptime(str(trans_time_str).strip(), MPESA_TIME_FORMAT)
                # Sacco servers must enforce local time zones (e.g., Africa/Nairobi) to match Safaricom timestamps
                trans_time_dt = make_aware(naive_dt, get_current_timezone())
            except ValueError:
                logger.warning(f"M-Pesa payload had anomalous time signature: {trans_time_str}")
                trans_time_dt = None

        # 2. Financial Precision: Parse Amount using Decimal instead of float
        try:
            trans_amount = Decimal(str(payload.get("TransAmount", "0.00")).strip())
        except (InvalidOperation, ValueError, TypeError):
            trans_amount = Decimal("0.00")

        # 3. Prevent duplicate processing of the same TransID (Idempotency)
        trans_id = payload.get("TransID", "").strip().upper()
        if MpesaNotification.objects.filter(trans_id=trans_id).exists():
            return JsonResponse({"ResultCode": 0, "ResultDesc": "Duplicate Transaction Ignored"})

        # 4. Persist to Staging Table
        MpesaNotification.objects.create(
            transaction_type=payload.get("TransactionType", ""),
            trans_id=trans_id,
            trans_time=trans_time_dt, 
            trans_amount=trans_amount,
            business_shortcode=payload.get("BusinessShortCode", ""),
            bill_ref_number=payload.get("BillRefNumber", ""),
            invoice_number=payload.get("InvoiceNumber", ""),
            org_account_balance=payload.get("OrgAccountBalance", ""),
            third_party_trans_id=payload.get("ThirdPartyTransID", ""),
            msisdn=payload.get("MSISDN", ""),
            first_name=payload.get("FirstName", ""),
            posted=False
        )

        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted successfully"}, status=200)
    
    except json.JSONDecodeError:
        logger.error("M-Pesa endpoint received malformed JSON payload.")
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Malformed JSON payload"}, status=400)
    except Exception as e:
        logger.exception(f"Critical crash on raw M-Pesa webhook collection: {str(e)}")
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Internal Server Processing Error"}, status=500)