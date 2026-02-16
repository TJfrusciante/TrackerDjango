(() => {
    if (window.__itrAmbientBgInitialized) return;
    window.__itrAmbientBgInitialized = true;

    const body = document.body;
    const ambientRoot = document.getElementById('itrAmbient');
    if (!body || !ambientRoot) return;

    const reduceMotionMedia = window.matchMedia('(prefers-reduced-motion: reduce)');
    const desktopMedia = window.matchMedia('(min-width: 993px)');
    const hasBrandMotion = !!document.querySelector('.brand-motion-host');
    if (hasBrandMotion) body.classList.add('has-brand-motion');

    const host = document.querySelector('.brand-motion-host');
    const canvas = document.getElementById('itrAmbientParticles');
    const ctx = canvas ? canvas.getContext('2d', { alpha: true }) : null;
    if (!body.dataset.animations) {
        body.dataset.animations = 'auto';
    }

    let particles = [];
    let animationId = null;
    let paused = false;
    let varsRaf = null;
    let driftRaf = null;
    let pointerX = 0;
    let pointerY = 0;
    let driftX = 0;
    let driftY = 0;

    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

    const numberFromVar = (name, fallback) => {
        const raw = getComputedStyle(ambientRoot).getPropertyValue(name).trim();
        const parsed = Number.parseFloat(raw);
        return Number.isFinite(parsed) ? parsed : fallback;
    };

    const getAmbientMode = () => {
        const mode = (body.dataset.ambientMode || 'full').toLowerCase();
        if (mode === 'lite' || mode === 'off') return mode;
        return 'full';
    };

    const getAmbientMood = () => {
        const mood = (body.dataset.ambientMood || 'neutral').toLowerCase();
        return (mood === 'positive' || mood === 'negative') ? mood : 'neutral';
    };

    const getAmbientScore = () => {
        const parsed = Number.parseInt(body.dataset.ambientScore || '0', 10);
        return Number.isFinite(parsed) ? clamp(parsed, -100, 100) : 0;
    };

    const reducedMotion = () => reduceMotionMedia.matches;

    const getAnimationPreference = () => {
        const pref = (body.dataset.animations || 'auto').toLowerCase();
        return pref === 'on' || pref === 'off' ? pref : 'auto';
    };

    const isLowPowerDevice = () => {
        const isMobile = !desktopMedia.matches;
        const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
        const saveData = !!(conn && conn.saveData);
        const lowMemory = Number.isFinite(Number(navigator.deviceMemory)) && Number(navigator.deviceMemory) <= 4;
        const lowCpu = Number.isFinite(Number(navigator.hardwareConcurrency)) && Number(navigator.hardwareConcurrency) <= 4;
        return isMobile || saveData || lowMemory || lowCpu;
    };

    const queueParallaxVars = () => {
        if (varsRaf) return;
        varsRaf = window.requestAnimationFrame(() => {
            varsRaf = null;
            const mixedX = pointerX + driftX;
            const mixedY = pointerY + driftY;
            document.documentElement.style.setProperty('--itr-ambient-mouse-x', `${mixedX}px`);
            document.documentElement.style.setProperty('--itr-ambient-mouse-y', `${mixedY}px`);
            if (host) {
                host.style.setProperty('--itr-brand-mouse-x', `${mixedX}px`);
                host.style.setProperty('--itr-brand-mouse-y', `${mixedY}px`);
            }
        });
    };

    const startAutoDrift = () => {
        if (reducedMotion() || getAnimationPreference() === 'off' || driftRaf) return;
        const loop = (ts) => {
            driftRaf = window.requestAnimationFrame(loop);
            const t = ts || performance.now();
            const mode = getAmbientMode();
            const liteFactor = mode === 'lite' ? 0.55 : 1;
            const brandFactor = hasBrandMotion ? 0.85 : 1;
            driftX = Math.sin(t * 0.00021) * 24 * liteFactor * brandFactor;
            driftY = Math.cos(t * 0.00017) * 18 * liteFactor * brandFactor;
            queueParallaxVars();
        };
        driftRaf = window.requestAnimationFrame(loop);
    };

    const stopAutoDrift = () => {
        if (driftRaf) {
            window.cancelAnimationFrame(driftRaf);
            driftRaf = null;
        }
        driftX = 0;
        driftY = 0;
        queueParallaxVars();
    };

    const onMouseMove = (event) => {
        const cx = window.innerWidth / 2;
        const cy = window.innerHeight / 2;
        pointerX = (event.clientX - cx) / 26;
        pointerY = (event.clientY - cy) / 26;
        queueParallaxVars();
    };

    const clearPointer = () => {
        pointerX = 0;
        pointerY = 0;
        queueParallaxVars();
    };

    if (!reducedMotion()) {
        window.addEventListener('mousemove', onMouseMove, { passive: true });
        window.addEventListener('mouseleave', clearPointer, { passive: true });
    } else {
        clearPointer();
    }

    const setRuntimeMoodVars = (mood, score) => {
        const safeMood = (mood === 'positive' || mood === 'negative') ? mood : 'neutral';
        const safeScore = clamp(Number.parseInt(score, 10) || 0, -100, 100);
        const factor = Math.abs(safeScore) / 100;

        let runtimeStrength = 1;
        let runtimeSpeed = 1;
        let runtimeParticles = 1;
        let runtimeGrid = 1;
        let runtimeSheen = 1;

        if (safeMood === 'positive') {
            runtimeStrength = 1 + (factor * 0.2);
            runtimeSpeed = 1 - (factor * 0.12);
            runtimeParticles = 1 + (factor * 0.26);
            runtimeGrid = 1 + (factor * 0.1);
            runtimeSheen = 1 + (factor * 0.16);
        } else if (safeMood === 'negative') {
            runtimeStrength = 1 - (factor * 0.2);
            runtimeSpeed = 1 + (factor * 0.14);
            runtimeParticles = 1 - (factor * 0.38);
            runtimeGrid = 1 - (factor * 0.16);
            runtimeSheen = 1 - (factor * 0.2);
        }

        ambientRoot.style.setProperty('--itr-ambient-runtime-strength', runtimeStrength.toFixed(3));
        ambientRoot.style.setProperty('--itr-ambient-runtime-speed', runtimeSpeed.toFixed(3));
        ambientRoot.style.setProperty('--itr-ambient-runtime-particles', runtimeParticles.toFixed(3));
        ambientRoot.style.setProperty('--itr-ambient-runtime-grid', runtimeGrid.toFixed(3));
        ambientRoot.style.setProperty('--itr-ambient-runtime-sheen', runtimeSheen.toFixed(3));
    };

    const shouldRunParticles = () => {
        if (!ctx || !canvas) return false;
        if (reducedMotion()) return false;
        const animationPref = getAnimationPreference();
        if (animationPref === 'off') return false;
        if (animationPref === 'auto' && isLowPowerDevice()) return false;
        if (!desktopMedia.matches) return false;
        const mode = getAmbientMode();
        if (mode === 'off') return false;
        return true;
    };

    const resizeCanvas = () => {
        if (!ctx || !canvas) return;
        const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
        canvas.width = Math.floor(window.innerWidth * dpr);
        canvas.height = Math.floor(window.innerHeight * dpr);
        canvas.style.width = `${window.innerWidth}px`;
        canvas.style.height = `${window.innerHeight}px`;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const createParticles = () => {
        const strength = numberFromVar('--itr-ambient-strength', 1);
        const runtime = numberFromVar('--itr-ambient-runtime-particles', 1);
        const mode = getAmbientMode();
        const liteFactor = mode === 'lite' ? 0.68 : 1;
        const brandFactor = hasBrandMotion ? 0.92 : 1;
        const base = Math.floor((window.innerWidth / 30) * (0.9 + strength * 0.34));
        const targetCount = clamp(Math.round(base * liteFactor * brandFactor * runtime), 30, 96);

        particles = Array.from({ length: targetCount }).map(() => ({
            x: Math.random() * window.innerWidth,
            y: Math.random() * window.innerHeight,
            vx: (Math.random() - 0.5) * (0.17 + Math.random() * 0.08),
            vy: (Math.random() - 0.5) * (0.17 + Math.random() * 0.08),
            size: 1.2 + Math.random() * 3.4,
            baseAlpha: 0.2 + Math.random() * 0.42,
            twinklePhase: Math.random() * Math.PI * 2,
            twinkleSpeed: 0.00055 + Math.random() * 0.0012,
        }));
    };

    const drawConnections = (alphaBase, color, mode, mood) => {
        if (!ctx || particles.length > 64 || mode === 'lite' || mood !== 'positive') return;
        const maxDistance = 132;
        for (let i = 0; i < particles.length; i += 1) {
            const p1 = particles[i];
            for (let j = i + 1; j < particles.length; j += 1) {
                const p2 = particles[j];
                const dx = p1.x - p2.x;
                const dy = p1.y - p2.y;
                const dist = Math.hypot(dx, dy);
                if (dist > maxDistance) continue;
                const alpha = (1 - (dist / maxDistance)) * alphaBase * 0.2;
                if (alpha <= 0.01) continue;
                ctx.beginPath();
                ctx.strokeStyle = `rgba(${color}, ${alpha})`;
                ctx.lineWidth = 0.7;
                ctx.moveTo(p1.x, p1.y);
                ctx.lineTo(p2.x, p2.y);
                ctx.stroke();
            }
        }
    };

    const updateParticles = (timestamp) => {
        if (!ctx || !canvas) return;

        const mode = getAmbientMode();
        const mood = getAmbientMood();
        const particlesAlpha = numberFromVar('--itr-ambient-particles-alpha', 0.52) * numberFromVar('--itr-ambient-runtime-particles', 1);
        ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);

        const theme = document.documentElement.getAttribute('data-bs-theme') || 'dark';
        const color = theme === 'light' ? '30, 41, 59' : '186, 230, 255';
        const parallaxX = (pointerX + driftX) * 0.025;
        const parallaxY = (pointerY + driftY) * 0.025;

        particles.forEach((p) => {
            p.x += p.vx + parallaxX;
            p.y += p.vy + parallaxY;

            if (p.x < -36) p.x = window.innerWidth + 36;
            if (p.x > window.innerWidth + 36) p.x = -36;
            if (p.y < -36) p.y = window.innerHeight + 36;
            if (p.y > window.innerHeight + 36) p.y = -36;

            const twinkle = 0.58 + 0.42 * Math.sin(timestamp * p.twinkleSpeed + p.twinklePhase);
            const alpha = clamp(p.baseAlpha * twinkle * particlesAlpha, 0.08, 0.92);
            const radius = p.size * (mode === 'lite' ? 0.88 : 1);

            ctx.beginPath();
            ctx.fillStyle = `rgba(${color}, ${alpha})`;
            ctx.shadowBlur = mode === 'lite' ? 10 : 16;
            ctx.shadowColor = `rgba(${color}, ${alpha * 0.5})`;
            ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
            ctx.fill();
            ctx.shadowBlur = 0;
        });

        drawConnections(particlesAlpha, color, mode, mood);
    };

    const tick = (timestamp) => {
        animationId = null;
        if (!shouldRunParticles() || paused) return;
        updateParticles(timestamp || performance.now());
        animationId = window.requestAnimationFrame(tick);
    };

    const startParticles = () => {
        if (!shouldRunParticles()) {
            if (ctx) ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
            return;
        }
        if (!particles.length) {
            resizeCanvas();
            createParticles();
        }
        if (!animationId) animationId = window.requestAnimationFrame(tick);
    };

    const stopParticles = () => {
        if (animationId) {
            window.cancelAnimationFrame(animationId);
            animationId = null;
        }
    };

    const refreshParticles = () => {
        stopParticles();
        if (shouldRunParticles()) {
            resizeCanvas();
            createParticles();
            startParticles();
        }
    };

    const applyAmbientState = ({ mood, score, mode, source } = {}) => {
        const incomingMode = (mode || getAmbientMode()).toLowerCase();
        const safeMode = (incomingMode === 'lite' || incomingMode === 'off') ? incomingMode : 'full';
        body.dataset.ambientMode = safeMode;

        const safeMood = (mood || getAmbientMood()).toLowerCase();
        const normalizedMood = (safeMood === 'positive' || safeMood === 'negative') ? safeMood : 'neutral';
        body.dataset.ambientMood = normalizedMood;

        const safeScore = clamp(Number.parseInt(score ?? getAmbientScore(), 10) || 0, -100, 100);
        body.dataset.ambientScore = String(safeScore);

        setRuntimeMoodVars(normalizedMood, safeScore);
        refreshParticles();

        if (window.localStorage && source === 'server') {
            try {
                localStorage.setItem('itr:ambient:last', JSON.stringify({ mood: normalizedMood, score: safeScore, mode: safeMode }));
            } catch (err) {
                // ignore storage failures
            }
        }
    };

    const applyPendingMood = () => {
        if (!window.__itrAmbientPendingMood) return false;
        const pending = window.__itrAmbientPendingMood;
        window.__itrAmbientPendingMood = null;
        applyAmbientState(pending || {});
        return true;
    };

    window.itrSetAmbientMood = (payload) => {
        if (!ambientRoot) {
            window.__itrAmbientPendingMood = payload || null;
            return;
        }
        applyAmbientState(payload || {});
    };

    const handleMotionPreference = () => {
        const animationPref = getAnimationPreference();
        if (reducedMotion() || animationPref === 'off') {
            stopAutoDrift();
            stopParticles();
            clearPointer();
            return;
        }
        startAutoDrift();
        startParticles();
    };

    document.addEventListener('visibilitychange', () => {
        paused = document.hidden;
        if (paused) {
            stopParticles();
            stopAutoDrift();
        } else {
            if (!reducedMotion()) startAutoDrift();
            startParticles();
        }
    });

    window.addEventListener('resize', () => {
        queueParallaxVars();
        refreshParticles();
    }, { passive: true });
    if (typeof desktopMedia.addEventListener === 'function') desktopMedia.addEventListener('change', refreshParticles);
    if (typeof reduceMotionMedia.addEventListener === 'function') reduceMotionMedia.addEventListener('change', handleMotionPreference);
    window.addEventListener('itr:animations-change', () => {
        handleMotionPreference();
        refreshParticles();
    });

    if (!applyPendingMood()) {
        applyAmbientState({ mood: getAmbientMood(), score: getAmbientScore(), mode: getAmbientMode(), source: 'server' });
    }
    handleMotionPreference();
    queueParallaxVars();
})();
