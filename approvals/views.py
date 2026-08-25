"""
approvals/views.py — Simple maker-checker approval views.
Matches the nodicbs ApprovalService / ApprovalRequest pattern.
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages

from .models import ApprovalRequest
from .services import ApprovalService


def _is_checker(user):
    """Return True if the user can approve/reject requests."""
    role = getattr(user, 'role', None)
    return role in ('manager', 'admin') or user.is_superuser


@login_required
def approval_list(request):
    """List pending and recent approval requests."""
    status_filter = request.GET.get('status', 'pending')
    qs = ApprovalRequest.objects.select_related('maker', 'checker')
    if status_filter:
        qs = qs.filter(status=status_filter)
    return render(request, 'approvals/list.html', {
        'approval_requests': qs[:100],
        'status_filter': status_filter,
        'can_action': _is_checker(request.user),
        'status_choices': ApprovalRequest.STATUS_CHOICES,
    })


@login_required
def approval_detail(request, pk):
    """View a single approval request and allow approve/reject."""
    approval_req = get_object_or_404(
        ApprovalRequest.objects.select_related('maker', 'checker'), pk=pk
    )
    return render(request, 'approvals/detail.html', {
        'approval_request': approval_req,
        'can_action': (
            _is_checker(request.user) and approval_req.is_pending
        ),
    })


@login_required
def approval_action(request, pk):
    """Approve or reject a pending request (POST only)."""
    if request.method != 'POST':
        return redirect('approvals:detail', pk=pk)

    action = request.POST.get('action', '')
    note = request.POST.get('note', '')

    try:
        if action == 'approve':
            ApprovalService.approve(pk, request.user, note=note)
            messages.success(request, 'Request approved.')

            # Execute the approved action (e.g. loan disbursement)
            approval = ApprovalRequest.objects.get(pk=pk)
            _execute_approved_action(approval)

        elif action == 'reject':
            ApprovalService.reject(pk, request.user, note=note)
            messages.warning(request, 'Request rejected.')
        else:
            messages.error(request, 'Invalid action.')
    except ValueError as e:
        messages.error(request, str(e))

    return redirect('approvals:detail', pk=pk)


@login_required
def approval_cancel(request, pk):
    """Maker cancels their own pending request."""
    if request.method != 'POST':
        return redirect('approvals:detail', pk=pk)
    try:
        ApprovalService.cancel(pk, request.user)
        messages.info(request, 'Request cancelled.')
    except ValueError as e:
        messages.error(request, str(e))
    return redirect('approvals:list')


def _execute_approved_action(approval_req):
    """
    After approval, execute the corresponding action.
    Called automatically when a checker approves.

    For loan_disburse: runs the full double-entry disbursement
    (LoanTransaction, SaccoAccountsLedger GL, SavingsTransaction credit)
    inside one atomic block via loans.disbursement.execute_disbursement().
    """
    if approval_req.action_type == 'loan_disburse':
        try:
            from loans.models import LoanHistory
            from loans.disbursement import execute_disbursement

            loan = LoanHistory.objects.get(pk=approval_req.object_id)
            payload = approval_req.payload or {}

            if not loan.is_disbursed and payload:
                checker_name = (
                    approval_req.checker.get_full_name()
                    or approval_req.checker.username
                ) if approval_req.checker else 'system'

                # execute_disbursement does everything:
                # - bridging offset re-validation
                # - LoanTransaction entries (principal, interest, fees)
                # - GL double-entry via post_journal (Dr Loans Receivable,
                #   Cr Member Deposits, Cr Fee Income, Cr Interest Income)
                # - SavingsTransaction credit to member's withdrawable product
                # - marks loan is_approved + is_disbursed
                # - SMS notification + admin financial alert
                execute_disbursement(
                    loan=loan,
                    payload=payload,
                    checker_username=checker_name,
                )
        except Exception:
            # Don't block the approval status — disbursement error is logged
            # and can be retried via admin or re-submission.
            import logging
            logging.getLogger(__name__).exception(
                "Disbursement failed for ApprovalRequest #%s (loan %s)",
                approval_req.pk, approval_req.object_id,
            )
