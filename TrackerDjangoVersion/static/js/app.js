(() => {
    const transitionEl = document.getElementById('pageTransition');
    const transitionText = transitionEl ? transitionEl.querySelector('#pageTransitionText') : null;
    const defaultTransitionText = transitionText ? transitionText.textContent : 'Carregando...';
    const showTransition = () => {
        if (!transitionEl) return;
        transitionEl.classList.remove('is-success');
        transitionEl.classList.add('is-active');
        if (transitionText) transitionText.textContent = defaultTransitionText;
    };
    const hideTransition = () => {
        if (!transitionEl) return;
        transitionEl.classList.remove('is-active');
    };

    window.addEventListener('pageshow', () => {
        hideTransition();
    });

    const getCookie = (name) => {
        const value = `; ${document.cookie}`;
        const parts = value.split(`; ${name}=`);
        if (parts.length === 2) return parts.pop().split(';').shift();
        return '';
    };

    const initTooltips = () => {
        if (!window.bootstrap || !bootstrap.Tooltip) return;
        document.querySelectorAll('[data-bs-toggle="tooltip"], .js-tooltip').forEach((el) => {
            if (el.dataset.tooltipReady === '1') return;
            new bootstrap.Tooltip(el);
            el.dataset.tooltipReady = '1';
        });
    };

    const initUndoToasts = () => {
        if (!window.bootstrap || !bootstrap.Toast) return;
        const csrfToken = getCookie('csrftoken');
        document.querySelectorAll('.undo-toast').forEach((el) => {
            if (el.dataset.toastReady === '1') return;
            const toast = new bootstrap.Toast(el, { delay: 6000 });
            el.addEventListener('hidden.bs.toast', () => {
                const url = el.dataset.clearUrl;
                if (!url) return;
                fetch(url, {
                    method: 'POST',
                    headers: { 'X-CSRFToken': csrfToken },
                });
            });
            toast.show();
            el.dataset.toastReady = '1';
        });
    };

    const initRevealOnScroll = () => {
        const targets = Array.from(document.querySelectorAll('[data-reveal], .reveal-on-scroll'));
        if (!targets.length) return;
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
            targets.forEach((el) => el.classList.add('is-visible'));
            return;
        }
        const observer = new IntersectionObserver((entries, obs) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('is-visible');
                    obs.unobserve(entry.target);
                }
            });
        }, { threshold: 0.2 });
        targets.forEach((el) => observer.observe(el));
    };

    const initPanelFade = () => {
        document.querySelectorAll('.glass-card').forEach((el, idx) => {
            if (el.dataset.fadeReady === '1') return;
            el.classList.add('fade-in-panel');
            el.style.animationDelay = `${Math.min(idx * 0.03, 0.2)}s`;
            el.dataset.fadeReady = '1';
        });
    };

    document.addEventListener('DOMContentLoaded', () => {
        initTooltips();
        initUndoToasts();
        initRevealOnScroll();
        initPanelFade();
    });

    const isInternalLink = (url) => {
        try {
            const parsed = new URL(url, window.location.origin);
            return parsed.origin === window.location.origin;
        } catch {
            return false;
        }
    };

    document.addEventListener('click', (event) => {
        const link = event.target.closest('a');
        if (!link) return;
        if (link.dataset.noTransition === '1' || link.closest('[data-no-transition]')) return;
        if (link.getAttribute('target') && link.getAttribute('target') !== '_self') return;
        if (link.hasAttribute('download')) return;
        const href = link.getAttribute('href') || '';
        if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
        if (!isInternalLink(href)) return;
        showTransition();
    }, true);

    document.addEventListener('submit', (event) => {
        const form = event.target;
        if (!form || !(form instanceof HTMLFormElement)) return;
        if (form.dataset.noTransition === '1' || form.closest('[data-no-transition]')) return;
        if (form.dataset.ajax === '1') return;
        showTransition();
    }, true);

    const setupSuperuserWorkspaceSearch = () => {
        const dataScript = document.getElementById('superWorkspaceData');
        const input = document.getElementById('superWsSearch');
        const results = document.getElementById('superWsResults');
        const button = document.getElementById('superWsSearchBtn');
        if (!dataScript || !input || !results) return;
        let data = [];
        try {
            data = JSON.parse(dataScript.textContent || '[]');
        } catch {
            data = [];
        }
        const base = results.dataset.switchBase || '';
        const renderResults = (items) => {
            results.innerHTML = '';
            if (!items.length) {
                results.classList.remove('d-none');
                const empty = document.createElement('div');
                empty.className = 'list-group-item text-secondary small';
                empty.textContent = 'Nenhum workspace encontrado.';
                results.appendChild(empty);
                return;
            }
            results.classList.remove('d-none');
            items.forEach((item) => {
                const link = document.createElement('a');
                link.href = base ? base.replace('slug-placeholder', item.slug) : '#';
                link.className = 'list-group-item list-group-item-action py-1';
                link.innerHTML = `<div class="small fw-semibold">${item.name}</div><div class="text-secondary small">${item.owner ? `Owner: ${item.owner}` : ''} · ${item.slug}</div>`;
                results.appendChild(link);
            });
        };
        const performSearch = () => {
            const term = input.value.trim().toLowerCase();
            if (!term) {
                results.classList.add('d-none');
                results.innerHTML = '';
                return;
            }
            const matches = data.filter(item => {
                const name = (item.name || '').toLowerCase();
                const slug = (item.slug || '').toLowerCase();
                const owner = (item.owner || '').toLowerCase();
                return name.includes(term) || slug.includes(term) || owner.includes(term);
            }).slice(0, 8);
            renderResults(matches);
        };
        input.addEventListener('input', performSearch);
        if (button) {
            button.addEventListener('click', (ev) => {
                ev.preventDefault();
                performSearch();
            });
        }
        document.addEventListener('click', (event) => {
            if (!results.contains(event.target) && event.target !== input && event.target !== button) {
                results.classList.add('d-none');
            }
        });
    };

    document.addEventListener('DOMContentLoaded', setupSuperuserWorkspaceSearch);

    const loginForm = document.getElementById('loginForm');
    if (loginForm) {
        loginForm.addEventListener('submit', async (event) => {
            event.preventDefault();
            if (loginForm.dataset.submitting === '1') return;
            loginForm.dataset.submitting = '1';
            const card = document.getElementById('loginCard') || document.querySelector('[data-login-card]') || loginForm.closest('.auth-card') || loginForm;
            const showError = (message) => {
                if (errorBox) {
                    errorBox.textContent = message;
                    errorBox.classList.remove('d-none');
                }
                if (card) {
                    if (card.animate) {
                        card.animate(
                            [
                                { transform: 'translateX(0)' },
                                { transform: 'translateX(-8px)' },
                                { transform: 'translateX(8px)' },
                                { transform: 'translateX(-6px)' },
                                { transform: 'translateX(6px)' },
                                { transform: 'translateX(0)' }
                            ],
                            { duration: 360, easing: 'ease' }
                        );
                    }
                    card.classList.remove('login-shake');
                    void card.offsetWidth;
                    card.classList.add('login-shake');
                }
                setTimeout(() => {
                    if (errorBox) errorBox.classList.add('d-none');
                }, 4000);
            };
            const ensureMessageBox = (id, classes) => {
                let box = document.getElementById(id);
                if (!box && card) {
                    box = document.createElement('div');
                    box.id = id;
                    box.className = classes;
                    card.insertBefore(box, card.firstChild);
                }
                return box;
            };
            const errorBox = ensureMessageBox('loginError', 'alert alert-danger py-2 small mb-3 d-none');
            const successBox = ensureMessageBox('loginSuccess', 'alert alert-success py-2 small mb-3 d-none');
            const submitBtn = loginForm.querySelector('button[type="submit"]');
            if (errorBox) {
                errorBox.classList.add('d-none');
                errorBox.textContent = '';
            }
            if (successBox) {
                successBox.classList.add('d-none');
                successBox.textContent = '';
            }
            if (submitBtn) submitBtn.disabled = true;
            showTransition();
            const formData = new FormData(loginForm);
            try {
                const res = await fetch(loginForm.action || window.location.href, {
                    method: 'POST',
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest',
                        'X-CSRFToken': getCookie('csrftoken')
                    },
                    credentials: 'same-origin',
                    body: formData
                });
                let data = { ok: res.ok };
                const contentType = res.headers.get('content-type') || '';
                if (contentType.includes('application/json')) {
                    data = await res.json();
                } else if (!res.ok) {
                    data.error = 'Não foi possível entrar. Verifique os dados.';
                }
                if (data.ok) {
                    const successMsg = data.message || 'Login realizado com sucesso.';
                    if (successBox) {
                        successBox.textContent = successMsg;
                        successBox.classList.remove('d-none');
                    }
                    if (transitionEl) transitionEl.classList.add('is-success');
                    if (transitionText) transitionText.textContent = successMsg;
                    setTimeout(() => {
                        window.location.href = data.redirect || '/app/';
                    }, 650);
                    return;
                }
                hideTransition();
                loginForm.dataset.submitting = '0';
                if (submitBtn) submitBtn.disabled = false;
                showError(data.error || 'Não foi possível entrar. Verifique os dados.');
            } catch (err) {
                hideTransition();
                loginForm.dataset.submitting = '0';
                if (submitBtn) submitBtn.disabled = false;
                showError('Erro ao conectar. Tente novamente.');
            }
            loginForm.dataset.submitting = '0';
            if (submitBtn) submitBtn.disabled = false;
        });
    }
})();
