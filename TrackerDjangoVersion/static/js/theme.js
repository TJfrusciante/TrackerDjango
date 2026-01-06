document.addEventListener('DOMContentLoaded', () => {
    const themeToggle = document.getElementById('themeToggle');
    const themeIcon = themeToggle ? themeToggle.querySelector('i') : null;
    const html = document.documentElement;
    const saved = localStorage.getItem('theme') || 'light';

    if (html) {
        html.setAttribute('data-bs-theme', saved);
        updateIcon(saved);
    }

    if (themeToggle) {
        themeToggle.addEventListener('click', () => {
            const current = html.getAttribute('data-bs-theme');
            const next = current === 'light' ? 'dark' : 'light';
            html.setAttribute('data-bs-theme', next);
            localStorage.setItem('theme', next);
            updateIcon(next);
        });
    }

    function updateIcon(mode) {
        if (!themeIcon) return;
        themeIcon.className = mode === 'light' ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
    }

    // auto-close alerts (respeita data-autoclose em ms; default 2000)
    const alerts = document.querySelectorAll('.alert');
    alerts.forEach(a => {
        const delay = parseInt(a.dataset.autoclose || '2000', 10);
        setTimeout(() => {
            const bsAlert = bootstrap.Alert.getOrCreateInstance(a);
            bsAlert.close();
        }, delay);
    });

    const scrollTopBtn = document.getElementById('scrollTopBtn');
    const scrollBottomBtn = document.getElementById('scrollBottomBtn');
    if (scrollTopBtn) {
        scrollTopBtn.addEventListener('click', () => {
            window.scrollTo({ top: 0, behavior: 'smooth' });
        });
    }
    if (scrollBottomBtn) {
        scrollBottomBtn.addEventListener('click', () => {
            window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
        });
    }

    const agentFab = document.getElementById('agentFab');
    const agentPanel = document.getElementById('agentPanel');
    if (agentFab && agentPanel) {
        agentFab.addEventListener('click', () => {
            agentPanel.classList.toggle('open');
            // força recarregar se estiver vazio
            const iframe = agentPanel.querySelector('iframe');
            if (agentPanel.classList.contains('open') && iframe && !iframe.src) {
                iframe.src = iframe.dataset.src || iframe.getAttribute('src');
            }
        });
    }

    // Tooltips globais
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.forEach(el => {
        new bootstrap.Tooltip(el);
    });
});
