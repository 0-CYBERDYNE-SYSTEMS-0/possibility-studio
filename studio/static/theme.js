(() => {
  try {
    const saved = localStorage.getItem("studio-theme");
    if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
  } catch { /* the system theme applies */ }
})();
