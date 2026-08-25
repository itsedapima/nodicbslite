from administration.models import ChamaInfo


def chama_branding(request):
    """
    Injects flat branding variables into every template:
        {{ brand_name }}   {{ brand_footer }}   {{ chama_name }}
    """
    info = ChamaInfo.objects.first()
    if info:
        return {
            'brand_name': info.brand_name or '',
            'brand_footer': info.brand_footer or '',
            'chama_name': info.chama_name or '',
        }
    return {
        'brand_name': 'NODi Lite',
        'brand_footer': 'NODi Lite Chama System',
        'chama_name': '',
    }


def pending_approvals_count(request):
    """
    Injects pending approval count for managers/admins into every template.
    Displays as a badge in the navigation bar.
    """
    count = 0
    if request.user.is_authenticated:
        from accounts.decorators import ROLE_HIERARCHY
        user_level = ROLE_HIERARCHY.get(getattr(request.user, 'role', 'customer'), 0)
        if getattr(request.user, 'is_admin', False):
            user_level = ROLE_HIERARCHY['admin']
        if user_level >= ROLE_HIERARCHY.get('manager', 3):
            try:
                from approvals.models import ApprovalRequest
                count = ApprovalRequest.objects.filter(status='pending').count()
            except Exception:
                count = 0
    return {'pending_approval_count': count}
