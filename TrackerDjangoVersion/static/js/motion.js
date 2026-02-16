(() => {
    if (window.__itrMotionSystemInitialized) return;
    if (window.__itrMotionInitialized) return;
    window.__itrMotionInitialized = true;

    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const initPageEnter = () => {
        document.body.classList.add('itr-page-enter');
        requestAnimationFrame(() => {
            document.body.classList.add('itr-page-enter-active');
        });
    };

    const initReveal = () => {
        const targets = Array.from(document.querySelectorAll('[data-motion="reveal"]'));
        if (!targets.length) return;

        const applyStagger = (el) => {
            if (!el.dataset.stagger) return;
            const children = Array.from(el.children).slice(0, 12);
            children.forEach((child, idx) => {
                child.style.setProperty('--itr-stagger-delay', `${Math.min(idx * 70, 280)}ms`);
                child.classList.add('itr-reveal');
            });
        };

        targets.forEach((el) => {
            el.classList.add('itr-reveal');
            applyStagger(el);
        });

        if (prefersReduced) {
            targets.forEach((el) => el.classList.add('is-visible'));
            return;
        }

        const observer = new IntersectionObserver((entries, obs) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) return;
                entry.target.classList.add('is-visible');
                if (entry.target.dataset.stagger) {
                    Array.from(entry.target.children).forEach((child) => child.classList.add('is-visible'));
                }
                obs.unobserve(entry.target);
            });
        }, { threshold: 0.2 });

        targets.forEach((el) => observer.observe(el));
    };

    document.addEventListener('DOMContentLoaded', () => {
        initPageEnter();
        initReveal();
        if (!prefersReduced) {
            document.querySelectorAll('.glass-card, .task-mini-card, .tx-mini-card, .tx-kpi-card, .page-hero, .task-hero, .tx-hero').forEach((el) => {
                el.classList.add('itr-hover-lift');
            });
            document.querySelectorAll('.page-action-btn, .auth-tab').forEach((el) => {
                el.classList.add('itr-hover-glow');
            });
        }
        document.querySelectorAll('a.btn, button.btn, .btn, .auth-tab').forEach((el) => el.classList.add('itr-focus-ring'));
    });
})();
