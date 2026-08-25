# accounting/management/commands/load_accounts.py
"""
Seeds the full SACCO chart of accounts into the database.

Includes every GL code referenced by the disbursement engine,
M-Pesa posting, interest batch, and the standard SACCO expense/income
categories. Also creates a zero SaccoAccountBalance for each new
account so post_journal never fails on a missing balance row.

Usage:
    python manage.py load_accounts           # create missing, skip existing
    python manage.py load_accounts --reset   # wipe & re-seed from scratch

Safe to run multiple times — existing accounts are skipped.
"""

from django.core.management.base import BaseCommand
from accounting.models import SaccoAccount, SaccoAccountBalance


class Command(BaseCommand):
    help = "Load the full SACCO Chart of Accounts into the database"

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset', action='store_true',
            help='Delete ALL existing accounts and re-seed from scratch.',
        )

    def handle(self, *args, **options):
        # Format: (account_code, account_name, account_group, is_cash_account)
        accounts_data = [
            # ====================== INCOME ACCOUNTS ======================
            ("900-110000", "Normal Loan Interest",              "Income", False),
            ("900-110010", "Mobile Loan Interest",              "Income", False),
            ("900-110011", "Development Loan Interest",         "Income", False),
            ("900-110012", "Emergency Loan Interest",           "Income", False),
            ("900-110013", "Instant Loan / Dividend Advance Interest", "Income", False),
            ("900-110014", "Mobile Loan Plus Interest",         "Income", False),
            ("900-111000", "Loan Top-up Charges",               "Income", False),
            ("900-111001", "Loan Appraisal Fee",                "Income", False),
            ("900-111002", "Loan Insurance Fee",                "Income", False),
            ("900-111003", "Loan Bridging Fee",                 "Income", False),
            ("900-111004", "SMS Charge Income",                 "Income", False),
            ("900-122000", "M-Pesa Gateway Commission",         "Income", False),
            ("900-123000", "Lump Sum Deposit Fee",              "Income", False),
            ("900-124000", "Registration / Membership Fee",     "Income", False),
            ("900-124002", "Withdrawal Charge / Transaction Fee","Income", False),
            ("900-124003", "Mobile Disbursement Fee",           "Income", False),
            ("900-124004", "Penalty Income",                    "Income", False),
            ("900-124005", "Legal Fee Income",                  "Income", False),
            ("900-124006", "Other Fee Income",                  "Income", False),

            # ====================== EXPENDITURE ACCOUNTS ======================
            ("900-301000", "Interest on Deposits Expense",      "Expenditure", False),
            ("900-302000", "Fixed Deposit Interest Expense",    "Expenditure", False),
            ("900-303000", "Dividend Expense",                  "Expenditure", False),
            ("900-304000", "Bank Charges",                      "Expenditure", False),
            ("900-305000", "M-Pesa Charges",                    "Expenditure", False),
            ("900-306000", "Legal Fee Expense",                 "Expenditure", False),
            ("900-306001", "Other Expenses",                    "Expenditure", False),
            ("900-309000", "Salaries & Wages",                  "Expenditure", False),
            ("900-309001", "NSSF Contribution",                 "Expenditure", False),
            ("900-309002", "SHIF Contribution",                 "Expenditure", False),
            ("900-309003", "Allowances",                        "Expenditure", False),
            ("900-310003", "Ushirika Day",                      "Expenditure", False),
            ("900-310004", "AGM & SGM",                         "Expenditure", False),
            ("900-310005", "Public Relations",                  "Expenditure", False),
            ("900-310006", "Board Travel / Subsistence",        "Expenditure", False),
            ("900-311001", "Marketing Expenses",                "Expenditure", False),
            ("900-312000", "Depreciation — Computer & Peripherals", "Expenditure", False),
            ("900-312001", "Depreciation — Software",           "Expenditure", False),
            ("900-313001", "Airtime",                           "Expenditure", False),
            ("900-313002", "Consultancy",                       "Expenditure", False),
            ("900-313003", "Printing & Stationery",             "Expenditure", False),
            ("900-313004", "Office Expenses",                   "Expenditure", False),
            ("900-313005", "Bookkeeping Fees",                  "Expenditure", False),
            ("900-313006", "Refreshments",                      "Expenditure", False),

            # ====================== FIXED ASSETS ======================
            ("900-501000", "Furniture & Fittings",              "Fixed Asset", False),
            ("900-501001", "Computer & Accessories",            "Fixed Asset", False),
            ("900-501002", "Office Container",                  "Fixed Asset", False),
            ("900-501003", "Office Equipment",                  "Fixed Asset", False),
            ("900-501004", "Office Phone",                      "Fixed Asset", False),
            ("900-501005", "TV Set",                            "Fixed Asset", False),
            ("900-501006", "Loose Tools",                       "Fixed Asset", False),
            ("900-502000", "Prepaid Rent Deposit",              "Fixed Asset", False),
            ("900-503000", "Software",                          "Fixed Asset", False),

            # ====================== CURRENT ASSETS ======================
            # Cash & Bank
            ("900-600000", "Cash Main Account",                 "Current Asset", True),
            ("900-601000", "Equity Bank Current A/c",           "Current Asset", True),
            ("900-601001", "Co-op Bank Current A/c",            "Current Asset", True),
            ("900-601002", "M-Pesa Paybill (C2B)",              "Current Asset", True),
            ("900-601003", "M-Pesa Bulk Payment (B2C)",         "Current Asset", True),
            ("900-623000", "CIC Money Market Fund",             "Current Asset", False),
            # Control & Receivable
            ("900-610000", "Interest Receivable",               "Current Asset", False),
            ("900-610001", "Interest Control",                  "Current Asset", False),
            ("900-610002", "Debtors",                           "Current Asset", False),
            ("900-610003", "SMS Gateway",                       "Current Asset", False),
            # Loan Receivables
            ("900-630010", "Normal Loan Receivable",            "Current Asset", False),
            ("900-630011", "Emergency Loan Receivable",         "Current Asset", False),
            ("900-630012", "Development Loan Receivable",       "Current Asset", False),
            ("900-630013", "Instant Loan / Dividend Advance Receivable", "Current Asset", False),
            ("900-630014", "Mobile Loan Receivable",            "Current Asset", False),
            ("900-630015", "Mobile Loan Plus Receivable",       "Current Asset", False),
            ("900-631000", "Allowance For Loan Loss",           "Current Asset", False),
            # Other Current Assets
            ("900-650000", "Integration Account",               "Current Asset", False),
            ("900-651000", "General Control",                   "Current Asset", False),
            ("900-660000", "Through / Contra Control A/c",      "Current Asset", False),

            # ====================== CURRENT LIABILITIES ======================
            ("900-700000", "Tax Payable",                       "Current Liability", False),
            ("900-700001", "Withholding Tax",                   "Current Liability", False),
            ("900-701000", "Interest on Deposits",              "Current Liability", False),
            ("900-701001", "Unpaid Dividends",                  "Current Liability", False),
            ("900-704000", "Insurance Payable",                 "Current Liability", False),
            ("900-704001", "Accrued Salary",                    "Current Liability", False),
            ("900-704002", "Salaries & Wages Payable",          "Current Liability", False),
            ("900-704003", "Mobile Wallet Liability",           "Current Liability", False),
            ("900-704004", "Unclaimed Deposits",                "Current Liability", False),
            ("900-704005", "Collection / Disbursal Holding A/c","Current Liability", False),
            ("900-704006", "Supervision Fee",                   "Current Liability", False),
            ("900-704007", "Honoraria",                         "Current Liability", False),
            ("900-704008", "Rent Payable",                      "Current Liability", False),
            ("900-704009", "Accountancy",                       "Current Liability", False),
            ("900-704010", "Insurance (Other)",                 "Current Liability", False),
            ("900-704011", "Rent (Other)",                      "Current Liability", False),
            ("900-704012", "Risk Fee (Sinking Fund)",           "Current Liability", False),
            ("900-704013", "Recoverable Expenses",              "Current Liability", False),
            ("900-704014", "Audit Disbursement",                "Current Liability", False),
            ("900-704700", "Gateway",                           "Current Liability", False),
            ("900-710000", "Mobile Wallet",                     "Current Liability", False),

            # ====================== LONG TERM LIABILITIES ======================
            ("900-802000", "Member Deposits (FOSA)",            "Long Term Liability", False),
            ("900-803000", "Fixed Deposits",                    "Long Term Liability", False),
            ("900-800004", "Benevolent Fund",                   "Long Term Liability", False),
            ("900-804000", "Benevolent Fund (GL)",              "Long Term Liability", False),
            ("900-805000", "Elimu Fund",                        "Long Term Liability", False),

            # ====================== EQUITY ======================
            ("900-900000", "Share Capital",                     "Equity", False),
            ("900-920000", "Retained Earnings",                 "Equity", False),
            ("900-940001", "Statutory Reserve",                 "Equity", False),
            ("900-940002", "General Reserve",                   "Equity", False),
            ("900-998000", "Surplus / (Loss) Last Year",        "Equity", False),
        ]

        if options.get('reset'):
            count = SaccoAccount.objects.count()
            SaccoAccountBalance.objects.all().delete()
            SaccoAccount.objects.all().delete()
            self.stdout.write(self.style.WARNING(
                f"Deleted {count} existing accounts. Re-seeding…"
            ))

        created_count = 0
        updated_count = 0
        existing_count = 0

        for code, name, group, is_cash in accounts_data:
            obj, created = SaccoAccount.objects.update_or_create(
                account_code=code,
                defaults={
                    "account_name": name,
                    "account_group": group,
                    "is_cash_account": is_cash,
                },
            )
            if created:
                # Ensure a zero-balance row exists for post_journal
                SaccoAccountBalance.objects.get_or_create(
                    sacco_account=obj,
                    defaults={"balance": 0},
                )
                created_count += 1
                self.stdout.write(f"  Created → {code} | {name}")
            elif obj.account_name != name or obj.account_group != group:
                updated_count += 1
                self.stdout.write(self.style.WARNING(f"  Updated → {code} | {name}"))
            else:
                existing_count += 1

        # Backfill: ensure every SaccoAccount has a SaccoAccountBalance row
        orphan_accounts = SaccoAccount.objects.exclude(
            pk__in=SaccoAccountBalance.objects.values_list('sacco_account_id', flat=True)
        )
        backfilled = 0
        for acct in orphan_accounts:
            SaccoAccountBalance.objects.create(sacco_account=acct, balance=0)
            backfilled += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nChart of Accounts import finished!\n"
            f"  Created : {created_count}\n"
            f"  Updated : {updated_count}\n"
            f"  Already existed (unchanged): {existing_count}\n"
            f"  Balance rows backfilled    : {backfilled}\n"
            f"  Total GL accounts          : {SaccoAccount.objects.count()}"
        ))
