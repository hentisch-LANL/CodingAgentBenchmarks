document.addEventListener("DOMContentLoaded", () => {
    const table = document.getElementById("leaderboard-table");
    const filterBar = document.getElementById("filter-bar");
    if (!table || !filterBar) return;

    const searchInput = document.getElementById("filter-search");
    const harnessSelect = document.getElementById("filter-harness");
    const modelSelect = document.getElementById("filter-model");
    const resetButton = document.getElementById("filter-reset");
    const statusEl = document.getElementById("filter-status");

    const rows = Array.from(table.querySelectorAll("tbody tr.agent-row"));
    if (rows.length === 0) return;

    const applyFilters = () => {
        const search = (searchInput?.value || "").trim().toLowerCase();
        const harness = harnessSelect?.value || "";
        const model = modelSelect?.value || "";

        let visible = 0;

        rows.forEach((row) => {
            const rowHarness = row.dataset.harness || "";
            const rowModels = row.dataset.models || "";

            let show = true;

            if (search) {
                const haystack = `${rowHarness} ${rowModels}`.toLowerCase();
                if (!haystack.includes(search)) show = false;
            }
            if (show && harness && rowHarness !== harness) show = false;
            if (show && model) {
                const modelList = rowModels.split(",").map((m) => m.trim());
                if (!modelList.includes(model)) show = false;
            }

            row.classList.toggle("filtered-out", !show);
            if (show) visible += 1;
        });

        if (statusEl) {
            statusEl.textContent =
                visible === rows.length
                    ? `Showing all ${rows.length} agents`
                    : `Showing ${visible} of ${rows.length} agents`;
        }
    };

    const reset = () => {
        if (searchInput) searchInput.value = "";
        if (harnessSelect) harnessSelect.value = "";
        if (modelSelect) modelSelect.value = "";
        applyFilters();
    };

    searchInput?.addEventListener("input", applyFilters);
    harnessSelect?.addEventListener("change", applyFilters);
    modelSelect?.addEventListener("change", applyFilters);
    resetButton?.addEventListener("click", reset);

    applyFilters();
});
