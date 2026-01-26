const html = document.documentElement;
const THEMES = [
    'dark-teal',
    'dark-orange',
    'dark-purple',
    'light-teal',
    'light-orange',
    'light-purple'
];

function normalizeTheme(value) {
    if (!value) return 'light-teal';
    if (value === 'light') return 'light-teal';
    if (value === 'dark') return 'dark-teal';
    return THEMES.includes(value) ? value : 'light-teal';
}

function themeMode(theme) {
    return theme.startsWith('light') ? 'light' : 'dark';
}

function updateIcons(mode) {
    document.querySelectorAll('.js-theme-toggle').forEach(toggle => {
        const icon = toggle.querySelector('i');
        if (!icon) return;
        icon.className = mode === 'light' ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
    });
}

function labelForTheme(theme) {
    const map = {
        'dark-teal': 'Dark/Teal',
        'dark-orange': 'Dark/Orange',
        'dark-purple': 'Dark/Purple',
        'light-teal': 'Light/Teal',
        'light-orange': 'Light/Orange',
        'light-purple': 'Light/Purple'
    };
    return map[theme] || theme;
}

function applyTheme(theme) {
    if (!html) return;
    const normalized = normalizeTheme(theme);
    const mode = themeMode(normalized);
    html.setAttribute('data-itracker-theme', normalized);
    html.setAttribute('data-bs-theme', mode);
    if (document.body) {
        document.body.setAttribute('data-bs-theme', mode);
        document.body.setAttribute('data-itracker-theme', normalized);
    }
    try {
        localStorage.setItem('theme', normalized);
    } catch (err) {
        // ignore storage failures
    }
    updateIcons(mode);
    const label = document.getElementById('currentThemeLabel');
    if (label) {
        label.textContent = labelForTheme(normalized);
    }
}

function toggleTheme() {
    const currentAttr = html.getAttribute('data-itracker-theme');
    const current = normalizeTheme(currentAttr || localStorage.getItem('theme'));
    const index = THEMES.indexOf(current);
    const next = THEMES[(index + 1) % THEMES.length];
    applyTheme(next);
}

window.iTrackerToggleTheme = toggleTheme;

document.addEventListener('DOMContentLoaded', () => {
    const saved = normalizeTheme(localStorage.getItem('theme'));
    applyTheme(saved);
    document.querySelectorAll('.js-theme-toggle').forEach(toggle => {
        toggle.addEventListener('click', toggleTheme);
    });

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
            if (!window.bootstrap || !bootstrap.Alert) return;
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
        if (!window.bootstrap || !bootstrap.Tooltip) return;
        new bootstrap.Tooltip(el);
    });
});
