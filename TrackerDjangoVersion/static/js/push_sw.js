self.addEventListener('push', (event) => {
    let payload = { title: 'Notificacao', body: '', url: '' };
    if (event.data) {
        try {
            payload = event.data.json();
        } catch (err) {
            payload.body = event.data.text();
        }
    }
    const options = {
        body: payload.body || '',
        data: { url: payload.url || '' },
    };
    event.waitUntil(self.registration.showNotification(payload.title || 'Notificacao', options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const targetUrl = event.notification.data && event.notification.data.url;
    if (!targetUrl) return;
    event.waitUntil(clients.openWindow(targetUrl));
});
