(function(){
    const searchInput = document.getElementById('search_input');
    const resultsDiv = document.getElementById('search_results');
    const custNoInput = document.getElementById('cust_no');
    const accountSelect = document.getElementById('account_code');
    const useFrom = document.getElementById('use_from');
    const useTo = document.getElementById('use_to');
    const fromDate = document.getElementById('from_date');
    const toDate = document.getElementById('to_date');
    const previewBtn = document.getElementById('btn_preview');
    const downloadBtn = document.getElementById('btn_download');
    const stmDisplay = document.getElementById('stm_display');

    let searchTimer = null;

    // Live search (debounced)
    searchInput.addEventListener('input', function(){
        const q = this.value.trim();
        custNoInput.value = "";
        if(searchTimer) clearTimeout(searchTimer);
        if(!q){
            resultsDiv.style.display = 'none';
            resultsDiv.innerHTML = "";
            return;
        }
        searchTimer = setTimeout(()=> {
            fetch(`/statements/customer-search/?q=${encodeURIComponent(q)}`)
                .then(r => r.json())
                .then(data => {
                    resultsDiv.innerHTML = "";
                    if(!data.results || data.results.length === 0){
                        resultsDiv.style.display = 'none';
                        return;
                    }
                    data.results.forEach(c => {
                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'list-group-item list-group-item-action';
                        btn.textContent = `${c.full_name} (${c.cust_no}) — ${c.mobile || ''}`;
                        btn.addEventListener('click', () => {
                            searchInput.value = `${c.full_name} (${c.cust_no})`;
                            custNoInput.value = c.cust_no;
                            resultsDiv.style.display = 'none';
                            resultsDiv.innerHTML = "";
                        });
                        resultsDiv.appendChild(btn);
                    });
                    resultsDiv.style.display = 'block';
                })
                .catch(err => {
                    console.error(err);
                    resultsDiv.style.display = 'none';
                });
        }, 180);
    });

    // Hide results if click outside
    document.addEventListener('click', function(e){
        if(!resultsDiv.contains(e.target) && e.target !== searchInput){
            resultsDiv.style.display = 'none';
        }
    });

    // Build query params helper
    function buildParams(){
        const params = new URLSearchParams();
        const cust_raw = custNoInput.value || searchInput.value || "";
        const account_code = accountSelect.value;
        
        if (!account_code || !cust_raw) {
            alert("Please select a valid customer and account.");
            return null;
        }

        params.set('cust_no', cust_raw);
        params.set('account_code', account_code);
        
        if(useFrom.checked && fromDate.value) {
            params.set('from_date', fromDate.value);
        }
        if(useTo.checked && toDate.value) {
            params.set('to_date', toDate.value);
        }
        return params.toString();
    }

    // Render function for table display
    function renderTable(data){
        if(!data.transactions || data.transactions.length === 0){
            return `<div class="text-center text-muted p-5"><p>No transactions found for the selected period.</p></div>`;
        }

        const header = `
            <h5 class="text-center mb-0">${data.customer_name}</h5>
            <p class="text-center text-muted small">${data.account_name}</p>
            <table class="table table-sm table-bordered table-striped" style="font-size:12px;">
                <thead>
                    <tr class="table-dark text-center">
                        <th>Date</th>
                        <th>Reference</th>
                        <th>Description</th>
                        <th class="text-end">Debit</th>
                        <th class="text-end">Credit</th>
                        <th class="text-end">Balance</th>
                    </tr>
                </thead>
                <tbody>
        `;
        let rows = '';
        data.transactions.forEach(tr => {
            rows += `
                <tr>
                    <td>${tr.tr_date}</td>
                    <td>${tr.tr_ref}</td>
                    <td>${tr.tr_desc}</td>
                    <td class="text-end">${tr.debit_amount.toLocaleString()}</td>
                    <td class="text-end">${tr.credit_amount.toLocaleString()}</td>
                    <td class="text-end fw-bold">${tr.balance.toLocaleString()}</td>
                </tr>
            `;
        });
        const footer = `
                </tbody>
            </table>
        `;
        return header + rows + footer;
    }

    previewBtn.addEventListener('click', function(){
        const qs = buildParams();
        if(!qs) {
            stmDisplay.innerHTML = `<div class="text-center text-danger p-5"><i class="fas fa-exclamation-circle fa-2x mb-2"></i><p>Please select a customer and account.</p></div>`;
            return;
        }

        stmDisplay.innerHTML = `<div class="text-center p-5"><div class="spinner-border text-primary" role="status"><span class="visually-hidden">Loading...</span></div></div>`;

        fetch(`/statements/preview/?${qs}`)
            .then(r => r.json())
            .then(data => {
                if(data.error){
                    stmDisplay.innerHTML = `<div class="text-center text-danger p-5"><i class="fas fa-exclamation-circle fa-2x mb-2"></i><p>${data.error}</p></div>`;
                    return;
                }
                stmDisplay.innerHTML = renderTable(data);
                resultsDiv.style.display = 'none';
            })
            .catch(err => {
                console.error(err);
                stmDisplay.innerHTML = `<div class="text-center text-danger p-5"><i class="fas fa-exclamation-circle fa-2x mb-2"></i><p>Error fetching preview.</p></div>`;
            });
    });

    downloadBtn.addEventListener('click', function(){
        const qs = buildParams();
        if(!qs) return;
        const url = `/statements/download/?${qs}`;
        window.open(url, '_blank');
    });

    searchInput.addEventListener('keydown', function(e){
        if(e.key === 'Enter'){
            e.preventDefault();
            const v = this.value.trim();
            if(/^\d+$/.test(v)){
                custNoInput.value = v;
            }
            previewBtn.click();
        }
    });
})();