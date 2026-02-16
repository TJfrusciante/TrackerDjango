(() => {
    const quizBlock = document.getElementById('planQuizBlock');
    if (!quizBlock) return;

    const quizBody = document.getElementById('planQuizBody');
    const startBtn = document.getElementById('startPlanQuizBtn');
    const resetBtn = document.getElementById('resetPlanQuizBtn');
    const prevBtn = document.getElementById('quizPrevBtn');
    const nextBtn = document.getElementById('quizNextBtn');
    const questionTitle = document.getElementById('quizQuestionTitle');
    const questionHint = document.getElementById('quizQuestionHint');
    const optionsWrap = document.getElementById('quizOptions');
    const resultWrap = document.getElementById('quizResult');
    const questionWrap = document.querySelector('.lp-quiz-question-wrap');
    const actionsWrap = document.querySelector('.lp-quiz-actions');
    const stepCurrent = document.getElementById('quizStepCurrent');
    const stepTotal = document.getElementById('quizStepTotal');
    const progressBar = document.getElementById('quizProgressBar');
    const modalEl = document.getElementById('planQuizPromptModal');
    const startFromModal = document.getElementById('startPlanQuizFromModal');

    const registerUrl = quizBlock.dataset.registerUrl || '/register/';
    const loginUrl = quizBlock.dataset.loginUrl || '/login/';
    const storageKey = quizBlock.dataset.quizKey || 'itracker:landing:plan-quiz';
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const debug = window.location.hostname === 'localhost' || localStorage.getItem('itracker:quiz:debug') === '1';

    const quizQuestions = [
        {
            id: 'team_size',
            title: 'Voce vai usar o iTracker sozinho ou com outras pessoas?',
            hint: 'Isso ajuda a definir o nivel de colaboracao.',
            options: [
                { value: 'solo', label: 'Somente eu', score: { essential: 3 } },
                { value: 'small', label: '2 a 6 pessoas', score: { pro: 3 } },
                { value: 'large', label: 'Mais de 6 pessoas', score: { master: 3 } }
            ]
        },
        {
            id: 'whatsapp',
            title: 'Deseja lancar tarefas e transacoes pelo WhatsApp?',
            hint: 'Ideal para quem quer rapidez no dia a dia.',
            options: [
                { value: 'yes', label: 'Sim, quero', score: { pro: 2, master: 2 } },
                { value: 'no', label: 'Nao preciso', score: { essential: 2 } }
            ]
        },
        {
            id: 'tasks',
            title: 'Voce precisa gerenciar tarefas com etapas e responsaveis?',
            hint: 'Recursos de produtividade e equipes.',
            options: [
                { value: 'yes', label: 'Sim', score: { pro: 2, master: 2 } },
                { value: 'no', label: 'Nao', score: { essential: 1 } }
            ]
        },
        {
            id: 'volume',
            title: 'Quantas movimentacoes financeiras voce registra por mes?',
            hint: 'Ajuda a definir o volume ideal do plano.',
            options: [
                { value: 'low', label: 'Poucas (ate 50)', score: { essential: 2 } },
                { value: 'medium', label: 'Medias (50 a 300)', score: { pro: 2 } },
                { value: 'high', label: 'Muitas (300+)', score: { master: 2 } }
            ]
        },
        {
            id: 'insights',
            title: 'Quer mais automacao e relatorios inteligentes?',
            hint: 'IA, resumos e alertas automatizados.',
            options: [
                { value: 'yes', label: 'Sim, gostaria', score: { pro: 1, master: 2 } },
                { value: 'no', label: 'Nao preciso agora', score: { essential: 1 } }
            ]
        },
        {
            id: 'growth',
            title: 'Voce pretende crescer o uso do sistema nos proximos meses?',
            hint: 'Serve para planejar upgrades futuros.',
            options: [
                { value: 'yes', label: 'Sim, quero crescer', score: { master: 2, pro: 1 } },
                { value: 'no', label: 'Nao, manter estavel', score: { essential: 1, pro: 1 } }
            ]
        }
    ];

    const planMeta = {
        essential: {
            label: 'Essencial',
            cta: 'Comecar com Essencial',
            perks: [
                'Ideal para uso individual e organizacao pessoal.',
                'Controle completo de entradas, saidas e tarefas.',
                'Dashboards e alertas essenciais.'
            ]
        },
        pro: {
            label: 'Pro',
            cta: 'Comecar com Pro',
            perks: [
                'Perfeito para familias ou pequenos times.',
                'WhatsApp + IA com mais autonomia.',
                'Mais limites para tarefas e automacoes.'
            ]
        },
        master: {
            label: 'Master',
            cta: 'Comecar com Master',
            perks: [
                'Para equipes maiores e operacao em escala.',
                'Limites e convidados ajustaveis.',
                'Melhor para alto volume e insights.'
            ]
        }
    };

    const storage = {
        read() {
            try {
                const raw = localStorage.getItem(storageKey);
                return raw ? JSON.parse(raw) : null;
            } catch {
                return null;
            }
        },
        write(data) {
            try {
                localStorage.setItem(storageKey, JSON.stringify(data));
            } catch {
                // ignore
            }
        }
    };

    let state = {
        step: 0,
        answers: {},
        done: false,
        dismissed: false,
        plan: null,
        ts: null
    };

    const saved = storage.read();
    if (saved) {
        state = { ...state, ...saved };
    }

    const totalSteps = quizQuestions.length;
    if (stepTotal) stepTotal.textContent = totalSteps.toString();

    const updateProgress = () => {
        const current = Math.min(state.step + 1, totalSteps);
        if (stepCurrent) stepCurrent.textContent = current.toString();
        if (progressBar) {
            const pct = Math.round((current / totalSteps) * 100);
            progressBar.style.width = `${pct}%`;
            progressBar.setAttribute('aria-valuenow', `${pct}`);
            progressBar.setAttribute('aria-valuemin', '0');
            progressBar.setAttribute('aria-valuemax', '100');
        }
    };

    const scorePlan = () => {
        const scores = { essential: 0, pro: 0, master: 0 };
        quizQuestions.forEach((q) => {
            const answer = state.answers[q.id];
            if (!answer) return;
            const opt = q.options.find((o) => o.value === answer);
            if (!opt || !opt.score) return;
            Object.keys(scores).forEach((key) => {
                scores[key] += opt.score[key] || 0;
            });
        });
        const sorted = Object.entries(scores).sort((a, b) => b[1] - a[1]);
        return { plan: sorted[0][0], scores };
    };

    const buildSummary = () => {
        const summary = [];
        if (state.answers.team_size === 'solo') summary.push('Uso individual e foco pessoal.');
        if (state.answers.team_size === 'small') summary.push('Colaboracao com pequeno grupo.');
        if (state.answers.team_size === 'large') summary.push('Equipe ampla e fluxo intenso.');
        if (state.answers.whatsapp === 'yes') summary.push('Quer lancar via WhatsApp para ganhar tempo.');
        if (state.answers.tasks === 'yes') summary.push('Precisa de tarefas com etapas e responsaveis.');
        if (state.answers.volume === 'high') summary.push('Volume alto de transacoes mensais.');
        if (state.answers.insights === 'yes') summary.push('Valoriza relatorios e IA.');
        return summary.slice(0, 4);
    };

    const renderResult = (planKey) => {
        if (!resultWrap) return;
        const meta = planMeta[planKey] || planMeta.essential;
        const summary = buildSummary();
        const ctaUrl = `${registerUrl}?plan=${encodeURIComponent(planKey)}`;
        resultWrap.classList.remove('d-none');
        if (questionWrap) questionWrap.classList.add('d-none');
        if (actionsWrap) actionsWrap.classList.add('d-none');
        resultWrap.innerHTML = `
            <div class="lp-quiz-result-card">
                <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-2">
                    <h5 class="mb-0">Plano sugerido: ${meta.label}</h5>
                    <span class="badge bg-primary-subtle text-primary">Resultado do quiz</span>
                </div>
                <div class="row g-3">
                    <div class="col-lg-6">
                        <div class="fw-semibold mb-2">Seu perfil</div>
                        <ul class="lp-muted small ps-3 mb-0">
                            ${summary.map(item => `<li class="mb-1">${item}</li>`).join('')}
                        </ul>
                    </div>
                    <div class="col-lg-6">
                        <div class="fw-semibold mb-2">Por que este plano</div>
                        <ul class="lp-muted small ps-3 mb-0">
                            ${meta.perks.map(item => `<li class="mb-1">${item}</li>`).join('')}
                        </ul>
                    </div>
                </div>
                <div class="d-flex flex-wrap gap-2 mt-3">
                    <a class="btn btn-primary" href="${ctaUrl}">${meta.cta}</a>
                    <a class="btn btn-outline-light" href="${loginUrl}">Ja tenho acesso</a>
                    <button type="button" class="btn btn-outline-light" id="quizRetakeInline">Refazer quiz</button>
                </div>
            </div>
        `;
        const retake = document.getElementById('quizRetakeInline');
        if (retake) {
            retake.addEventListener('click', () => resetQuiz());
        }
        if (debug) console.debug('[quiz] quiz_completed', { plan: planKey, answers: state.answers });
    };

    const setQuizActive = () => {
        if (quizBody) quizBody.classList.remove('d-none');
        quizBlock.classList.add('lp-quiz-shell--active');
        quizBlock.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' });
    };

    const setQuizInactive = () => {
        if (quizBody) quizBody.classList.add('d-none');
        quizBlock.classList.remove('lp-quiz-shell--active');
    };

    const animateQuestionSwap = (cb) => {
        if (!questionTitle || reduceMotion) {
            cb();
            return;
        }
        const container = questionTitle.closest('.lp-quiz-question-wrap');
        if (!container) {
            cb();
            return;
        }
        container.classList.remove('fade-slide-in');
        container.classList.add('fade-slide-out');
        container.addEventListener('animationend', () => {
            container.classList.remove('fade-slide-out');
            cb();
            requestAnimationFrame(() => container.classList.add('fade-slide-in'));
        }, { once: true });
    };

    const renderQuestion = () => {
        const question = quizQuestions[state.step];
        if (!question) return;
        updateProgress();
        if (questionTitle) questionTitle.textContent = question.title;
        if (questionHint) questionHint.textContent = question.hint || '';
        if (questionWrap) questionWrap.classList.remove('d-none');
        if (actionsWrap) actionsWrap.classList.remove('d-none');
        if (optionsWrap) {
            optionsWrap.innerHTML = '';
            optionsWrap.setAttribute('aria-label', question.title);
            question.options.forEach((opt) => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'lp-quiz-option';
                btn.setAttribute('role', 'radio');
                btn.setAttribute('aria-checked', 'false');
                btn.dataset.value = opt.value;
                btn.innerHTML = `<span>${opt.label}</span><i class="fa-solid fa-check"></i>`;
                if (state.answers[question.id] === opt.value) {
                    btn.classList.add('is-selected');
                    btn.setAttribute('aria-checked', 'true');
                }
                btn.addEventListener('click', () => {
                    state.answers[question.id] = opt.value;
                    storage.write({ ...state, done: false });
                    optionsWrap.querySelectorAll('.lp-quiz-option').forEach((el) => {
                        el.classList.remove('is-selected');
                        el.setAttribute('aria-checked', 'false');
                    });
                    btn.classList.add('is-selected');
                    btn.setAttribute('aria-checked', 'true');
                    if (nextBtn) nextBtn.disabled = false;
                });
                btn.addEventListener('keydown', (event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        btn.click();
                    }
                });
                optionsWrap.appendChild(btn);
            });
        }
        if (prevBtn) prevBtn.disabled = state.step === 0;
        if (nextBtn) {
            nextBtn.disabled = !state.answers[question.id];
            nextBtn.innerHTML = state.step === totalSteps - 1 ? 'Ver resultado<i class="fa-solid fa-arrow-right ms-2"></i>' : 'Proxima<i class="fa-solid fa-arrow-right ms-2"></i>';
        }
        if (resultWrap) resultWrap.classList.add('d-none');
        const focusTarget = optionsWrap ? optionsWrap.querySelector('.lp-quiz-option.is-selected') || optionsWrap.querySelector('.lp-quiz-option') : null;
        if (focusTarget) {
            focusTarget.focus({ preventScroll: true });
        } else if (questionTitle) {
            questionTitle.focus({ preventScroll: true });
        }
    };

    const startQuiz = () => {
        state.done = false;
        state.dismissed = false;
        state.step = 0;
        state.answers = {};
        setQuizActive();
        animateQuestionSwap(renderQuestion);
        storage.write({ ...state });
        if (debug) console.debug('[quiz] quiz_started');
    };

    const resetQuiz = () => {
        state = { step: 0, answers: {}, done: false, dismissed: false, plan: null, ts: Date.now() };
        storage.write(state);
        setQuizActive();
        animateQuestionSwap(renderQuestion);
        if (resetBtn) resetBtn.classList.add('d-none');
    };

    const completeQuiz = () => {
        const scored = scorePlan();
        state.plan = scored.plan;
        state.done = true;
        state.ts = Date.now();
        storage.write(state);
        renderResult(state.plan);
        if (resetBtn) resetBtn.classList.remove('d-none');
    };

    if (prevBtn) {
        prevBtn.addEventListener('click', () => {
            if (state.step === 0) return;
            state.step -= 1;
            animateQuestionSwap(renderQuestion);
            storage.write({ ...state, done: false });
        });
    }

    if (nextBtn) {
        nextBtn.addEventListener('click', () => {
            if (state.step >= totalSteps - 1) {
                completeQuiz();
                return;
            }
            state.step += 1;
            animateQuestionSwap(renderQuestion);
            storage.write({ ...state, done: false });
        });
    }

    if (startBtn) {
        startBtn.addEventListener('click', () => {
            startQuiz();
        });
    }

    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            resetQuiz();
        });
    }

    if (startFromModal) {
        startFromModal.addEventListener('click', () => {
            if (modal) {
                modalEl.dataset.quizStart = '1';
                modal.hide();
            }
            startQuiz();
        });
    }

    let modal;
    if (modalEl && window.bootstrap && bootstrap.Modal) {
        modal = new bootstrap.Modal(modalEl, { backdrop: 'static' });
        modalEl.addEventListener('hidden.bs.modal', () => {
            const isStarting = modalEl.dataset.quizStart === '1';
            modalEl.dataset.quizStart = '';
            if (!state.done && !state.dismissed && !state.step && !isStarting) {
                state.dismissed = true;
                storage.write(state);
            }
        });
    }

    const restoreIfDone = () => {
        if (state.done && state.plan) {
            setQuizActive();
            renderResult(state.plan);
            if (resetBtn) resetBtn.classList.remove('d-none');
            return true;
        }
        return false;
    };

    const autoPrompt = () => {
        if (state.done || state.dismissed) return;
        if (!modal) return;
        setTimeout(() => {
            modal.show();
        }, 700);
    };

    if (!restoreIfDone()) {
        setQuizInactive();
        if (state.step > 0 && Object.keys(state.answers).length) {
            setQuizActive();
            renderQuestion();
        } else {
            autoPrompt();
        }
    }

    const promoCounts = document.querySelectorAll('.promo-count[data-count]');
    promoCounts.forEach((el) => {
        const target = parseInt(el.dataset.count || '0', 10);
        if (!Number.isFinite(target)) return;
        if (reduceMotion) {
            el.textContent = target.toString();
            return;
        }
        let current = 0;
        const step = Math.max(1, Math.ceil(target / 40));
        const tick = () => {
            current = Math.min(target, current + step);
            el.textContent = current.toString();
            if (current < target) {
                requestAnimationFrame(tick);
            }
        };
        tick();
    });
})();
