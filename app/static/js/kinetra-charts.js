// Chart.js grafikleri için ortak tema yardımcıları. Renkler sabit değil, CSS token'larından
// okunur; böylece açık/koyu mod değişince grafikler de doğru renklerle yeniden çizilir.
window.KChart = (() => {
  const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function alpha(color, a) {
    const h = color.replace('#', '');
    if (h.length !== 6) return color;
    return `rgba(${parseInt(h.slice(0, 2), 16)}, ${parseInt(h.slice(2, 4), 16)}, ${parseInt(h.slice(4, 6), 16)}, ${a})`;
  }

  const tooltip = (extra = {}) => ({
    backgroundColor: token('--tooltip-bg'),
    borderColor: token('--border'),
    borderWidth: 1,
    titleColor: token('--text-primary'),
    bodyColor: token('--text-secondary'),
    padding: 10,
    cornerRadius: 8,
    displayColors: false,
    ...extra,
  });

  const xAxis = (ticks = {}) => ({
    grid: { display: false },
    border: { display: false },
    ticks: { color: token('--text-muted'), maxRotation: 0, autoSkip: true, maxTicksLimit: 6, font: { size: 11 }, ...ticks },
  });

  const yAxis = (ticks = {}, extra = {}) => ({
    grid: { color: token('--chart-grid') },
    border: { display: false },
    ticks: { color: token('--text-muted'), maxTicksLimit: 4, font: { size: 11 }, ...ticks },
    ...extra,
  });

  // render() grafikleri oluşturup dizi olarak döner; tema değişince eskileri silinip yeniden
  // çizilir. Dönen fonksiyon elle yeniden çizmek için (ör. zaman aralığı değişince) kullanılır.
  function themed(render) {
    let charts = [];
    const draw = () => {
      charts.forEach((c) => c.destroy());
      charts = (render() || []).filter(Boolean);
    };
    draw();
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', draw);
    return draw;
  }

  return { token, alpha, tooltip, xAxis, yAxis, themed };
})();
