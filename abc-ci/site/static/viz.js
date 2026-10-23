// Visualization selector: switches between the table and plot views.
document.addEventListener("DOMContentLoaded", () => {
    const selector = document.getElementById("viz-selector");
    if (!selector) return;

    const views = {
        table: document.getElementById("view-table"),
        plot: document.getElementById("view-plot"),
    };

    const showView = (name) => {
        Object.entries(views).forEach(([key, el]) => {
            if (!el) return;
            el.classList.toggle("hidden", key !== name);
        });
        if (name === "plot" && window.renderPlot) {
            window.renderPlot();
        }
    };

    selector.querySelectorAll('input[name="viz"]').forEach((radio) => {
        radio.addEventListener("change", (e) => {
            if (e.target.checked) showView(e.target.value);
        });
    });

    const checked = selector.querySelector('input[name="viz"]:checked');
    showView(checked ? checked.value : "table");
});
