document.addEventListener("DOMContentLoaded", () => {
    const table = document.getElementById("leaderboard-table");
    if (!table) return;

    const groupHeaders = table.querySelectorAll("th.group-header");
    const taskHeaders = table.querySelectorAll("th.task-header");
    if (groupHeaders.length === 0) return;

    let isExpanded = false;

    const toggleAllGroups = () => {
        isExpanded = !isExpanded;

        groupHeaders.forEach((header) => {
            header.classList.toggle("hidden", isExpanded);
        });

        const groupCells = table.querySelectorAll(".group-cell");
        groupCells.forEach((cell) => {
            cell.classList.toggle("hidden", isExpanded);
        });

        taskHeaders.forEach((header) => {
            header.classList.toggle("hidden", !isExpanded);
        });

        const taskCols = table.querySelectorAll(".task-col");
        taskCols.forEach((col) => {
            col.classList.toggle("hidden", !isExpanded);
        });

        groupHeaders.forEach((header) => {
            const btn = header.querySelector(".group-sort-btn");
            if (btn) {
                btn.setAttribute("aria-expanded", isExpanded ? "true" : "false");
            }
        });

        taskHeaders.forEach((header) => {
            const btn = header.querySelector(".task-sort-btn");
            if (btn) {
                btn.setAttribute("aria-expanded", isExpanded ? "true" : "false");
            }
        });
    };

    groupHeaders.forEach((header) => {
        const btn = header.querySelector(".group-sort-btn");
        if (!btn) return;

        btn.addEventListener("dblclick", (e) => {
            e.preventDefault();
            e.stopPropagation();
            toggleAllGroups();
        });
    });

    taskHeaders.forEach((header) => {
        const btn = header.querySelector(".task-sort-btn");
        if (!btn) return;

        btn.addEventListener("dblclick", (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (isExpanded) {
                toggleAllGroups();
            }
        });
    });
});
