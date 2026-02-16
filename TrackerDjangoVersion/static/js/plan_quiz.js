(() => {
    const quizRoot = document.getElementById('planQuizPage');
    if (!quizRoot) return;

    const stage = document.getElementById('quizStage');
    const stageTrack = document.getElementById('quizStageTrack');
    const stepCurrent = document.getElementById('quizStepCurrent');
    const stepTotal = document.getElementById('quizStepTotal');
    const progressBar = document.getElementById('quizProgressBar');
    const dotsWrap = document.getElementById('quizDots');
    const resultWrap = document.getElementById('quizResult');
    const actionsWrap = document.getElementById('quizActions');
    const prevBtn = document.getElementById('quizPrevBtn');
    const nextBtn = document.getElementById('quizNextBtn');
    const skipBtn = document.getElementById('quizSkipBtn');
    const resetBtn = document.getElementById('quizResetBtn');

    let currentPanel = document.getElementById('quizPanelCurrent');
    let incomingPanel = document.getElementById('quizPanelIncoming');

    const registerUrl = quizRoot.dataset.registerUrl || '/register/';
    const loginUrl = quizRoot.dataset.loginUrl || '/login/';
    const storageKey = quizRoot.dataset.quizKey || 'itracker:plan-quiz:v2';
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const debug = window.location.hostname === 'localhost' || localStorage.getItem('itracker:quiz:debug') === '1';

    const quizQuestions = [
        {
            id: 'team_size',
            title: 'Voce vai usar o iTracker sozinho ou com outras pessoas?',
            hint: 'Define o nivel de colaboracao ideal para o seu dia a dia.',
            options: [
                { value: 'solo', label: 'Somente eu', score: { essential: 3 } },
                { value: 'small_team', label: 'De 2 a 6 pessoas', score: { pro: 3 } },
                { value: 'large_team', label: 'Mais de 6 pessoas', score: { master: 3 } }
            ]
        },
        {
            id: 'frequency',
            title: 'Com que frequencia voce quer atualizar o sistema?',
            hint: 'Ajuda a calibrar automacao e volume de uso.',
            options: [
                { value: 'weekly', label: '1 vez por semana', score: { essential: 2 } },
                { value: 'daily', label: 'Quase todos os dias', score: { pro: 2 } },
                { value: 'all_day', label: 'Varias vezes ao dia', score: { master: 2 } }
            ]
        },
        {
            id: 'whatsapp',
            title: 'Quer lancar tarefas e transacoes pelo WhatsApp?',
            hint: 'Para agilizar registros sem abrir o sistema o tempo todo.',
            options: [
                { value: 'yes', label: 'Quero', score: { pro: 2, master: 2 } },
                { value: 'no', label: 'Nao quero agora', score: { essential: 1 } }
            ]
        },
        {
            id: 'task_flow',
            title: 'Seu fluxo precisa de tarefas com etapas e responsaveis?',
            hint: 'Importante para organizacao compartilhada.',
            options: [
                { value: 'basic', label: 'Nao, tarefas simples', score: { essential: 2 } },
                { value: 'structured', label: 'Sim, com etapas', score: { pro: 2 } },
                { value: 'complex', label: 'Sim, com varios responsaveis', score: { master: 2 } }
            ]
        },
        {
            id: 'imports',
            title: 'Voce pretende importar extratos e historicos em lote?',
            hint: 'Ideal para ganhar velocidade na migracao.',
            options: [
                { value: 'low', label: 'Pouco ou nunca', score: { essential: 1 } },
                { value: 'medium', label: 'As vezes', score: { pro: 2 } },
                { value: 'high', label: 'Com frequencia', score: { master: 2 } }
            ]
        },
        {
            id: 'insights',
            title: 'Deseja IA para resumos e sugestoes no seu contexto?',
            hint: 'Mais automacao para decisoes no dia a dia.',
            options: [
                { value: 'yes', label: 'Sim, quero IA ativa', score: { pro: 2, master: 2 } },
                { value: 'maybe', label: 'Talvez, uso moderado', score: { essential: 1, pro: 1 } },
                { value: 'no', label: 'Nao por enquanto', score: { essential: 1 } }
            ]
        },
        {
            id: 'alerts',
            title: 'Voce quer alertas e acompanhamentos frequentes?',
            hint: 'Lembretes e visibilidade de pendencias.',
            options: [
                { value: 'simple', label: 'Alertas basicos', score: { essential: 1 } },
                { value: 'regular', label: 'Alertas semanais', score: { pro: 2 } },
                { value: 'intense', label: 'Alertas e monitoramento continuo', score: { master: 2 } }
            ]
        },
        {
            id: 'volume',
            title: 'Quantas movimentacoes financeiras por mes voce registra?',
            hint: 'Ajuda a projetar o volume ideal de uso.',
            options: [
                { value: 'up_50', label: 'Ate 50', score: { essential: 2 } },
                { value: 'up_300', label: 'Entre 50 e 300', score: { pro: 2 } },
                { value: 'more_300', label: 'Mais de 300', score: { master: 2 } }
            ]
        },
        {
            id: 'multi_workspace',
            title: 'Voce precisa separar contextos em mais de um workspace?',
            hint: 'Ex.: pessoal, familiar e profissional.',
            options: [
                { value: 'single', label: 'Um unico workspace basta', score: { essential: 2 } },
                { value: 'few', label: 'Dois ou tres workspaces', score: { pro: 2 } },
                { value: 'many', label: 'Varios workspaces com equipe', score: { master: 3 } }
            ]
        },
        {
            id: 'growth',
            title: 'Como voce enxerga o crescimento do uso nos proximos 6 meses?',
            hint: 'Ajuda a indicar o plano mais aderente para hoje e para evolucao.',
            options: [
                { value: 'stable', label: 'Manter estavel', score: { essential: 2 } },
                { value: 'grow', label: 'Crescer gradualmente', score: { pro: 2 } },
                { value: 'scale', label: 'Escalar rapido', score: { master: 3 } }
            ]
        }
    ];

    const planMeta = {
        essential: {
            label: 'Essencial',
            cta: 'Comecar com Essencial',
            perks: [
                'Uso individual com foco em organizacao financeira e tarefas.',
                'Painel claro com controle de entradas, saidas e prazos.',
                'Base ideal para evoluir sem complexidade inicial.'
            ]
        },
        pro: {
            label: 'Pro',
            cta: 'Comecar com Pro',
            perks: [
                'Equilibrio ideal para colaboracao e produtividade.',
                'Mais automacao com IA, filtros e operacao fluida.',
                'Perfeito para familia, pequeno time ou negocio em crescimento.'
            ]
        },
        master: {
            label: 'Master',
            cta: 'Comecar com Master',
            perks: [
                'Escala para equipes maiores e alto volume operacional.',
                'Maior flexibilidade de convidados e governanca.',
                'Melhor escolha para consolidar multiplos fluxos de trabalho.'
            ]
        }
    };

    const storage = {
        read() {
            try {
                const raw = localStorage.getItem(storageKey);
                return raw ? JSON.parse(raw) : null;
            } catch (err) {
                return null;
            }
        },
        write(payload) {
            try {
                localStorage.setItem(storageKey, JSON.stringify(payload));
            } catch (err) {
                // ignore
            }
        },
        clear() {
            try {
                localStorage.removeItem(storageKey);
            } catch (err) {
                // ignore
            }
        }
    };

    const totalSteps = quizQuestions.length;
    if (stepTotal) stepTotal.textContent = String(totalSteps);

    let state = {
        step: 0,
        answers: {},
        done: false,
        plan: null,
        ts: null
    };

    const saved = storage.read();
    if (saved && typeof saved === 'object') {
        state = {
            ...state,
            ...saved,
            step: Number.isFinite(saved.step) ? Math.max(0, Math.min(saved.step, totalSteps - 1)) : 0,
            answers: saved.answers && typeof saved.answers === 'object' ? saved.answers : {}
        };
    }

    let isAnimating = false;
    let autoTimer = null;

    const persist = () => storage.write({ ...state, ts: Date.now() });

    const scorePlan = () => {
        const scores = { essential: 0, pro: 0, master: 0 };
        quizQuestions.forEach((question) => {
            const answer = state.answers[question.id];
            if (!answer) return;
            const option = question.options.find((item) => item.value === answer);
            if (!option || !option.score) return;
            Object.keys(scores).forEach((key) => {
                scores[key] += option.score[key] || 0;
            });
        });
        const top = Object.entries(scores).sort((a, b) => b[1] - a[1]);
        return { plan: top[0][0], scores };
    };

    const buildSummary = () => {
        const out = [];
        if (state.answers.team_size === 'solo') out.push('Perfil individual com foco em controle pessoal.');
        if (state.answers.team_size === 'small_team') out.push('Colaboracao em equipe pequena.');
        if (state.answers.team_size === 'large_team') out.push('Fluxo compartilhado com equipe ampla.');
        if (state.answers.whatsapp === 'yes') out.push('Deseja operacao rapida via WhatsApp.');
        if (state.answers.volume === 'more_300') out.push('Volume alto de transacoes mensais.');
        if (state.answers.insights === 'yes') out.push('Alta abertura para IA e automacao.');
        if (state.answers.multi_workspace === 'many') out.push('Precisa separar multiplos contextos.');
        return out.slice(0, 5);
    };

    const setProgress = () => {
        const current = Math.min(state.step + 1, totalSteps);
        if (stepCurrent) stepCurrent.textContent = String(current);
        const pct = Math.round((current / totalSteps) * 100);
        if (progressBar) {
            progressBar.style.width = `${pct}%`;
            progressBar.setAttribute('aria-valuenow', String(pct));
            progressBar.classList.remove('is-pulse');
            if (!reduceMotion) {
                void progressBar.offsetWidth;
                progressBar.classList.add('is-pulse');
            }
        }
    };

    const buildDots = () => {
        if (!dotsWrap) return;
        dotsWrap.innerHTML = '';
        for (let i = 0; i < totalSteps; i += 1) {
            const dot = document.createElement('span');
            dot.className = 'quiz-dot';
            dot.dataset.index = String(i);
            dotsWrap.appendChild(dot);
        }
    };

    const updateDots = () => {
        if (!dotsWrap) return;
        const dots = dotsWrap.querySelectorAll('.quiz-dot');
        dots.forEach((dot, idx) => {
            dot.classList.toggle('is-done', idx < state.step);
            dot.classList.toggle('is-active', idx === state.step);
        });
    };

    const questionByIndex = (index) => quizQuestions[Math.max(0, Math.min(index, totalSteps - 1))];

    const createOptionButton = (question, option) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'quiz-option itr-focus-ring';
        button.dataset.value = option.value;
        button.setAttribute('role', 'radio');
        button.setAttribute('aria-checked', state.answers[question.id] === option.value ? 'true' : 'false');
        button.innerHTML = `
            <span class="quiz-option-content">
                <span class="quiz-option-label">${option.label}</span>
                <span class="quiz-option-check"><i class="fa-solid fa-check"></i></span>
            </span>
        `;
        if (state.answers[question.id] === option.value) button.classList.add('is-selected');
        return button;
    };

    const createQuestionPanelContent = (index) => {
        const question = questionByIndex(index);
        const wrapper = document.createElement('div');
        wrapper.className = 'quiz-question-shell';
        wrapper.innerHTML = `
            <h2 class="quiz-question-title" id="quizQuestionTitle" tabindex="-1">${question.title}</h2>
            <p class="quiz-question-hint">${question.hint}</p>
            <div class="quiz-options" role="radiogroup" aria-label="${question.title}"></div>
        `;
        const optionsContainer = wrapper.querySelector('.quiz-options');
        question.options.forEach((option) => {
            optionsContainer.appendChild(createOptionButton(question, option));
        });
        return wrapper;
    };

    const syncStageHeight = () => {
        if (!stage || !currentPanel) return;
        const currentHeight = currentPanel.scrollHeight || 280;
        stage.style.height = `${Math.max(currentHeight, 280)}px`;
    };

    const focusQuestion = () => {
        const title = currentPanel ? currentPanel.querySelector('#quizQuestionTitle') : null;
        if (title) title.focus();
    };

    const updateButtons = () => {
        const question = questionByIndex(state.step);
        if (prevBtn) prevBtn.disabled = state.step === 0;
        if (nextBtn) nextBtn.disabled = !state.answers[question.id];
    };

    const renderCurrentPanel = () => {
        if (!currentPanel) return;
        currentPanel.innerHTML = '';
        currentPanel.appendChild(createQuestionPanelContent(state.step));
        currentPanel.classList.add('is-current');
        currentPanel.classList.remove('is-incoming');
        setProgress();
        updateDots();
        updateButtons();
        syncStageHeight();
        bindPanelOptionEvents(currentPanel, state.step);
    };

    const setSelectedOption = (panel, question, value) => {
        const buttons = panel.querySelectorAll('.quiz-option');
        buttons.forEach((btn) => {
            const selected = btn.dataset.value === value;
            btn.classList.toggle('is-selected', selected);
            btn.setAttribute('aria-checked', selected ? 'true' : 'false');
        });
        if (nextBtn) nextBtn.disabled = !state.answers[question.id];
    };

    const rippleOption = (button, sourceEvent) => {
        if (!button || reduceMotion) return;
        const rect = button.getBoundingClientRect();
        const clientX = sourceEvent && Number.isFinite(sourceEvent.clientX) ? sourceEvent.clientX : rect.left + (rect.width / 2);
        const clientY = sourceEvent && Number.isFinite(sourceEvent.clientY) ? sourceEvent.clientY : rect.top + (rect.height / 2);
        button.style.setProperty('--x', `${clientX - rect.left}px`);
        button.style.setProperty('--y', `${clientY - rect.top}px`);
        button.classList.remove('is-rippling');
        void button.offsetWidth;
        button.classList.add('is-rippling');
    };

    const chooseOption = (panel, question, value, sourceEvent) => {
        state.answers[question.id] = value;
        persist();
        setSelectedOption(panel, question, value);
        rippleOption(sourceEvent ? sourceEvent.currentTarget : null, sourceEvent);
        if (debug) console.debug('[quiz] option_selected', { question: question.id, value });
        if (autoTimer) clearTimeout(autoTimer);
        autoTimer = setTimeout(() => {
            autoTimer = null;
            goNext();
        }, reduceMotion ? 0 : 230);
    };

    function bindPanelOptionEvents(panel, stepIndex) {
        const question = questionByIndex(stepIndex);
        const buttons = panel.querySelectorAll('.quiz-option');
        buttons.forEach((button) => {
            button.addEventListener('click', (event) => {
                if (isAnimating || state.done) return;
                chooseOption(panel, question, button.dataset.value, event);
            });
            button.addEventListener('keydown', (event) => {
                if (event.key !== 'Enter' && event.key !== ' ') return;
                event.preventDefault();
                if (isAnimating || state.done) return;
                chooseOption(panel, question, button.dataset.value, event);
            });
            button.addEventListener('animationend', (event) => {
                if (event.animationName === 'itr-quiz-ripple') {
                    button.classList.remove('is-rippling');
                }
            });
        });
    }

    const finishTransition = (targetIndex) => {
        const oldCurrent = currentPanel;
        currentPanel = incomingPanel;
        incomingPanel = oldCurrent;

        incomingPanel.className = 'quiz-step-panel is-incoming';
        incomingPanel.innerHTML = '';
        incomingPanel.setAttribute('aria-hidden', 'true');

        currentPanel.className = 'quiz-step-panel is-current';
        currentPanel.removeAttribute('aria-hidden');

        state.step = targetIndex;
        persist();
        setProgress();
        updateDots();
        updateButtons();
        syncStageHeight();
        focusQuestion();

        stageTrack.classList.remove('is-animating', 'dir-next', 'dir-prev');
        isAnimating = false;
    };

    const switchToStep = (targetIndex, direction) => {
        if (isAnimating) return;
        const bounded = Math.max(0, Math.min(targetIndex, totalSteps - 1));
        if (bounded === state.step) return;

        if (reduceMotion) {
            state.step = bounded;
            persist();
            renderCurrentPanel();
            focusQuestion();
            return;
        }

        isAnimating = true;

        incomingPanel.innerHTML = '';
        incomingPanel.appendChild(createQuestionPanelContent(bounded));
        incomingPanel.className = 'quiz-step-panel is-incoming';
        incomingPanel.removeAttribute('aria-hidden');

        bindPanelOptionEvents(incomingPanel, bounded);

        const nextHeight = incomingPanel.scrollHeight || 280;
        const currentHeight = currentPanel.scrollHeight || 280;
        stage.style.height = `${Math.max(nextHeight, currentHeight, 280)}px`;

        stageTrack.classList.remove('dir-next', 'dir-prev');
        stageTrack.classList.add('is-animating', direction === 'prev' ? 'dir-prev' : 'dir-next');

        const onEnd = (event) => {
            if (event.target !== incomingPanel) return;
            incomingPanel.removeEventListener('animationend', onEnd);
            finishTransition(bounded);
        };
        incomingPanel.addEventListener('animationend', onEnd);
    };

    const renderResult = (planKey) => {
        const meta = planMeta[planKey] || planMeta.essential;
        const summary = buildSummary();
        const ctaUrl = `${registerUrl}?plan=${encodeURIComponent(planKey)}`;

        if (resultWrap) {
            resultWrap.classList.add('is-visible');
            resultWrap.innerHTML = `
                <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-3">
                    <h5 class="mb-0">Plano sugerido: ${meta.label}</h5>
                    <span class="badge text-bg-primary quiz-result-badge">Resultado do quiz</span>
                </div>
                <div class="row g-3">
                    <div class="col-lg-6">
                        <div class="fw-semibold mb-2">Seu perfil</div>
                        <ul class="quiz-result-list">
                            ${summary.map((item) => `<li>${item}</li>`).join('')}
                        </ul>
                    </div>
                    <div class="col-lg-6">
                        <div class="fw-semibold mb-2">Por que este plano</div>
                        <ul class="quiz-result-list">
                            ${meta.perks.map((item) => `<li>${item}</li>`).join('')}
                        </ul>
                    </div>
                </div>
                <div class="d-flex flex-wrap gap-2 mt-3">
                    <a class="btn btn-primary quiz-cta-btn" href="${ctaUrl}" id="quizCtaPrimary">${meta.cta}</a>
                    <a class="btn btn-outline-light quiz-cta-btn" href="${loginUrl}">Ja tenho acesso</a>
                    <button type="button" class="btn btn-outline-light quiz-cta-btn" id="quizRetakeBtn">Refazer quiz</button>
                </div>
            `;
            const retake = document.getElementById('quizRetakeBtn');
            if (retake) retake.addEventListener('click', resetQuiz);
            resultWrap.querySelectorAll('.quiz-cta-btn').forEach((btn) => {
                btn.addEventListener('pointerdown', () => btn.classList.add('is-press'));
                btn.addEventListener('pointerup', () => btn.classList.remove('is-press'));
                btn.addEventListener('pointerleave', () => btn.classList.remove('is-press'));
            });
        }

        if (actionsWrap) actionsWrap.classList.add('d-none');
        if (stage) stage.classList.add('d-none');

        if (debug) console.debug('[quiz] quiz_completed', { plan: planKey, answers: state.answers });
    };

    const completeQuiz = () => {
        const result = scorePlan();
        state.done = true;
        state.plan = result.plan;
        state.ts = Date.now();
        persist();
        setProgress();
        updateDots();
        renderResult(result.plan);
    };

    const resetQuiz = () => {
        if (autoTimer) {
            clearTimeout(autoTimer);
            autoTimer = null;
        }
        state = {
            step: 0,
            answers: {},
            done: false,
            plan: null,
            ts: null
        };
        storage.clear();

        if (actionsWrap) actionsWrap.classList.remove('d-none');
        if (stage) stage.classList.remove('d-none');
        if (resultWrap) {
            resultWrap.classList.remove('is-visible');
            resultWrap.innerHTML = '';
        }

        renderCurrentPanel();
        focusQuestion();
    };

    const goNext = () => {
        if (state.done || isAnimating) return;
        const question = questionByIndex(state.step);
        if (!state.answers[question.id]) return;
        if (state.step >= totalSteps - 1) {
            completeQuiz();
            return;
        }
        switchToStep(state.step + 1, 'next');
    };

    const goPrev = () => {
        if (state.done || isAnimating) return;
        if (autoTimer) {
            clearTimeout(autoTimer);
            autoTimer = null;
        }
        if (state.step <= 0) return;
        switchToStep(state.step - 1, 'prev');
    };

    const skipQuestion = () => {
        if (state.done || isAnimating) return;
        if (autoTimer) {
            clearTimeout(autoTimer);
            autoTimer = null;
        }
        if (state.step >= totalSteps - 1) {
            completeQuiz();
            return;
        }
        switchToStep(state.step + 1, 'next');
    };

    const init = () => {
        buildDots();
        updateDots();

        if (prevBtn) prevBtn.addEventListener('click', goPrev);
        if (nextBtn) nextBtn.addEventListener('click', goNext);
        if (skipBtn) skipBtn.addEventListener('click', skipQuestion);
        if (resetBtn) resetBtn.addEventListener('click', resetQuiz);

        if (state.done && state.plan) {
            setProgress();
            renderResult(state.plan);
        } else {
            renderCurrentPanel();
            focusQuestion();
        }

        const motionHost = document.querySelector('.brand-motion-host');
        document.querySelectorAll('.motion-hover-target').forEach((element) => {
            element.addEventListener('mouseenter', () => {
                if (motionHost) motionHost.classList.add('motion-boost');
            });
            element.addEventListener('mouseleave', () => {
                if (motionHost) motionHost.classList.remove('motion-boost');
            });
        });

        window.addEventListener('resize', () => {
            if (!state.done) syncStageHeight();
        });

        if (debug) console.debug('[quiz] quiz_started', { restored: !!saved, state });
    };

    init();
})();
