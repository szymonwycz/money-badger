(function () {
  fetch('/api/transactions/unreviewed/count')
    .then(r => r.json())
    .then(d => {
      if (!d.count) return;
      document.querySelectorAll('a.nav-link[href="/check-me"]').forEach(a => {
        if (!a.classList.contains('active')) a.classList.add('alert');
      });
    })
    .catch(() => {});
})();

// PWA: register the service worker on every page so the app is installable
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
