(() => {
  try {
    const saved = localStorage.getItem("dusheng-payment-theme");
    document.documentElement.dataset.theme = saved === "dark" || saved === "light"
      ? saved : window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch (_) {
    document.documentElement.dataset.theme = "light";
  }
})();
