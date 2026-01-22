(() => {
    const steps = [
        {
            selector: '[data-tour="workspace-switch"]',
            title: 'Workspace ativo',
            text: 'Troque de workspace ou entre no modo global quando precisar.',
        },
        {
            selector: '[data-tour="nav-dashboard"]',
            title: 'Dashboard',
            text: 'Veja indicadores, filtros e graficos do periodo.',
        },
        {
            selector: '[data-tour="nav-launch"]',
            title: 'Lancamentos',
            text: 'Convidados podem lancar transacoes sem acessar o dashboard.',
        },
        {
            selector: '[data-tour="nav-transactions"]',
            title: 'Transacoes',
            text: 'Gerencie entradas e saidas com filtros e importacoes.',
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
            selector: '[data-tour="nav-more"]',
            title: 'Demais funcionalidades',
            text: 'Acesse categorias, notificacoes, workspaces e ajuda.',
        },
        {
            selector: '[data-tour="dashboard-filters"]',
            title: 'Filtros de periodo',
            text: 'Defina intervalo e tipo para refinar os graficos.',
        },
        {
            selector: '[data-tour="chart-category"]',
            title: 'Graficos interativos',
            text: 'Clique nas categorias para filtrar o dashboard.',
        },
        {
            selector: '[data-tour="agent-fab"]',
            title: 'Atalho do agente',
            text: 'Abra o assistente flutuante de qualquer tela.',
        },
    ];

    let current = 0;
    let activeSteps = [];
    let tooltip;
    let highlighted;

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
                <button type="button" class="btn btn-sm btn-primary tour-next">Proximo</button>
                <button type="button" class="btn btn-sm btn-outline-light tour-close">Fechar</button>
            </div>
        `;
        document.body.appendChild(tooltip);
        return tooltip;
    }

    function positionTooltip(target) {
        if (!tooltip || !target) return;
        const rect = target.getBoundingClientRect();
        const tipRect = tooltip.getBoundingClientRect();
        const padding = 16;
        let top = rect.bottom + 12;
        let left = rect.left;
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
        highlighted.scrollIntoView({ behavior: 'smooth', block: 'center' });

        const tip = ensureTooltip();
        tip.querySelector('.tour-title').textContent = step.title;
        tip.querySelector('.tour-text').textContent = step.text;
        tip.querySelector('.tour-progress').textContent = `${current + 1}/${activeSteps.length}`;
        tip.querySelector('.tour-prev').disabled = current === 0;
        tip.querySelector('.tour-next').textContent = current === activeSteps.length - 1 ? 'Finalizar' : 'Proximo';
        tip.classList.add('show');
        requestAnimationFrame(() => positionTooltip(target));
    }

    function finishTour(storeKey) {
        clearHighlight();
        if (tooltip) tooltip.classList.remove('show');
        if (storeKey) localStorage.setItem(storeKey, 'done');
    }

    function startTour(storeKey) {
        activeSteps = steps.filter(step => document.querySelector(step.selector));
        if (!activeSteps.length) return;
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
        window.addEventListener('resize', () => positionTooltip(highlighted));
        window.addEventListener('scroll', () => positionTooltip(highlighted), { passive: true });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') finishTour(storeKey);
        }, { once: true });
    }

    window.iTrackerTour = {
        start: (storeKey) => startTour(storeKey),
    };
})();
