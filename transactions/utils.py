import random
import datetime
from decimal import Decimal

PREFIX_MAP = {
    # Savings & Loans
    "seed_deposit": "SD",
    "share_capital": "SHR",
    "welfare_deposit": "WD",
    "normal_loan": "NLR",
    "emergency_loan": "ELR",
    "mobile_loan": "MLR",
    "repsi_loan": "RLR",
    
    # Batch & System Operations
    "dividend": "DIV",
    "journal_voucher": "JV",
    "batch": "BCH",
    "interest": "INT",
    "loan_interest": "LINT",
    "transfer": "TRF",
    "loan_disbursement": "LD",
    "loan_offset": "OFF",

}
from decimal import InvalidOperation


# ── Customer Number Normalisation ─────────────────────────────────────
def normalize_cust_no(cust_no, min_digits: int = 5) -> str:
    """
    Normalize customer number to a zero-padded string.

    >>> normalize_cust_no(123)
    '00123'
    >>> normalize_cust_no('456')
    '00456'
    """
    if cust_no is None:
        raise ValueError("cust_no cannot be None")
    if isinstance(cust_no, Decimal):
        cust_no_str = str(int(cust_no))
    else:
        cust_no_str = str(cust_no).strip()
    cust_no_str = ''.join(c for c in cust_no_str if c.isdigit())
    if not cust_no_str:
        raise ValueError(f"No digits found in cust_no: {cust_no}")
    return cust_no_str.zfill(min_digits)


def normalize_cust_no_list(cust_nos, min_digits: int = 5) -> list:
    """Normalize a list of customer numbers (deduplicated)."""
    return list(set(normalize_cust_no(cn, min_digits) for cn in cust_nos))


def ensure_cust_no_dict(data_dict, min_digits: int = 5) -> dict:
    """Return *data_dict* with every key run through normalize_cust_no."""
    return {normalize_cust_no(k, min_digits): v for k, v in data_dict.items()}


# ── Safe Decimal Conversion ──────────────────────────────────────────
def safe_decimal(value, default=Decimal('0.00')) -> Decimal:
    """
    Safely converts an incoming value (string, float, int, or None) into a Decimal.
    Cleans up whitespace and thousand-separator commas automatically.
    """
    if value is None:
        return default
        
    if isinstance(value, Decimal):
        return value
        
    # Convert to string and strip spaces
    clean_val = str(value).strip()
    
    # Handle thousands separators often found in financial copy-pastes (e.g., "520,000.00")
    clean_val = clean_val.replace(',', '')
    
    if not clean_val:
        return default
        
    try:
        return Decimal(clean_val)
    except (InvalidOperation, ValueError, TypeError):
        return default
    
def make_tr_ref(kind: str) -> str:
    """
    Standardized Reference Generator
    Format: [PREFIX][YYYYMMDDHHMMSS][5-DIGIT-RANDOM]
    Example: TRF2026031311450254321
    """
    kind_key = kind.lower()
    prefix = PREFIX_MAP.get(kind_key, "TRX")
    
    # Using %Y (2026) instead of %y (26) is industry standard for clarity
    timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    random_suffix = random.randint(100, 999)
    
    return f"{prefix}{timestamp}{random_suffix}"


import datetime
import secrets

def make_tr_ref2(kind: str) -> str:
    """
    Standardized Reference Generator
    Format: [PREFIX][YYYYMMDDHHMMSS][6-CHAR-HEX-SUFFIX]
    Example: TRF20260313114502A3F8B2
    """
    kind_key = kind.lower()
    prefix = PREFIX_MAP.get(kind_key, "TRX")
    
    timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    
    # Generate a cryptographically secure 6-character hex suffix (3 bytes)
    # 3 bytes of entropy = 16^6 = 16,777,216 possible unique combinations per second
    hex_suffix = secrets.token_hex(3).upper() 
    
    return f"{prefix}{timestamp}{hex_suffix}"

import secrets

def make_tr_ref3(kind: str) -> str:
    """
    Standardized Reference Generator (Timestamp-free)
    Format: [PREFIX][16-CHARACTER-HEX]
    Example: TRF5F8B2C4E9A1D3B7D
    """
    kind_key = kind.lower()
    prefix = PREFIX_MAP.get(kind_key, "TRX")
    
    # 8 bytes of entropy = 16 hex characters
    # Yields 18,446,744,073,709,551,616 unique combinations
    hex_suffix = secrets.token_hex(8).upper() 
    
    return f"{prefix}{hex_suffix}"