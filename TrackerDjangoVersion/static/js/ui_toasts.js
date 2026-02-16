(() => {
    const initToasts = () => {
        if (!window.bootstrap || !bootstrap.Toast) return;
        const toasts = document.querySelectorAll('[data-toast="1"]');
        toasts.forEach((el) => {
            if (el.dataset.toastReady === '1') return;
            const timeout = parseInt(el.dataset.toastTimeout || '4000', 10);
            const toast = new bootstrap.Toast(el, { delay: timeout, autohide: true });
            toast.show();
            el.dataset.toastReady = '1';
        });
    };

    document.addEventListener('DOMContentLoaded', initToasts);
})();
