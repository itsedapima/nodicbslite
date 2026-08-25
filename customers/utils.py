# customers/utils.py
from django.utils import timezone
from datetime import timedelta
from django.db.models import Count, Q, Max
from .models import Customer, CustomerStats
from transactions.models import SavingsTransaction, LoanTransaction
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.views.decorators.http import require_GET, require_http_methods
@require_GET
def customer_search_api(request):
    """AJAX live search endpoint."""
    q = (request.GET.get("q") or "").strip()
    if not q:
        return JsonResponse({"results": []})
    
    qs = Customer.objects.all()
    if q.isdigit():
        qs = qs.filter(Q(cust_no__exact=q) | Q(national_id__icontains=q))
    else:
        qs = qs.filter(full_name__icontains=q)
    
    qs = qs.order_by("cust_no")[:5]
    
    results = []
    for c in qs:
        results.append({
            "cust_no": c.cust_no,
            "full_name": c.full_name,
            "national_id": getattr(c, "national_id", ""),
            "phone": getattr(c, "phone", "")
        })
    return JsonResponse({"results": results})

def update_customer_statistics():
    now = timezone.now()
    three_months_ago = now - timedelta(days=90)
    
    # Age calculations logic
    def get_age_q(min_age, max_age=None):
        today = timezone.now().date()
        if max_age:
            return Q(dob__lte=today - timedelta(days=min_age*365), 
                     dob__gt=today - timedelta(days=(max_age+1)*365))
        return Q(dob__lte=today - timedelta(days=min_age*365))

    # Dormancy Logic: Find members with transactions in last 3 months
    active_cust_nos = set(SavingsTransaction.objects.filter(tr_date__gte=three_months_ago).values_list('cust_no', flat=True))
    active_cust_nos.update(LoanTransaction.objects.filter(tr_date__gte=three_months_ago).values_list('cust_no', flat=True))
    
    # ── 1. Update status: Active -> Dormant ─────────────────────────────────────
    # If customer is currently marked 'active' but has NO transactions in the last 3 months
    Customer.objects.filter(
        customer_status='active'
    ).exclude(
        cust_no__in=active_cust_nos
    ).update(customer_status='dormant')

    # ── 2. Update status: Dormant -> Active (Unflagging) ────────────────────────
    # If customer is currently marked 'dormant' but HAS transaction activity
    Customer.objects.filter(
        customer_status='dormant',
        cust_no__in=active_cust_nos
    ).update(customer_status='active')

    # ── 3. Aggregate statistics from the newly updated database state ───────────
    stats_data = Customer.objects.aggregate(
        total=Count('id'),
        churches=Count('id', filter=Q(customer_type='church')),
        groups=Count('id', filter=Q(customer_type='group')),
        male=Count('id', filter=Q(gender='Male')),
        female=Count('id', filter=Q(gender='Female')),
        deceased=Count('id', filter=Q(customer_status='deceased')),
        active_status=Count('id', filter=Q(customer_status='active')),
        dormant_status=Count('id', filter=Q(customer_status='dormant')),
        a18_35=Count('id', filter=get_age_q(18, 35)),
        a35_50=Count('id', filter=get_age_q(35, 50)),
        a50_60=Count('id', filter=get_age_q(50, 60)),
        a70plus=Count('id', filter=get_age_q(70)),
    )

    # ── 4. Save statistics ──────────────────────────────────────────────────────
    stats, created = CustomerStats.objects.get_or_create(id=1)
    stats.total_members = stats_data['total']
    stats.churches = stats_data['churches']
    stats.groups = stats_data['groups']
    stats.male = stats_data['male']
    stats.female = stats_data['female']
    stats.age_18_35 = stats_data['a18_35']
    stats.age_35_50 = stats_data['a35_50']
    stats.age_50_60 = stats_data['a50_60']
    stats.age_above_70 = stats_data['a70plus']
    stats.active = stats_data['active_status']
    stats.dormant = stats_data['dormant_status']
    stats.deceased = stats_data['deceased']
    stats.save()