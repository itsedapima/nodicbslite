"""
approvals/services.py
----------------------
Business logic for creating and actioning ApprovalRequests.

CHANGES:
- Admin users can approve their own requests (configurable via
  settings.ADMIN_SELF_APPROVE = True)
- Added audit logging for all approval actions
- Added approval workflow for income/expense entries
"""

from django.conf import settings
from django.utils import timezone
from django.db import transaction as db_transaction
from django.contrib.contenttypes.models import ContentType

from .models import ApprovalRequest


# Configurable: whether admin users can approve their own requests
ADMIN_SELF_APPROVE = getattr(settings, 'ADMIN_SELF_APPROVE', True)


class ApprovalService:

    @staticmethod
    def submit(action_type, maker, obj=None, payload=None, note=''):
        """
        Maker submits an action for approval.

        If the same object already has a pending request of the same
        action_type, the old request is superseded (cancelled) so the
        same loan / transfer / etc. cannot be approved twice.
        """
        ct = None
        obj_id = None
        if obj is not None:
            ct = ContentType.objects.get_for_model(obj)
            obj_id = obj.pk

            # ── Cancel any existing pending request for this exact object ──
            stale_qs = ApprovalRequest.objects.filter(
                action_type=action_type,
                content_type=ct,
                object_id=obj_id,
                status='pending',
            )
            for stale in stale_qs:
                stale.status = 'cancelled'
                stale.actioned_at = timezone.now()
                stale.save(update_fields=['status', 'actioned_at'])
                _log_approval_event(
                    'APPROVAL_SUPERSEDED', stale, maker,
                    f"Superseded by re-submission of "
                    f"{stale.get_action_type_display()} for {obj}"
                )

        approval = ApprovalRequest.objects.create(
            action_type=action_type,
            maker=maker,
            content_type=ct,
            object_id=obj_id,
            payload=payload or {},
            maker_note=note,
        )

        # Audit trail
        _log_approval_event(
            'APPROVAL_SUBMITTED', approval, maker,
            f"Submitted {approval.get_action_type_display()} for approval"
        )

        return approval

    @staticmethod
    def approve(approval_id, checker, note=''):
        """
        Checker approves a pending request.

        Authorization rules:
          - Only manager/admin can approve
          - 4-eyes principle: maker != checker
          - EXCEPTION: if checker is admin AND settings.ADMIN_SELF_APPROVE is True,
            admin can approve their own requests (single-operator SACCO scenario)
        """
        checker_role = getattr(checker, 'role', None)
        is_admin = (checker_role == 'admin')

        if checker_role not in ('manager', 'admin') and not is_admin:
            raise ValueError("Only managers and admins can approve requests.")

        with db_transaction.atomic():
            try:
                approval = ApprovalRequest.objects.select_for_update().get(
                    pk=approval_id, status='pending'
                )
            except ApprovalRequest.DoesNotExist:
                raise ValueError(
                    "Approval request not found or is no longer pending."
                )

            # 4-eyes: non-admin maker cannot approve their own request.
            # Admin can self-approve when ADMIN_SELF_APPROVE is True.
            is_self_approve = (approval.maker_id == checker.id)

            if is_self_approve:
                if is_admin and ADMIN_SELF_APPROVE:
                    # Admin self-approval allowed — log it prominently
                    _log_approval_event(
                        'ADMIN_SELF_APPROVAL', approval, checker,
                        f"Admin {checker.username} approved their own request "
                        f"#{approval.pk} ({approval.get_action_type_display()}). "
                        f"This is permitted under ADMIN_SELF_APPROVE=True."
                    )
                else:
                    raise ValueError(
                        "The maker cannot approve their own request. "
                        "A different user must approve (4-eyes principle)."
                    )

            approval.checker = checker
            approval.checker_note = note
            approval.status = 'approved'
            approval.actioned_at = timezone.now()
            approval.save()

            _log_approval_event(
                'APPROVAL_APPROVED', approval, checker,
                f"Approved {approval.get_action_type_display()} #{approval.pk}"
            )

            return approval

    @staticmethod
    def reject(approval_id, checker, note=''):
        """Checker rejects a pending request."""
        checker_role = getattr(checker, 'role', None)
        is_admin = (checker_role == 'admin')

        if checker_role not in ('manager', 'admin') and not is_admin:
            raise ValueError("Only managers and admins can reject requests.")

        with db_transaction.atomic():
            try:
                approval = ApprovalRequest.objects.select_for_update().get(
                    pk=approval_id, status='pending'
                )
            except ApprovalRequest.DoesNotExist:
                raise ValueError(
                    "Approval request not found or is no longer pending."
                )

            is_self = (approval.maker_id == checker.id)
            if is_self and not (is_admin and ADMIN_SELF_APPROVE):
                raise ValueError("The maker cannot reject their own request.")

            approval.checker = checker
            approval.checker_note = note
            approval.status = 'rejected'
            approval.actioned_at = timezone.now()
            approval.save()

            _log_approval_event(
                'APPROVAL_REJECTED', approval, checker,
                f"Rejected {approval.get_action_type_display()} #{approval.pk}"
            )

            return approval

    @staticmethod
    def cancel(approval_id, user):
        """Maker cancels their own pending request."""
        with db_transaction.atomic():
            try:
                approval = ApprovalRequest.objects.select_for_update().get(
                    pk=approval_id, status='pending'
                )
            except ApprovalRequest.DoesNotExist:
                raise ValueError(
                    "Approval request not found or is no longer pending."
                )

            if approval.maker != user and not getattr(user, 'is_admin', False):
                raise ValueError("Only the maker or an admin can cancel this request.")

            approval.status = 'cancelled'
            approval.actioned_at = timezone.now()
            approval.save()

            _log_approval_event(
                'APPROVAL_CANCELLED', approval, user,
                f"Cancelled {approval.get_action_type_display()} #{approval.pk}"
            )

            return approval


def _log_approval_event(event, approval, user, details):
    """Log approval actions to the audit trail."""
    try:
        from audit.services import log_security_event
        log_security_event(
            event=event,
            actor=user,
            severity='warning' if 'SELF' in event else 'info',
            details=details,
            object_ref=f"ApprovalRequest #{approval.pk}",
        )
    except Exception:
        pass
