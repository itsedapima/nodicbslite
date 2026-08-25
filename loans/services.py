import polars as pl
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import (
    SavingsTransaction,
    LoanTransaction,
    LoanLimitGraduation,
    CustomerAccountsSetup,
    Customer,
)

# ---- Tuning knobs -----------------------------------------------------------
MIN_TENURE_DAYS      = 90     # < 3 months since reg_date => limit forced to 0
SAVINGS_WEIGHT       = 0.50   # was 0.60 — tightened
REPAYMENT_WEIGHT     = 0.35   # was 0.40 — tightened
OUTSTANDING_PENALTY  = 0.70   # each shilling of outstanding loan bites hard
NEUTRAL_DISCIPLINE   = 0.65   # customers with no loan history can't reach max on savings alone
# -----------------------------------------------------------------------------


def update_mobile_loan_limits():
    """
    Calculates and graduates loan limits for all active mobile loan products
    using Polars for high-performance vectorized operations.

    Tightened model:
        raw    = (net_savings * SAVINGS_WEIGHT)
               + (loan_repayments * REPAYMENT_WEIGHT)
               - (outstanding_loans * OUTSTANDING_PENALTY)
        scaled = raw * discipline_multiplier   # repayment_ratio (0..1), or NEUTRAL for no-history
        final  = clip(scaled, 0, product.max_loan_limit)

    Additional gates:
        - Customers whose reg_date is younger than MIN_TENURE_DAYS get final=0.
        - Customers whose customer_status is NOT 'Active' get final=0.
    """
    # 1. Active mobile loan configurations
    mobile_setups = CustomerAccountsSetup.objects.filter(is_mobile_loan=True, is_active=True)
    if not mobile_setups.exists():
        return {"status": "error", "message": "No active mobile loan setups found."}

    today = timezone.now().date()
    tenure_cutoff = today - timedelta(days=MIN_TENURE_DAYS)

    # 2. Pull minimal columns to Polars (customer_status included for dormancy gate)
    savings_qs   = SavingsTransaction.objects.values('cust_no', 'credit_amount', 'debit_amount')
    loans_qs     = LoanTransaction.objects.values('cust_no', 'credit_amount', 'debit_amount')
    customers_qs = Customer.objects.values('cust_no', 'full_name', 'reg_date', 'customer_status')

    if not customers_qs.exists():
        return {"status": "success", "message": "No customers to process."}

    df_savings   = pl.DataFrame(list(savings_qs))
    df_loans     = pl.DataFrame(list(loans_qs))
    df_customers = pl.DataFrame(list(customers_qs))

    # 3. Savings aggregate: net_savings = credits - debits
    if not df_savings.is_empty():
        df_savings_agg = df_savings.group_by('cust_no').agg([
            pl.col('credit_amount').sum().alias('total_savings_in'),
            pl.col('debit_amount').sum().alias('total_savings_out'),
        ]).with_columns(
            net_savings=(pl.col('total_savings_in') - pl.col('total_savings_out'))
        ).select(['cust_no', 'net_savings'])
    else:
        df_savings_agg = pl.DataFrame(schema={'cust_no': pl.String, 'net_savings': pl.Float64})

    # 4. Loans aggregate: repayments (credits), disbursed (debits), outstanding, discipline ratio
    if not df_loans.is_empty():
        df_loans_agg = df_loans.group_by('cust_no').agg([
            pl.col('credit_amount').sum().alias('total_loan_repayments'),
            pl.col('debit_amount').sum().alias('total_loan_disbursed'),
        ]).with_columns([
            # outstanding is clamped at zero — overpayment doesn't become a bonus
            (pl.col('total_loan_disbursed') - pl.col('total_loan_repayments'))
                .clip(lower_bound=0.0)
                .alias('outstanding_loans'),
            # repayment_ratio in [0,1]; neutral value when the customer has never borrowed
            pl.when(pl.col('total_loan_disbursed') > 0)
                .then(
                    (pl.col('total_loan_repayments') / pl.col('total_loan_disbursed'))
                    .clip(lower_bound=0.0, upper_bound=1.0)
                )
                .otherwise(pl.lit(NEUTRAL_DISCIPLINE))
                .alias('discipline'),
        ])
    else:
        df_loans_agg = pl.DataFrame(schema={
            'cust_no': pl.String,
            'total_loan_repayments': pl.Float64,
            'total_loan_disbursed': pl.Float64,
            'outstanding_loans': pl.Float64,
            'discipline': pl.Float64,
        })

    # 5. Join everything on cust_no (customers is the driving frame — we score every customer)
    df_combined = (
        df_customers
        .join(df_savings_agg, on='cust_no', how='left')
        .join(df_loans_agg,   on='cust_no', how='left')
        .with_columns([
            pl.col('net_savings').fill_null(0.0),
            pl.col('total_loan_repayments').fill_null(0.0),
            pl.col('total_loan_disbursed').fill_null(0.0),
            pl.col('outstanding_loans').fill_null(0.0),
            pl.col('discipline').fill_null(NEUTRAL_DISCIPLINE),
            # Normalise customer_status: null / missing → treat as inactive
            pl.col('customer_status').fill_null('').alias('customer_status'),
        ])
    )

    # 6. Compute the tightened raw score and apply discipline multiplier
    df_combined = df_combined.with_columns(
        calculated_limit=(
            (pl.col('net_savings') * SAVINGS_WEIGHT)
            + (pl.col('total_loan_repayments') * REPAYMENT_WEIGHT)
            - (pl.col('outstanding_loans') * OUTSTANDING_PENALTY)
        ) * pl.col('discipline')
    )

    # 7. Eligibility gates:
    #    a) Tenure — reg_date must be at least MIN_TENURE_DAYS old
    #    b) Status — customer_status must be exactly 'Active'
    #    Dormant, Deceased, Exited, or any non-Active status → limit = 0
    df_combined = df_combined.with_columns(
        eligible=(
            pl.col('reg_date').is_not_null()
            & (pl.col('reg_date') <= tenure_cutoff)
            & (pl.col('customer_status').str.to_lowercase() == 'active')
        )
    )

    # 8. Per-product capping and record building
    new_limits = []
    for setup in mobile_setups:
        max_limit = float(setup.max_loan_limit)

        df_product = df_combined.with_columns(
            final_limit=pl.when(~pl.col('eligible'))
                .then(0.0)
                .when(pl.col('calculated_limit') <= 0)
                .then(0.0)
                .when(pl.col('calculated_limit') > max_limit)
                .then(max_limit)
                .otherwise(pl.col('calculated_limit'))
        )

        for row in df_product.iter_rows(named=True):
            new_limits.append(
                LoanLimitGraduation(
                    cust_no=str(row['cust_no']),
                    full_name=row['full_name'] or "Unknown",
                    loan_product=setup.account_name,
                    graduation_date=today,
                    amount=row['final_limit'],
                )
            )

    if new_limits:
        with transaction.atomic():
            LoanLimitGraduation.objects.bulk_create(new_limits, batch_size=5000)

    return {
        "status": "success",
        "message": (
            f"Updated limits for {mobile_setups.count()} products across "
            f"{df_combined.height} profiles "
            f"(tenure gate: {MIN_TENURE_DAYS} days)."
        ),
    }