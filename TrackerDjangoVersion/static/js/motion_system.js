(() => {
    if (window.__itrMotionSystemInitialized) return;
    window.__itrMotionSystemInitialized = true;

    const reduceMotionMedia = typeof window.matchMedia === 'function'
        ? window.matchMedia('(prefers-reduced-motion: reduce)')
        : null;
    let revealObserver = null;

    const reduceMotion = () => !!(reduceMotionMedia && reduceMotionMedia.matches);
    const animationsPref = () => ((document.body && document.body.getAttribute('data-animations')) || 'auto').toLowerCase();
    const animationsOff = () => animationsPref() === 'off';

    const revealTargets = () => Array.from(document.querySelectorAll('[data-motion="reveal"], .itr-reveal'));

    const syncAnimatedClass = () => {
        if (!document.body) return;
        document.body.classList.toggle('itr-ms-animated', !reduceMotion() && !animationsOff());
    };

    const markPageReady = () => {
        document.body.classList.add('itr-ms-ready');
        syncAnimatedClass();
    };

    const forceVisibleWhenOff = () => {
        revealTargets().forEach((target) => {
            target.classList.add('itr-ms-visible', 'is-visible');
            target.style.opacity = '1';
            target.style.transform = 'none';
            target.style.filter = 'none';
            target.style.visibility = 'visible';
        });
        document.querySelectorAll('.table-responsive.d-none').forEach((node) => node.classList.remove('d-none'));
        document.querySelectorAll('.itr-loading-shell').forEach((shell) => shell.classList.add('is-loaded'));
        document.querySelectorAll('.itr-loading-shell .skeleton, .itr-loading-shell .itr-skeleton').forEach((node) => {
            node.classList.add('d-none');
        });
    };

    const clearForcedVisibleStyles = () => {
        revealTargets().forEach((target) => {
            target.style.removeProperty('opacity');
            target.style.removeProperty('transform');
            target.style.removeProperty('filter');
            target.style.removeProperty('visibility');
        });
    };

    const disconnectRevealObserver = () => {
        if (revealObserver) {
            revealObserver.disconnect();
            revealObserver = null;
        }
    };

    const applyReveal = () => {
        const targets = revealTargets();
        if (!targets.length) return;

        targets.forEach((target) => {
            if (target.dataset.stagger === '1') {
                Array.from(target.children).slice(0, 12).forEach((child, idx) => {
                    child.style.setProperty('--itr-ms-stagger', `${Math.min(idx * 72, 420)}ms`);
                });
            }
        });

        if (reduceMotion() || animationsOff()) {
            disconnectRevealObserver();
            targets.forEach((target) => target.classList.add('itr-ms-visible'));
            forceVisibleWhenOff();
            return;
        }

        clearForcedVisibleStyles();
        disconnectRevealObserver();
        if (typeof window.IntersectionObserver !== 'function') {
            targets.forEach((target) => target.classList.add('itr-ms-visible'));
            return;
        }
        revealObserver = new IntersectionObserver((entries, obs) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) return;
                entry.target.classList.add('itr-ms-visible');
                obs.unobserve(entry.target);
            });
        }, { threshold: 0.18, rootMargin: '0px 0px -8% 0px' });

        targets.forEach((target) => revealObserver.observe(target));
    };

    const bindInteractiveMotion = () => {
        const interactive = Array.from(document.querySelectorAll(
            '.glass-card, .list-group-item, .page-action-btn, .btn, .task-mini-card, .tx-mini-card, .tx-kpi-card'
        ));

        interactive.forEach((el) => {
            if (el.dataset.itrMotionBound === '1') return;
            el.dataset.itrMotionBound = '1';
            el.classList.add('itr-hover-lift', 'itr-hover-glow', 'itr-ripple', 'itr-focus-ring');
            if (el.tagName === 'A' || el.tagName === 'BUTTON' || el.classList.contains('btn')) {
                el.classList.add('itr-active-highlight');
            }
            el.addEventListener('pointerdown', (event) => {
                if (reduceMotion() || animationsOff()) return;
                const rect = el.getBoundingClientRect();
                const x = event.clientX - rect.left;
                const y = event.clientY - rect.top;
                el.style.setProperty('--itr-ripple-x', `${x}px`);
                el.style.setProperty('--itr-ripple-y', `${y}px`);
                el.classList.remove('itr-ms-rippling');
                void el.offsetWidth;
                el.classList.add('itr-ms-rippling');
                el.classList.add('itr-pressing');
            }, { passive: true });

            el.addEventListener('pointerup', () => el.classList.remove('itr-pressing'), { passive: true });
            el.addEventListener('pointerleave', () => el.classList.remove('itr-pressing'), { passive: true });
            el.addEventListener('animationend', (event) => {
                if (event.animationName === 'itr-ms-ripple') {
                    el.classList.remove('itr-ms-rippling');
                }
            });
        });
    };

    const bindLoadingShells = () => {
        const shells = Array.from(document.querySelectorAll('.itr-loading-shell'));
        if (!shells.length) return;

        const updateShell = (shell) => {
            const skeletons = Array.from(shell.querySelectorAll('.skeleton, .itr-skeleton'));
            const hasVisibleSkeleton = skeletons.some((node) => {
                const styles = window.getComputedStyle(node);
                return styles.display !== 'none' && styles.visibility !== 'hidden' && !node.classList.contains('d-none');
            });
            shell.classList.toggle('is-loaded', !hasVisibleSkeleton);
        };

        shells.forEach((shell) => {
            updateShell(shell);
            const observer = new MutationObserver(() => updateShell(shell));
            observer.observe(shell, {
                attributes: true,
                childList: true,
                subtree: true,
                attributeFilter: ['class', 'style']
            });
            setTimeout(() => updateShell(shell), 1200);
            setTimeout(() => updateShell(shell), 2200);
        });
    };

    const init = () => {
        markPageReady();
        applyReveal();
        bindInteractiveMotion();
        bindLoadingShells();
    };

    const syncForAnimationsPreference = () => {
        syncAnimatedClass();
        if (animationsOff()) {
            document.body.classList.add('itr-ms-off');
            forceVisibleWhenOff();
            disconnectRevealObserver();
            return;
        }
        document.body.classList.remove('itr-ms-off');
        applyReveal();
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }

    window.addEventListener('itr:animations-change', syncForAnimationsPreference);
    if (reduceMotionMedia && typeof reduceMotionMedia.addEventListener === 'function') {
        reduceMotionMedia.addEventListener('change', syncForAnimationsPreference);
    }
})();
