from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from .models import MpWebhookEvent


class MpWebhookTests(TestCase):
    def test_requires_post(self):
        response = self.client.get(reverse('payments:mp_webhook'))
        self.assertEqual(response.status_code, 405)

    def test_idempotent_processed_event(self):
        MpWebhookEvent.objects.create(topic='preapproval', mp_id='123', status='processed')
        with patch('payments.views.fetch_preapproval') as mock_fetch:
            response = self.client.post(
                reverse('payments:mp_webhook') + '?topic=preapproval&id=123',
                data='{}',
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 200)
        mock_fetch.assert_not_called()
        self.assertEqual(
            MpWebhookEvent.objects.filter(topic='preapproval', mp_id='123').count(),
            1,
        )
