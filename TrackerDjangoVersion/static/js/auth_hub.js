(() => {
    const hub = document.getElementById('authHub');
    if (!hub) return;

    const panels = Array.from(document.querySelectorAll('[data-auth-panel]'));
    const tabs = Array.from(document.querySelectorAll('[data-auth-switch]'));
    const viewport = document.getElementById('authPanelsViewport');
    const track = document.getElementById('authPanelsTrack');

    const panelOrder = panels.map((panel) => panel.dataset.authPanel);
    const findPanelIndex = (name) => {
        const idx = panelOrder.indexOf(name);
        return idx >= 0 ? idx : 0;
    };

    const updateUrlState = (name) => {
        if (!history || !history.replaceState) return;
        const url = new URL(window.location.href);
        url.searchParams.set('panel', name);
        history.replaceState(null, '', url.toString());
    };

    const syncViewportHeight = (activePanel) => {
        if (!viewport || !activePanel) return;
        const styles = window.getComputedStyle(viewport);
        const padding = parseFloat(styles.paddingTop || '0') + parseFloat(styles.paddingBottom || '0');
        const safeGap = 20;
        viewport.style.height = 'auto';
        const body = activePanel.querySelector('.auth-panel-body') || activePanel;
        const rectHeight = body.getBoundingClientRect().height || 0;
        const scrollHeight = body.scrollHeight || 0;
        const nextHeight = Math.max(rectHeight, scrollHeight) + padding + safeGap;
        viewport.style.height = `${Math.ceil(nextHeight)}px`;
    };

    const focusPanel = (activePanel) => {
        if (!activePanel) return;
        const errorField = activePanel.querySelector('.is-invalid, [aria-invalid="true"]');
        const focusTarget = errorField || activePanel.querySelector('input, select, textarea, button');
        if (focusTarget) {
            setTimeout(() => focusTarget.focus(), 120);
        }
    };

    const getViewportWidth = () => {
        if (!viewport) return 0;
        return viewport.clientWidth || viewport.getBoundingClientRect().width || 0;
    };

    const updateTrackPosition = (index) => {
        if (!track || !viewport) return;
        const width = getViewportWidth();
        if (!width) return;
        const offset = width * index;
        track.style.transform = `translate3d(-${offset}px, 0, 0)`;
    };

    const setActivePanel = (panelName, options = {}) => {
        const name = panelName || 'login';
        const index = findPanelIndex(name);
        panels.forEach((panel) => {
            const isActive = panel.dataset.authPanel === name;
            panel.classList.toggle('is-active', isActive);
            panel.setAttribute('aria-hidden', isActive ? 'false' : 'true');
        });
        tabs.forEach((tab) => {
            const isActive = tab.dataset.authSwitch === name;
            tab.classList.toggle('is-active', isActive);
            tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });
        updateTrackPosition(index);
        const activePanel = panels.find((panel) => panel.dataset.authPanel === name);
        const scrollY = window.scrollY;
        requestAnimationFrame(() => syncViewportHeight(activePanel));
        setTimeout(() => syncViewportHeight(activePanel), 180);
        setTimeout(() => syncViewportHeight(activePanel), 420);
        setTimeout(() => updateTrackPosition(index), 40);
        setTimeout(() => updateTrackPosition(index), 220);
        if (Math.abs(window.scrollY - scrollY) > 2) {
            window.scrollTo({ top: scrollY });
        }
        if (!options.skipFocus) {
            focusPanel(activePanel);
        }
        if (!options.skipUrl) {
            updateUrlState(name);
        }
    };

    const getInitialPanel = () => {
        const query = new URLSearchParams(window.location.search).get('panel');
        if (query) return query;
        const hash = (window.location.hash || '').replace('#', '');
        if (hash) return hash;
        return hub.dataset.activePanel || 'login';
    };

    const initialPanel = getInitialPanel();
    setActivePanel(initialPanel, { skipUrl: true });

    tabs.forEach((tab) => {
        tab.addEventListener('click', (event) => {
            event.preventDefault();
            const target = tab.dataset.authSwitch;
            if (!target) return;
            setActivePanel(target);
        });
    });

    const setupPasswordRules = (form) => {
        const rulesBox = form.querySelector('.password-rules');
        if (!rulesBox) return;
        const pwd = form.querySelector('input[name="password1"]');
        const confirm = form.querySelector('input[name="password2"]');
        const username = form.querySelector('input[name="username"]');
        const email = form.querySelector('input[name="email"]');
        const firstName = form.querySelector('input[name="first_name"]');
        const lastName = form.querySelector('input[name="last_name"]');
        if (!pwd) return;

        const commonList = new Set([
            '12345678','123456789','1234567890','senha123','password','qwerty','admin123','iloveyou'
        ]);

        const ruleEls = {};
        rulesBox.querySelectorAll('[data-rule]').forEach((el) => {
            ruleEls[el.dataset.rule] = el;
        });

        const normalize = (value) => (value || '').toLowerCase().trim();

        const hasSimilarInfo = (pass) => {
            const tokens = [];
            [username, email, firstName, lastName].forEach((el) => {
                if (!el || !el.value) return;
                if (el === email) {
                    tokens.push(el.value.split('@')[0]);
                } else {
                    tokens.push(el.value);
                }
            });
            const cleaned = normalize(pass);
            return tokens.some((token) => {
                const t = normalize(token);
                return t.length >= 3 && cleaned.includes(t);
            });
        };

        const setRule = (rule, passed, active) => {
            const el = ruleEls[rule];
            if (!el) return;
            el.classList.remove('is-valid', 'is-invalid');
            if (!active) return;
            el.classList.add(passed ? 'is-valid' : 'is-invalid');
        };

        const updateRules = () => {
            const value = pwd.value || '';
            const confirmValue = confirm ? confirm.value || '' : '';
            const active = value.length > 0 || confirmValue.length > 0;

            const lengthOk = value.length >= 8;
            const numericOk = !/^\d+$/.test(value);
            const similarOk = !hasSimilarInfo(value);
            const commonOk = value.length ? !commonList.has(value.toLowerCase()) : false;
            const specialOk = /[^A-Za-z0-9]/.test(value);
            const matchOk = confirmValue.length ? value === confirmValue : false;

            setRule('length', lengthOk, active);
            setRule('numeric', numericOk, active);
            setRule('similar', similarOk, active);
            setRule('common', commonOk, active);
            setRule('special', specialOk, active);
            setRule('match', matchOk, confirmValue.length > 0);
        };

        pwd.addEventListener('input', updateRules);
        if (confirm) confirm.addEventListener('input', updateRules);
        if (username) username.addEventListener('input', updateRules);
        if (email) email.addEventListener('input', updateRules);
        if (firstName) firstName.addEventListener('input', updateRules);
        if (lastName) lastName.addEventListener('input', updateRules);
    };

    const setupMasterGuestField = (form) => {
        const planSelect = form.querySelector('#id_plan_choice');
        const masterField = form.querySelector('[data-master-guest]');
        if (!planSelect || !masterField) return;

        const toggleField = () => {
            const value = planSelect.value || '';
            const show = value.includes('master');
            masterField.classList.toggle('d-none', !show);
        };
        planSelect.addEventListener('change', toggleField);
        toggleField();
    };

    document.querySelectorAll('form[data-auth-form]').forEach((form) => {
        setupPasswordRules(form);
        if (form.dataset.authForm === 'register') {
            setupMasterGuestField(form);
        }
        if (form.dataset.authForm === 'guest' && form.dataset.guestWarning) {
            form.addEventListener('submit', (event) => {
                const ok = window.confirm(form.dataset.guestWarning);
                if (!ok) event.preventDefault();
            });
        }
    });

    const motionHost = document.querySelector('.brand-motion-host');
    document.querySelectorAll('.motion-hover-target').forEach((el) => {
        el.addEventListener('mouseenter', () => {
            if (motionHost) motionHost.classList.add('motion-boost');
        });
        el.addEventListener('mouseleave', () => {
            if (motionHost) motionHost.classList.remove('motion-boost');
        });
    });

    window.addEventListener('resize', () => {
        const activePanel = panels.find((panel) => panel.classList.contains('is-active')) || panels[0];
        requestAnimationFrame(() => {
            syncViewportHeight(activePanel);
            const index = findPanelIndex(activePanel.dataset.authPanel);
            updateTrackPosition(index);
        });
    });

    window.addEventListener('load', () => {
        const activePanel = panels.find((panel) => panel.classList.contains('is-active')) || panels[0];
        syncViewportHeight(activePanel);
        const index = findPanelIndex(activePanel.dataset.authPanel);
        updateTrackPosition(index);
    });

    if (window.ResizeObserver) {
        const observer = new ResizeObserver(() => {
            const activePanel = panels.find((panel) => panel.classList.contains('is-active')) || panels[0];
            syncViewportHeight(activePanel);
            const index = findPanelIndex(activePanel.dataset.authPanel);
            updateTrackPosition(index);
        });
        panels.forEach((panel) => observer.observe(panel));
    }
})();
