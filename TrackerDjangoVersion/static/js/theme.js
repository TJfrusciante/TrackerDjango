document.addEventListener('DOMContentLoaded', () => {
    const themeToggles = document.querySelectorAll('.js-theme-toggle');
    const html = document.documentElement;
    const saved = localStorage.getItem('theme') || 'light';

    if (html) {
        html.setAttribute('data-bs-theme', saved);
        updateIcons(saved);
    }

    themeToggles.forEach(toggle => {
        toggle.addEventListener('click', () => {
            const current = html.getAttribute('data-bs-theme');
            const next = current === 'light' ? 'dark' : 'light';
            html.setAttribute('data-bs-theme', next);
            localStorage.setItem('theme', next);
            updateIcons(next);
        });
    });

    function updateIcons(mode) {
        themeToggles.forEach(toggle => {
            const icon = toggle.querySelector('i');
            if (!icon) return;
            icon.className = mode === 'light' ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
        });
    }

    const sidebarToggle = document.getElementById('sidebarToggle');
    const sidebarClose = document.getElementById('sidebarClose');
    const sidebarBackdrop = document.getElementById('sidebarBackdrop');
    const sidebarCollapse = document.getElementById('sidebarCollapse');
    const quickActionsToggle = document.getElementById('quickActionsToggle');
    const quickActionsPanel = document.getElementById('quickActionsPanel');
    const collapsedKey = 'sidebarCollapsed';
    const quickKey = 'sidebarQuickOpen';
    const closeSidebar = () => document.body.classList.remove('sidebar-open');
    const openSidebar = () => document.body.classList.add('sidebar-open');

    function setCollapsed(collapsed) {
        if (window.innerWidth < 992) {
            document.body.classList.remove('sidebar-collapsed');
            return;
        }
        document.body.classList.toggle('sidebar-collapsed', collapsed);
        localStorage.setItem(collapsedKey, collapsed ? '1' : '0');
        updateCollapseIcon(collapsed);
        if (quickActionsPanel && quickActionsToggle) {
            if (collapsed) {
                quickActionsPanel.classList.remove('open');
                quickActionsToggle.setAttribute('aria-expanded', 'false');
            } else {
                const savedQuick = localStorage.getItem(quickKey) === '1';
                quickActionsPanel.classList.toggle('open', savedQuick);
                quickActionsToggle.setAttribute('aria-expanded', savedQuick ? 'true' : 'false');
            }
        }
    }

    function updateCollapseIcon(collapsed) {
        if (!sidebarCollapse) return;
        const icon = sidebarCollapse.querySelector('i');
        if (!icon) return;
        icon.className = collapsed ? 'fa-solid fa-angles-right' : 'fa-solid fa-angles-left';
    }

    if (sidebarCollapse) {
        const collapsedSaved = localStorage.getItem(collapsedKey) === '1';
        setCollapsed(collapsedSaved);
        sidebarCollapse.addEventListener('click', () => {
            const collapsed = !document.body.classList.contains('sidebar-collapsed');
            setCollapsed(collapsed);
        });
    }

    if (quickActionsToggle && quickActionsPanel) {
        const savedQuick = localStorage.getItem(quickKey) === '1';
        quickActionsPanel.classList.toggle('open', savedQuick);
        quickActionsToggle.setAttribute('aria-expanded', savedQuick ? 'true' : 'false');
        quickActionsToggle.addEventListener('click', () => {
            const isOpen = quickActionsPanel.classList.toggle('open');
            quickActionsToggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
            localStorage.setItem(quickKey, isOpen ? '1' : '0');
        });
    }

    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', () => openSidebar());
    }
    if (sidebarClose) {
        sidebarClose.addEventListener('click', () => closeSidebar());
    }
    if (sidebarBackdrop) {
        sidebarBackdrop.addEventListener('click', () => closeSidebar());
    }
    document.querySelectorAll('.sidebar .nav-link').forEach(link => {
        link.addEventListener('click', () => {
            if (window.innerWidth < 992) {
                closeSidebar();
            }
        });
    });

    window.addEventListener('resize', () => {
        if (window.innerWidth < 992) {
            document.body.classList.remove('sidebar-collapsed');
        } else if (sidebarCollapse) {
            const collapsedSaved = localStorage.getItem(collapsedKey) === '1';
            setCollapsed(collapsedSaved);
        }
    });

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
