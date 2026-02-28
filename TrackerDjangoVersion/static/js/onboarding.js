(() => {
    const steps = [
        {
            selector: '[data-tour="workspace-switch"]',
            title: 'Workspace ativo',
            text: 'Troque de workspace ou entre no modo global quando precisar.',
        },
        {
            selector: '[data-tour="theme-toggle"]',
            title: 'Modo claro/escuro',
            text: 'Escolha o tema que prefere para trabalhar.',
        },
        {
            selector: '[data-tour="nav-dashboard"]',
            title: 'Dashboard',
            text: 'Veja indicadores, filtros e gráficos do período.',
        },
        {
            selector: '[data-tour="nav-launch"]',
            title: 'Lançamentos',
            text: 'Convidados podem lançar transações sem acessar o dashboard.',
        },
        {
            selector: '[data-tour="nav-transactions"]',
            title: 'Transações',
            text: 'Gerencie entradas e saídas com filtros e importações.',
        },
        {
            selector: '[data-tour="nav-tasks"]',
            title: 'Tarefas',
            text: 'Organize etapas e acompanhe prazos do time.',
        },
        {
            selector: '[data-tour="nav-ai"]',
            title: 'Agente de IA',
            text: 'Pergunte sobre categorias, saldo e tarefas.',
        },
        {
            selector: '[data-tour="nav-whatsapp"]',
            title: 'ChatAgent no WhatsApp',
            text: 'Lance transações e tarefas direto do WhatsApp.',
        },
        {
            selector: '[data-tour="nav-more"]',
            title: 'Demais funcionalidades',
            text: 'Acesse categorias, notificações, workspaces e ajuda.',
        },
        {
            selector: '[data-tour="nav-notifications"]',
            title: 'Notificações',
            text: 'Veja alertas, resumos e mensagens do sistema.',
        },
        {
            selector: '[data-tour="nav-categories"]',
            title: 'Categorias',
            text: 'Organize transações e tarefas com cores.',
        },
        {
            selector: '[data-tour="nav-workspaces"]',
            title: 'Workspaces',
            text: 'Gerencie membros, convites e acessos.',
        },
        {
            selector: '[data-tour="nav-help"]',
            title: 'Ajuda',
            text: 'Confira guias rápidos e exemplos de uso.',
        },
        {
            selector: '[data-tour="dashboard-filters"]',
            title: 'Filtros de período',
            text: 'Defina intervalo e tipo para refinar os gráficos.',
        },
        {
            selector: '[data-tour="chart-category"]',
            title: 'Gráficos interativos',
            text: 'Clique nas categorias para filtrar o dashboard.',
        },
        {
            selector: '[data-tour="agent-fab"]',
            title: 'Atalho do agente',
            text: 'Abra o assistente flutuante de qualquer tela.',
            placement: 'floating-right',
        },
    ];

    let current = 0;
    let activeSteps = [];
    let tooltip;
    let highlighted;
    let resizeHandler;
    let scrollHandler;
    let keyHandler;

    function removeTourListeners() {
        if (resizeHandler) {
            window.removeEventListener('resize', resizeHandler);
            resizeHandler = null;
        }
        if (scrollHandler) {
            window.removeEventListener('scroll', scrollHandler);
            scrollHandler = null;
        }
        if (keyHandler) {
            document.removeEventListener('keydown', keyHandler);
            keyHandler = null;
        }
    }

    function ensureTooltip() {
        if (tooltip) return tooltip;
        tooltip = document.createElement('div');
        tooltip.className = 'tour-tooltip';
        tooltip.innerHTML = `
            <div class="tour-title"></div>
            <div class="tour-text"></div>
            <div class="tour-actions">
                <span class="tour-progress"></span>
                <button type="button" class="btn btn-sm btn-outline-light tour-prev">Voltar</button>
                <button type="button" class="btn btn-sm btn-primary tour-next">Próximo</button>
                <button type="button" class="btn btn-sm btn-outline-light tour-close">Fechar</button>
            </div>
        `;
        document.body.appendChild(tooltip);
        return tooltip;
    }

    function positionTooltip(target, step) {
        if (!tooltip || !target) return;
        const rect = target.getBoundingClientRect();
        const padding = 16;
        const isMobile = window.innerWidth <= 768;
        tooltip.classList.toggle('tour-tooltip-mobile', isMobile);
        if (isMobile) {
            tooltip.style.maxWidth = `${window.innerWidth - padding * 2}px`;
            target.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
        const tipRect = tooltip.getBoundingClientRect();
        let top = rect.bottom + 12;
        let left = rect.left;
        if (isMobile) {
            top = rect.bottom + 12;
            if (top + tipRect.height > window.innerHeight - padding) {
                top = rect.top - tipRect.height - 12;
            }
            if (top < padding) {
                top = window.innerHeight - tipRect.height - padding;
            }
            left = Math.max(padding, (window.innerWidth - tipRect.width) / 2);
            tooltip.style.top = `${top}px`;
            tooltip.style.left = `${left}px`;
            return;
        }
        if (step && step.placement === 'floating-right') {
            let left = window.innerWidth - tipRect.width - padding;
            let top = rect.top - tipRect.height - 16;
            if (top + tipRect.height > window.innerHeight - padding) {
                top = window.innerHeight - tipRect.height - padding;
            }
            if (top < padding) top = padding;
            tooltip.style.top = `${top}px`;
            tooltip.style.left = `${left}px`;
            return;
        }

        if (step && step.placement === 'left') {
            let top = rect.top + (rect.height - tipRect.height) / 2;
            let left = rect.left - tipRect.width - 16;
            if (left < padding) {
                left = rect.right + 16;
            }
            if (top + tipRect.height > window.innerHeight - padding) {
                top = window.innerHeight - tipRect.height - padding;
            }
            if (top < padding) top = padding;
            tooltip.style.top = `${top}px`;
            tooltip.style.left = `${left}px`;
            return;
        }

        if (step && step.placement === 'left-bottom') {
            let left = rect.left - tipRect.width - 16;
            if (left < padding) {
                left = rect.right + 16;
            }
            let top = rect.bottom - tipRect.height;
            if (top + tipRect.height > window.innerHeight - padding) {
                top = window.innerHeight - tipRect.height - padding;
            }
            if (top < padding) top = padding;
            tooltip.style.top = `${top}px`;
            tooltip.style.left = `${left}px`;
            return;
        }

        const sidebar = target.closest('.sidebar');
        if (sidebar) {
            const sidebarRect = sidebar.getBoundingClientRect();
            const candidateLeft = sidebarRect.right + 16;
            if (candidateLeft + tipRect.width < window.innerWidth - padding) {
                left = candidateLeft;
                top = rect.top;
            }
        }
        if (top + tipRect.height > window.innerHeight - padding) {
            top = rect.top - tipRect.height - 12;
        }
        if (left + tipRect.width > window.innerWidth - padding) {
            left = window.innerWidth - tipRect.width - padding;
        }
        if (left < padding) left = padding;
        if (top < padding) top = padding;
        tooltip.style.top = `${top}px`;
        tooltip.style.left = `${left}px`;
    }

    function clearHighlight() {
        if (highlighted) {
            highlighted.classList.remove('tour-highlight');
            highlighted.classList.remove('tour-highlight-relative');
            highlighted = null;
        }
    }

    function showStep(index) {
        if (!activeSteps.length) return;
        current = Math.max(0, Math.min(index, activeSteps.length - 1));
        const step = activeSteps[current];
        const target = document.querySelector(step.selector);
        if (!target) {
            showStep(current + 1);
            return;
        }
        clearHighlight();
        highlighted = target;
        highlighted.classList.add('tour-highlight');
        if (window.getComputedStyle(highlighted).position === 'static') {
            highlighted.classList.add('tour-highlight-relative');
        }
        const isFixed = window.getComputedStyle(highlighted).position === 'fixed';
        if (!isFixed) {
            highlighted.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }

        const tip = ensureTooltip();
        tip.querySelector('.tour-title').textContent = step.title;
        tip.querySelector('.tour-text').textContent = step.text;
        tip.querySelector('.tour-progress').textContent = `${current + 1}/${activeSteps.length}`;
        tip.querySelector('.tour-prev').disabled = current === 0;
        tip.querySelector('.tour-next').textContent = current === activeSteps.length - 1 ? 'Finalizar' : 'Próximo';
        tip.classList.add('show');
        requestAnimationFrame(() => positionTooltip(target, step));
    }

    function finishTour(storeKey) {
        clearHighlight();
        if (tooltip) tooltip.classList.remove('show');
        removeTourListeners();
        if (storeKey) localStorage.setItem(storeKey, 'done');
    }

    function startTour(storeKey) {
        activeSteps = steps.filter(step => document.querySelector(step.selector));
        if (!activeSteps.length) return;
        removeTourListeners();
        showStep(0);

        const tip = ensureTooltip();
        tip.querySelector('.tour-prev').onclick = () => showStep(current - 1);
        tip.querySelector('.tour-next').onclick = () => {
            if (current >= activeSteps.length - 1) {
                finishTour(storeKey);
                return;
            }
            showStep(current + 1);
        };
        tip.querySelector('.tour-close').onclick = () => finishTour(storeKey);
        resizeHandler = () => positionTooltip(highlighted, activeSteps[current]);
        scrollHandler = () => positionTooltip(highlighted, activeSteps[current]);
        keyHandler = (event) => {
            if (event.key === 'Escape') finishTour(storeKey);
        };
        window.addEventListener('resize', resizeHandler);
        window.addEventListener('scroll', scrollHandler, { passive: true });
        document.addEventListener('keydown', keyHandler);
    }

    window.iTrackerTour = {
        start: (storeKey) => startTour(storeKey),
    };
})();
