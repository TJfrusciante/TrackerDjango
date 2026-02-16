(() => {
    if (window.__itrQuizMotionInitialized) return;
    window.__itrQuizMotionInitialized = true;

    const quizPage = document.querySelector('.plan-quiz-page');
    if (!quizPage) return;

    const stepCurrent = document.getElementById('quizStepCurrent');
    const stepTotal = document.getElementById('quizStepTotal');
    const quizResult = document.getElementById('quizResult');
    const stageTrack = document.getElementById('quizStageTrack');
    let lastStep = Number.parseInt(stepCurrent?.textContent || '1', 10) || 1;

    const setAmbientMood = (payload) => {
        if (!payload) return;
        if (typeof window.itrSetAmbientMood === 'function') {
            window.itrSetAmbientMood(payload);
        } else {
            window.__itrAmbientPendingMood = payload;
        }
    };

    const animateStepTransition = (nextStep) => {
        if (!stageTrack) return;
        const dir = nextStep >= lastStep ? 'dir-next' : 'dir-prev';
        quizPage.classList.add('is-transitioning');
        stageTrack.classList.remove('dir-next', 'dir-prev', 'is-animating');
        void stageTrack.offsetWidth;
        stageTrack.classList.add(dir, 'is-animating');
        window.setTimeout(() => {
            stageTrack.classList.remove('is-animating', 'dir-next', 'dir-prev');
            quizPage.classList.remove('is-transitioning');
        }, 360);
    };

    const applyAmbientForQuiz = () => {
        const total = Number.parseInt(stepTotal?.textContent || '10', 10) || 10;
        const current = Number.parseInt(stepCurrent?.textContent || '1', 10) || 1;
        const progress = Math.max(0, Math.min(1, current / total));
        const hasResult = !!quizResult && quizResult.classList.contains('is-visible');

        if (hasResult) {
            const resultText = (quizResult.textContent || '').toLowerCase();
            let mood = 'neutral';
            let score = 14;
            if (resultText.includes('master')) {
                mood = 'positive';
                score = 52;
            } else if (resultText.includes('pro')) {
                mood = 'positive';
                score = 34;
            } else if (resultText.includes('essencial')) {
                mood = 'neutral';
                score = 18;
            }
            setAmbientMood({ mode: 'lite', mood, score, source: 'quiz-result' });
            return;
        }

        const score = Math.round(progress * 28);
        const mood = progress >= 0.65 ? 'positive' : 'neutral';
        setAmbientMood({ mode: 'lite', mood, score, source: 'quiz-progress' });
    };

    const bindOptionRipple = () => {
        quizPage.addEventListener('click', (event) => {
            const option = event.target.closest('.quiz-option');
            if (!option) return;
            const rect = option.getBoundingClientRect();
            option.style.setProperty('--x', `${event.clientX - rect.left}px`);
            option.style.setProperty('--y', `${event.clientY - rect.top}px`);
            option.classList.remove('is-rippling');
            void option.offsetWidth;
            option.classList.add('is-rippling');
        });
    };

    const observeState = () => {
        const observer = new MutationObserver(() => {
            const nextStep = Number.parseInt(stepCurrent?.textContent || '1', 10) || 1;
            if (nextStep !== lastStep) {
                animateStepTransition(nextStep);
                lastStep = nextStep;
            }
            applyAmbientForQuiz();
        });
        if (stepCurrent) {
            observer.observe(stepCurrent, { childList: true, characterData: true, subtree: true });
        }
        if (quizResult) {
            observer.observe(quizResult, { childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ['class'] });
        }
    };

    bindOptionRipple();
    observeState();
    applyAmbientForQuiz();
})();
