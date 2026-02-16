(() => {
    const root = document.querySelector('.js-mouse-field-root');
    if (!root || window.__itrMouseFieldInitialized) return;
    window.__itrMouseFieldInitialized = true;
    document.body.classList.add('public-mouse-field-only');
    document.body.dataset.ambientMode = 'off';

    const PREF_KEY = 'itrPublicMouseFieldPreference';
    const CYCLE = ['low', 'medium', 'high', 'off'];
    const fxButton = document.querySelector('.js-public-fx-toggle');
    const brandHost = document.querySelector('.brand-motion-host');
    const reducedMotionMedia = window.matchMedia('(prefers-reduced-motion: reduce)');

    const labels = {
        auto: 'Auto',
        low: 'Baixo',
        medium: 'Médio',
        high: 'Alto',
        off: 'Desligado',
    };

    const presets = {
        low: { spacing: 124, radius: 206, force: 48, driftX: 0.14, driftY: 0.11, particleBase: 14, particleAlpha: 0.25, lineAlpha: 0.18, speed: 0.3, pull: 0.00005 },
        medium: { spacing: 102, radius: 254, force: 68, driftX: 0.18, driftY: 0.14, particleBase: 24, particleAlpha: 0.34, lineAlpha: 0.23, speed: 0.4, pull: 0.00007 },
        high: { spacing: 86, radius: 304, force: 88, driftX: 0.21, driftY: 0.16, particleBase: 34, particleAlpha: 0.42, lineAlpha: 0.3, speed: 0.52, pull: 0.000095 },
    };

    const supportsCanvas = typeof window.requestAnimationFrame === 'function' && typeof document.createElement('canvas').getContext === 'function';

    const normalizePreference = (value) => {
        const normalized = String(value || '').toLowerCase();
        if (normalized === 'low' || normalized === 'medium' || normalized === 'high' || normalized === 'off') return normalized;
        return 'auto';
    };

    const isMobileLike = () => (
        window.matchMedia('(pointer: coarse)').matches
        || window.innerWidth <= 992
        || (navigator.maxTouchPoints || 0) > 0
    );

    const currentThemeMode = () => {
        const htmlMode = document.documentElement.getAttribute('data-bs-theme');
        const bodyMode = document.body ? document.body.getAttribute('data-bs-theme') : null;
        return (htmlMode || bodyMode || 'light').toLowerCase().startsWith('dark') ? 'dark' : 'light';
    };

    const resolveAutoMode = () => {
        if (reducedMotionMedia.matches) return 'off';
        if (document.body && document.body.dataset.animations === 'off') return 'off';
        if (isMobileLike()) return 'low';
        return currentThemeMode() === 'dark' ? 'medium' : 'low';
    };

    const getEffectiveMode = (preference) => {
        if (reducedMotionMedia.matches) return 'off';
        if (document.body && document.body.dataset.animations === 'off') return 'off';
        if (preference === 'auto') return resolveAutoMode();
        return preference;
    };

    let storedPreference = 'auto';
    try {
        storedPreference = localStorage.getItem(PREF_KEY) || 'auto';
    } catch (err) {
        storedPreference = 'auto';
    }
    let preference = normalizePreference(storedPreference);
    let effectiveMode = getEffectiveMode(preference);

    const updateControlLabel = () => {
        if (!fxButton) return;
        const resolvedLabel = labels[effectiveMode] || labels.medium;
        fxButton.textContent = `Efeito: ${resolvedLabel}`;
        fxButton.dataset.fx = effectiveMode;
        fxButton.dataset.pref = preference;
        const title = preference === 'auto'
            ? `Efeito automático (${resolvedLabel}). Clique para alternar. Botão direito: Auto.`
            : `Efeito ${resolvedLabel}. Clique para alternar. Botão direito: Auto.`;
        fxButton.setAttribute('title', title);
        fxButton.setAttribute('aria-label', title);
    };

    const persistPreference = () => {
        try {
            localStorage.setItem(PREF_KEY, preference);
        } catch (err) {
            // Ignore storage failures.
        }
    };

    let canvas = null;
    let ctx = null;
    let rafId = null;
    let paused = false;
    let vw = 0;
    let vh = 0;
    let dpr = 1;
    let gridRows = [];
    let gridCols = [];
    let particles = [];

    const pointer = {
        x: 0,
        y: 0,
        active: false,
    };

    const attractor = {
        x: 0,
        y: 0,
    };

    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

    const setSharedParallaxVars = () => {
        if (!vw || !vh) return;
        const offsetX = (attractor.x - (vw / 2)) / 18;
        const offsetY = (attractor.y - (vh / 2)) / 18;
        document.documentElement.style.setProperty('--itr-ambient-mouse-x', `${offsetX.toFixed(2)}px`);
        document.documentElement.style.setProperty('--itr-ambient-mouse-y', `${offsetY.toFixed(2)}px`);
        if (brandHost) {
            brandHost.style.setProperty('--itr-brand-mouse-x', `${(offsetX * 1.2).toFixed(2)}px`);
            brandHost.style.setProperty('--itr-brand-mouse-y', `${(offsetY * 1.2).toFixed(2)}px`);
        }
    };

    const buildGrid = () => {
        gridRows = [];
        gridCols = [];
        const preset = presets[effectiveMode] || presets.low;
        const spacing = preset.spacing * (isMobileLike() ? 1.2 : 1);
        for (let y = -spacing; y <= vh + spacing; y += spacing) {
            gridRows.push(y);
        }
        for (let x = -spacing; x <= vw + spacing; x += spacing) {
            gridCols.push(x);
        }
    };

    const ensureCanvas = () => {
        if (!supportsCanvas) return false;
        if (canvas && ctx) return true;
        canvas = document.createElement('canvas');
        canvas.id = 'mouseFieldCanvas';
        canvas.className = 'mouse-field-canvas';
        canvas.setAttribute('aria-hidden', 'true');

        const appLayer = document.querySelector('.itr-app-layer');
        if (document.body) {
            if (appLayer && appLayer.parentNode === document.body) {
                document.body.insertBefore(canvas, appLayer);
            } else {
                document.body.appendChild(canvas);
            }
        }

        ctx = canvas.getContext('2d', { alpha: true, desynchronized: true });
        return !!ctx;
    };

    const resize = () => {
        if (!ctx || !canvas) return;
        vw = window.innerWidth;
        vh = window.innerHeight;
        dpr = Math.min(window.devicePixelRatio || 1, 1.45);

        canvas.width = Math.floor(vw * dpr);
        canvas.height = Math.floor(vh * dpr);
        canvas.style.width = `${vw}px`;
        canvas.style.height = `${vh}px`;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        attractor.x = vw / 2;
        attractor.y = vh / 2;

        buildGrid();
        seedParticles();
        setSharedParallaxVars();
    };

    const seedParticles = () => {
        if (!ctx) return;
        const preset = presets[effectiveMode] || presets.low;
        const mobileFactor = isMobileLike() ? 0.62 : 1;
        const baseByArea = Math.sqrt((vw * vh) / (1920 * 1080));
        const count = clamp(Math.round((preset.particleBase * baseByArea * mobileFactor)), 8, 52);
        particles = Array.from({ length: count }).map(() => ({
            x: Math.random() * vw,
            y: Math.random() * vh,
            vx: (Math.random() - 0.5) * preset.speed,
            vy: (Math.random() - 0.5) * preset.speed,
            r: 0.9 + Math.random() * 2.4,
            twinkle: Math.random() * Math.PI * 2,
            speed: 0.0007 + Math.random() * 0.0012,
        }));
    };

    const displacePoint = (x, y, preset) => {
        const dx = x - attractor.x;
        const dy = y - attractor.y;
        const dist = Math.hypot(dx, dy) || 1;
        if (dist > preset.radius) return [x, y];
        const t = 1 - (dist / preset.radius);
        // Stronger falloff near the cursor for a clearer "gravitational field" feeling.
        const push = (t * t * preset.force) * (1 + (t * 1.9));
        const clampedPush = Math.min(push, preset.force * 2.2);
        return [x + ((dx / dist) * clampedPush), y + ((dy / dist) * clampedPush)];
    };

    const drawGrid = (theme, timestamp) => {
        if (!ctx) return;
        const preset = presets[effectiveMode] || presets.low;
        const alpha = preset.lineAlpha * (isMobileLike() ? 0.6 : 1);
        const lineColor = theme === 'dark'
            ? `rgba(170, 214, 255, ${alpha})`
            : `rgba(30, 64, 175, ${alpha * 0.8})`;

        const shimmer = 0.8 + Math.sin(timestamp * 0.0008) * 0.2;
        ctx.strokeStyle = lineColor;
        ctx.lineWidth = theme === 'dark' ? 1.05 : 0.95;
        ctx.globalAlpha = shimmer;

        for (const y of gridRows) {
            ctx.beginPath();
            gridCols.forEach((x, index) => {
                const [px, py] = displacePoint(x, y, preset);
                if (index === 0) ctx.moveTo(px, py);
                else ctx.lineTo(px, py);
            });
            ctx.stroke();
        }

        for (const x of gridCols) {
            ctx.beginPath();
            gridRows.forEach((y, index) => {
                const [px, py] = displacePoint(x, y, preset);
                if (index === 0) ctx.moveTo(px, py);
                else ctx.lineTo(px, py);
            });
            ctx.stroke();
        }

        ctx.globalAlpha = 1;
    };

    const drawParticles = (theme, timestamp) => {
        if (!ctx || !particles.length || effectiveMode === 'off') return;
        const preset = presets[effectiveMode] || presets.low;
        const alphaBase = preset.particleAlpha * (isMobileLike() ? 0.62 : 1);
        const color = theme === 'dark' ? '110, 241, 255' : '15, 118, 110';

        particles.forEach((p) => {
            const pullX = (attractor.x - p.x) * preset.pull;
            const pullY = (attractor.y - p.y) * preset.pull;
            p.vx = (p.vx + pullX) * 0.996;
            p.vy = (p.vy + pullY) * 0.996;
            p.x += p.vx;
            p.y += p.vy;

            if (p.x < -20) p.x = vw + 20;
            if (p.x > vw + 20) p.x = -20;
            if (p.y < -20) p.y = vh + 20;
            if (p.y > vh + 20) p.y = -20;

            const twinkle = 0.65 + Math.sin((timestamp * p.speed) + p.twinkle) * 0.35;
            const alpha = clamp(alphaBase * twinkle, 0.08, 0.86);

            ctx.beginPath();
            ctx.fillStyle = `rgba(${color}, ${alpha})`;
            ctx.shadowBlur = effectiveMode === 'high' ? 18 : 12;
            ctx.shadowColor = `rgba(${color}, ${alpha * 0.45})`;
            ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
            ctx.fill();
            ctx.shadowBlur = 0;
        });
    };

    const drawFrame = (timestamp) => {
        rafId = null;
        if (paused || document.hidden || reducedMotionMedia.matches || effectiveMode === 'off') return;
        if (!ctx) return;

        const preset = presets[effectiveMode] || presets.low;
        const t = timestamp || performance.now();
        const driftX = (vw / 2) + Math.cos(t * 0.00021) * (vw * preset.driftX);
        const driftY = (vh / 2) + Math.sin(t * 0.00016) * (vh * preset.driftY);
        if (pointer.active) {
            attractor.x = pointer.x;
            attractor.y = pointer.y;
        } else {
            attractor.x += (driftX - attractor.x) * 0.09;
            attractor.y += (driftY - attractor.y) * 0.09;
        }
        setSharedParallaxVars();

        ctx.clearRect(0, 0, vw, vh);
        const theme = currentThemeMode();
        drawGrid(theme, t);
        drawParticles(theme, t);

        rafId = window.requestAnimationFrame(drawFrame);
    };

    const stopLoop = () => {
        if (rafId) {
            window.cancelAnimationFrame(rafId);
            rafId = null;
        }
        if (ctx) {
            ctx.clearRect(0, 0, vw, vh);
        }
    };

    const startLoop = () => {
        if (!ctx || paused || document.hidden || reducedMotionMedia.matches || effectiveMode === 'off') return;
        if (!rafId) {
            rafId = window.requestAnimationFrame(drawFrame);
        }
    };

    const applyPreference = (nextPreference, persist = true) => {
        preference = normalizePreference(nextPreference);
        effectiveMode = getEffectiveMode(preference);
        if (persist) persistPreference();
        updateControlLabel();

        if (!supportsCanvas || !ensureCanvas()) return;

        if (effectiveMode === 'off' || reducedMotionMedia.matches) {
            stopLoop();
            return;
        }

        resize();
        startLoop();
    };

    const cyclePreference = () => {
        if (preference === 'auto') {
            applyPreference('low', true);
            return;
        }
        const index = CYCLE.indexOf(preference);
        const next = CYCLE[(index + 1) % CYCLE.length];
        applyPreference(next, true);
    };

    const handlePointerMove = (x, y) => {
        pointer.x = x;
        pointer.y = y;
        pointer.active = true;
    };

    const handleMouseMove = (event) => {
        handlePointerMove(event.clientX, event.clientY);
    };

    const handleTouchMove = (event) => {
        const touch = event.touches && event.touches[0];
        if (!touch) return;
        handlePointerMove(touch.clientX, touch.clientY);
    };

    const handlePointerLeave = () => {
        pointer.active = false;
    };

    const onVisibilityChange = () => {
        paused = document.hidden;
        if (paused) {
            stopLoop();
            return;
        }
        startLoop();
    };

    if (fxButton) {
        fxButton.addEventListener('click', cyclePreference);
        fxButton.addEventListener('contextmenu', (event) => {
            event.preventDefault();
            applyPreference('auto', true);
        });
    }

    window.addEventListener('mousemove', handleMouseMove, { passive: true });
    window.addEventListener('touchstart', handleTouchMove, { passive: true });
    window.addEventListener('touchmove', handleTouchMove, { passive: true });
    window.addEventListener('touchend', handlePointerLeave, { passive: true });
    window.addEventListener('mouseleave', handlePointerLeave, { passive: true });
    window.addEventListener('resize', () => {
        if (!ctx) return;
        if (preference === 'auto') {
            effectiveMode = getEffectiveMode('auto');
            updateControlLabel();
        }
        resize();
    }, { passive: true });

    document.addEventListener('visibilitychange', onVisibilityChange, { passive: true });
    window.addEventListener('itr:animations-change', () => {
        applyPreference(preference, false);
    });

    if (typeof reducedMotionMedia.addEventListener === 'function') {
        reducedMotionMedia.addEventListener('change', () => applyPreference(preference, false));
    } else if (typeof reducedMotionMedia.addListener === 'function') {
        reducedMotionMedia.addListener(() => applyPreference(preference, false));
    }

    const themeObserver = new MutationObserver(() => {
        if (preference === 'auto') {
            applyPreference('auto', false);
        }
    });
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-bs-theme'] });

    updateControlLabel();

    if (supportsCanvas && ensureCanvas()) {
        resize();
        applyPreference(preference, false);
    }
})();
