(function() {
    const searchInput    = document.getElementById('search_input');
    const resultsDiv     = document.getElementById('search_results');
    const custNoInput    = document.getElementById('cust_no');
    const accountSelect  = document.getElementById('account_code');
    const useFrom        = document.getElementById('use_from');
    const useTo          = document.getElementById('use_to');
    const fromDate       = document.getElementById('from_date');
    const toDate         = document.getElementById('to_date');
    const previewBtn     = document.getElementById('btn_preview');
    const downloadBtn    = document.getElementById('btn_download');
    const stmDisplay     = document.getElementById('stm_display');
    const includeZeroChk = document.getElementById('include_zero');

    // ── Customer info bar elements ───────────────────────────────────
    const custInfoBar    = document.getElementById('cust_info_bar');
    const custInfoName   = document.getElementById('cust_info_name');
    const custInfoNo     = document.getElementById('cust_info_no');
    const custInfoId     = document.getElementById('cust_info_id');
    const custInfoPhone  = document.getElementById('cust_info_phone');

    let searchTimer = null;

    // ─────────────────────────────────────────────────────────────────
    //  HELPERS
    // ─────────────────────────────────────────────────────────────────

    /** Show the selected customer in the info bar */
    function showCustomerInfo(c) {
        if (!custInfoBar) return;
        if (custInfoName)  custInfoName.textContent  = c.full_name || '';
        if (custInfoNo)    custInfoNo.textContent     = c.cust_no || '';
        if (custInfoId)    custInfoId.textContent     = c.national_id || '-';
        if (custInfoPhone) custInfoPhone.textContent  = c.mobile || '-';
        custInfoBar.style.display = '';
    }

    function hideCustomerInfo() {
        if (custInfoBar) custInfoBar.style.display = 'none';
    }

    /** Select a customer: populate hidden field, label, fetch accounts */
    function selectCustomer(c) {
        searchInput.value = c.full_name + '  (' + c.cust_no + ')';
        custNoInput.value = c.cust_no;
        resultsDiv.style.display = 'none';
        resultsDiv.innerHTML = '';
        showCustomerInfo(c);
        loadCustomerAccounts(c.cust_no);
    }

    // ─────────────────────────────────────────────────────────────────
    //  ACCOUNT FETCHING  (respects include_zero toggle)
    // ─────────────────────────────────────────────────────────────────

    function loadCustomerAccounts(custNo) {
        if (!custNo) return;

        accountSelect.innerHTML = '<option value="">Loading accounts…</option>';
        accountSelect.disabled = true;

        const includeZero = includeZeroChk && includeZeroChk.checked ? '1' : '0';

        fetch(`/statements/api/get-customer-accounts/?cust_no=${encodeURIComponent(custNo)}&include_zero=${includeZero}`)
            .then(r => r.json())
            .then(data => {
                accountSelect.innerHTML = '<option value="">Select Account</option>';
                if (data.results && data.results.length > 0) {
                    data.results.forEach(acc => {
                        const opt = document.createElement('option');
                        opt.value = acc.id;
                        opt.textContent = acc.text;
                        accountSelect.appendChild(opt);
                    });
                    // Auto-select first account when only one exists
                    if (data.results.length === 1) {
                        accountSelect.value = data.results[0].id;
                    }
                } else {
                    accountSelect.innerHTML = '<option value="">No active accounts found</option>';
                }
                accountSelect.disabled = false;
            })
            .catch(err => {
                console.error('Account fetch error:', err);
                accountSelect.innerHTML = '<option value="">Error loading accounts</option>';
                accountSelect.disabled = false;
            });
    }

    // ─────────────────────────────────────────────────────────────────
    //  LIVE SEARCH  (debounced, auto-selects on exact cust_no match)
    // ─────────────────────────────────────────────────────────────────

    // Clicking/focusing the search box clears it for a fresh member lookup,
    // so the clerk doesn't have to backspace the previous selection.
    searchInput.addEventListener('focus', function() {
        if (this.value) {
            this.value = '';
            custNoInput.value = '';
            accountSelect.innerHTML = '<option value="">Select account</option>';
            hideCustomerInfo();
            resultsDiv.style.display = 'none';
            resultsDiv.innerHTML = '';
        }
    });

    searchInput.addEventListener('input', function() {
        const q = this.value.trim();
        custNoInput.value = '';
        hideCustomerInfo();

        if (searchTimer) clearTimeout(searchTimer);

        if (!q) {
            resultsDiv.style.display = 'none';
            resultsDiv.innerHTML = '';
            return;
        }

        searchTimer = setTimeout(() => {
            fetch(`/statements/customer-search/?q=${encodeURIComponent(q)}`)
                .then(r => r.json())
                .then(data => {
                    resultsDiv.innerHTML = '';

                    if (!data.results || data.results.length === 0) {
                        resultsDiv.innerHTML = '<div class="list-group-item text-muted small py-2 text-center">No members found</div>';
                        resultsDiv.style.display = 'block';
                        return;
                    }

                    // ★ AUTO-SELECT: only when the user has typed a FULL
                    //   member number (5+ digits) that exactly matches a
                    //   result. Partial numbers (e.g. "1", "114") always
                    //   show the dropdown so the clerk picks deliberately.
                    if (/^\d{5,}$/.test(q)) {
                        const paddedQ = q.padStart(5, '0');
                        const exactMatch = data.results.find(c => c.cust_no === paddedQ);
                        if (exactMatch) {
                            selectCustomer(exactMatch);
                            return;   // dropdown never shows
                        }
                    }

                    // Build dropdown for manual selection
                    data.results.forEach(c => {
                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'list-group-item list-group-item-action d-flex align-items-center gap-2 py-2';

                        btn.innerHTML =
                            '<span class="badge bg-light text-dark border font-monospace" style="min-width:52px;">' + c.cust_no + '</span>' +
                            '<span class="fw-semibold text-truncate">' + (c.full_name || '') + '</span>' +
                            '<span class="text-muted small ms-auto text-nowrap">' + (c.national_id || '') + '</span>';

                        btn.addEventListener('click', () => selectCustomer(c));
                        resultsDiv.appendChild(btn);
                    });
                    resultsDiv.style.display = 'block';
                })
                .catch(err => {
                    console.error(err);
                    resultsDiv.style.display = 'none';
                });
        }, 200);
    });

    // Hide dropdown on outside click
    document.addEventListener('click', function(e) {
        if (!resultsDiv.contains(e.target) && e.target !== searchInput) {
            resultsDiv.style.display = 'none';
        }
    });

    // ─────────────────────────────────────────────────────────────────
    //  ZERO-BALANCE TOGGLE  → re-fetch accounts for current customer
    // ─────────────────────────────────────────────────────────────────

    if (includeZeroChk) {
        includeZeroChk.addEventListener('change', function() {
            const cust = custNoInput.value;
            if (cust) loadCustomerAccounts(cust);
        });
    }

    // ─────────────────────────────────────────────────────────────────
    //  BUILD QUERY PARAMS
    // ─────────────────────────────────────────────────────────────────

    function buildParams() {
        const params = new URLSearchParams();
        const cust_raw = custNoInput.value || searchInput.value || '';
        const account_code = accountSelect.value;

        if (!cust_raw) {
            alert('Please search and select a customer first.');
            searchInput.focus();
            return null;
        }
        if (!account_code) {
            alert('Please select a target account.');
            accountSelect.focus();
            return null;
        }

        params.set('cust_no', cust_raw);
        params.set('account_code', account_code);

        if (useFrom.checked && fromDate.value) params.set('from_date', fromDate.value);
        if (useTo.checked && toDate.value)     params.set('to_date', toDate.value);

        return params.toString();
    }

    // ─────────────────────────────────────────────────────────────────
    //  RENDER TABLE  (D365 grid styling)
    // ─────────────────────────────────────────────────────────────────

    function fmt(n) {
        return n > 0 ? n.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '-';
    }
    function fmtBal(n) {
        return n.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }

    function renderTable(data) {
        if (!data.transactions || data.transactions.length === 0) {
            return '<div class="d-flex flex-column align-items-center justify-content-center text-muted py-5">' +
                   '<i class="bi bi-journal-x fs-1 mb-2 opacity-50"></i>' +
                   '<p class="mb-0">No transactions found for the selected period.</p></div>';
        }

        // TB verification badge from API response
        let tbBadge = '';
        if (data.tb_verified) {
            tbBadge = ' <span style="display:inline-flex;align-items:center;gap:3px;font-size:.6rem;font-weight:600;padding:2px 7px;border-radius:999px;background:#d1fae5;color:#065f46;border:1px solid #6ee7b7;text-transform:uppercase;letter-spacing:.03em;vertical-align:middle;margin-left:6px;" title="Backed by TigerBeetle — immutable ledger"><i class="bi bi-shield-check"></i> TB</span>';
        }

        let html =
            '<div class="mb-3 text-center">' +
                '<h6 class="mb-0 fw-bold" style="color: var(--d365-text);">' + data.customer_name + tbBadge + '</h6>' +
                '<span class="small" style="color: var(--d365-primary);">' + data.account_name + '</span>' +
            '</div>' +
            '<div class="grid-scroll">' +
            '<table class="grid-table w-100">' +
            '<thead><tr>' +
                '<th style="width:12%">Date</th>' +
                '<th style="width:15%">Reference</th>' +
                '<th>Description</th>' +
                '<th class="num" style="width:13%">Debit</th>' +
                '<th class="num" style="width:13%">Credit</th>' +
                '<th class="num" style="width:13%">Balance</th>' +
            '</tr></thead><tbody>';

        data.transactions.forEach(tr => {
            const isBF = tr.tr_ref === 'B/F';
            const rowCls = isBF ? 'style="background:var(--d365-surface-alt);font-style:italic;"' : '';
            const balCls = tr.balance < 0 ? 'neg' : (tr.balance > 0 ? 'pos' : '');
            html +=
                '<tr ' + rowCls + '>' +
                    '<td>' + tr.tr_date + '</td>' +
                    '<td class="font-monospace">' + tr.tr_ref + '</td>' +
                    '<td>' + tr.tr_desc + '</td>' +
                    '<td class="num">' + fmt(tr.debit_amount) + '</td>' +
                    '<td class="num">' + fmt(tr.credit_amount) + '</td>' +
                    '<td class="num fw-bold ' + balCls + '">' + fmtBal(tr.balance) + '</td>' +
                '</tr>';
        });

        html += '</tbody></table></div>';
        return html;
    }

    // ─────────────────────────────────────────────────────────────────
    //  PREVIEW + DOWNLOAD
    // ─────────────────────────────────────────────────────────────────

    previewBtn.addEventListener('click', function() {
        const qs = buildParams();
        if (!qs) return;

        stmDisplay.innerHTML =
            '<div class="d-flex flex-column align-items-center justify-content-center py-5">' +
                '<div class="spinner-border" style="color:var(--d365-primary);" role="status">' +
                    '<span class="visually-hidden">Loading…</span>' +
                '</div>' +
                '<p class="mt-3 small" style="color:var(--d365-text-secondary);">Generating statement…</p>' +
            '</div>';

        fetch(`/statements/preview/?${qs}`)
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    stmDisplay.innerHTML =
                        '<div class="d-flex flex-column align-items-center justify-content-center py-5">' +
                            '<i class="bi bi-exclamation-triangle-fill fs-2 mb-2" style="color:var(--d365-danger);"></i>' +
                            '<p class="text-center" style="color:var(--d365-danger);">' + data.error + '</p>' +
                        '</div>';
                    return;
                }
                stmDisplay.innerHTML = renderTable(data);
                resultsDiv.style.display = 'none';
            })
            .catch(err => {
                console.error(err);
                stmDisplay.innerHTML =
                    '<div class="d-flex flex-column align-items-center justify-content-center py-5">' +
                        '<i class="bi bi-exclamation-triangle-fill fs-2 mb-2" style="color:var(--d365-danger);"></i>' +
                        '<p class="text-center" style="color:var(--d365-danger);">Error fetching preview. Check console.</p>' +
                    '</div>';
            });
    });

    downloadBtn.addEventListener('click', function() {
        const qs = buildParams();
        if (!qs) return;
        window.open(`/statements/download/?${qs}`, '_blank');
    });

    // ─────────────────────────────────────────────────────────────────
    //  ENTER KEY  →  auto-select numeric input, preview
    // ─────────────────────────────────────────────────────────────────

    searchInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            // Only act if a customer has actually been selected (via the
            // dropdown or a full-cust_no auto-match). Never resolve a bare,
            // unconfirmed number here.
            if (custNoInput.value && accountSelect.value) {
                previewBtn.click();
            }
        }
    });

    // Keyboard navigation: pressing Enter on account select triggers preview
    accountSelect.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            previewBtn.click();
        }
    });
})();