import decimal
import json

import six

from django.dispatch import Signal
from django.test import TestCase
from django.test.client import Client

try:
    from django.urls import reverse
except ImportError:
    from django.core.urlresolvers import reverse

import stripe

from mock import patch

from . import TRANSFER_CREATED_TEST_DATA, TRANSFER_PENDING_TEST_DATA
from ..models import Event, Transfer, EventProcessingException, Customer
from ..webhooks import registry, AccountApplicationDeauthorizeWebhook, ChargeCapturedWebhook, CustomerUpdatedWebhook, CustomerSourceCreatedWebhook, CustomerSourceDeletedWebhook, CustomerSubscriptionCreatedWebhook, InvoiceCreatedWebhook


class WebhookRegistryTest(TestCase):

    def test_get_signal(self):
        signal = registry.get_signal("account.updated")
        self.assertTrue(isinstance(signal, Signal))

    def test_get_signal_keyerror(self):
        self.assertIsNone(registry.get_signal("not a webhook"))


class WebhookTests(TestCase):

    event_data = {
        "created": 1348360173,
        "data": {
            "object": {
                "amount": 455,
                "currency": "usd",
                "date": 1348876800,
                "description": None,
                "id": "ach_XXXXXXXXXXXX",
                "object": "transfer",
                "other_transfers": [],
                "status": "pending",
                "livemode": True,
                "reversed": False,
                "summary": {
                    "adjustment_count": 0,
                    "adjustment_fee_details": [],
                    "adjustment_fees": 0,
                    "adjustment_gross": 0,
                    "charge_count": 1,
                    "charge_fee_details": [{
                        "amount": 45,
                        "application": None,
                        "currency": "usd",
                        "description": None,
                        "type": "stripe_fee"
                    }],
                    "charge_fees": 45,
                    "charge_gross": 500,
                    "collected_fee_count": 0,
                    "collected_fee_gross": 0,
                    "currency": "usd",
                    "net": 455,
                    "refund_count": 0,
                    "refund_fees": 0,
                    "refund_gross": 0,
                    "validation_count": 0,
                    "validation_fees": 0
                }
            }
        },
        "id": "evt_XXXXXXXXXXXXx",
        "livemode": True,
        "object": "event",
        "pending_webhooks": 1,
        "type": "transfer.created"
    }

    @patch("stripe.Event.retrieve")
    @patch("stripe.Transfer.retrieve")
    def test_webhook_with_transfer_event(self, TransferMock, StripeEventMock):
        StripeEventMock.return_value.to_dict.return_value = self.event_data
        TransferMock.return_value = self.event_data['data']['object']
        msg = json.dumps(self.event_data)
        resp = Client().post(
            reverse("pinax_stripe_webhook"),
            six.u(msg),
            content_type="application/json"
        )
        self.assertEquals(resp.status_code, 200)
        self.assertTrue(Event.objects.filter(kind="transfer.created").exists())

    @patch("stripe.Event.retrieve")
    @patch("stripe.Transfer.retrieve")
    def test_webhook_associated_with_stripe_account(self, TransferMock, StripeEventMock):
        connect_event_data = self.event_data.copy()
        stripe_account = 'acct_123123123'
        # only difference is that we'll have a user_id value
        connect_event_data['user_id'] = stripe_account
        StripeEventMock.return_value.to_dict.return_value = connect_event_data
        TransferMock.return_value = connect_event_data['data']['object']
        msg = json.dumps(connect_event_data)
        resp = Client().post(
            reverse("pinax_stripe_webhook"),
            six.u(msg),
            content_type="application/json"
        )
        self.assertEquals(resp.status_code, 200)
        self.assertTrue(Event.objects.filter(kind="transfer.created").exists())
        self.assertEqual(
            Event.objects.filter(kind="transfer.created").first().stripe_account,
            stripe_account
        )

    def test_webhook_duplicate_event(self):
        data = {"id": 123}
        Event.objects.create(stripe_id=123, livemode=True)
        msg = json.dumps(data)
        resp = Client().post(
            reverse("pinax_stripe_webhook"),
            six.u(msg),
            content_type="application/json"
        )
        self.assertEquals(resp.status_code, 200)
        self.assertTrue(EventProcessingException.objects.filter(message="Duplicate event record").exists())

    def test_webhook_event_mismatch(self):
        event = Event(kind="account.updated")
        WH = registry.get("account.application.deauthorized")
        with self.assertRaises(Exception):
            WH(event)

    @patch("django.dispatch.Signal.send")
    def test_send_signal(self, SignalSendMock):
        event = Event(kind="account.application.deauthorized")
        WH = registry.get("account.application.deauthorized")
        WH(event).send_signal()
        self.assertTrue(SignalSendMock.called)

    def test_send_signal_not_sent(self):
        event = Event(kind="account.application.deauthorized")
        WH = registry.get("account.application.deauthorized")

        def signal_handler(sender, *args, **kwargs):
            self.fail("Should not have been called.")
        registry.get_signal("account.application.deauthorized").connect(signal_handler)
        webhook = WH(event)
        webhook.name = "mismatch name"  # Not sure how this ever happens due to the registry
        webhook.send_signal()

    @patch("pinax.stripe.actions.customers.link_customer")
    @patch("pinax.stripe.webhooks.Webhook.validate")
    @patch("pinax.stripe.webhooks.Webhook.process_webhook")
    def test_process_exception_is_logged(self, ProcessWebhookMock, ValidateMock, LinkMock):
        # note: we choose an event type for which we do no processing
        event = Event.objects.create(kind="account.application.deauthorized", webhook_message={}, valid=True, processed=False)
        ProcessWebhookMock.side_effect = stripe.StripeError("Message", "error")
        with self.assertRaises(stripe.StripeError):
            AccountApplicationDeauthorizeWebhook(event).process()
        self.assertTrue(EventProcessingException.objects.filter(event=event).exists())

    @patch("pinax.stripe.actions.customers.link_customer")
    @patch("pinax.stripe.webhooks.Webhook.validate")
    def test_process_return_none(self, ValidateMock, LinkMock):
        # note: we choose an event type for which we do no processing
        event = Event.objects.create(kind="account.application.deauthorized", webhook_message={}, valid=True, processed=False)
        self.assertIsNone(AccountApplicationDeauthorizeWebhook(event).process())


class ChargeWebhookTest(TestCase):

    @patch("stripe.Charge.retrieve")
    @patch("pinax.stripe.actions.charges.sync_charge_from_stripe_data")
    def test_process_webhook(self, SyncMock, RetrieveMock):
        event = Event.objects.create(kind=ChargeCapturedWebhook.name, webhook_message={}, valid=True, processed=False)
        event.validated_message = dict(data=dict(object=dict(id=1)))
        ChargeCapturedWebhook(event).process_webhook()
        self.assertTrue(SyncMock.called)


class CustomerUpdatedWebhookTest(TestCase):

    @patch("pinax.stripe.actions.customers.sync_customer")
    def test_process_webhook(self, SyncMock):
        event = Event.objects.create(kind=CustomerUpdatedWebhook.name, webhook_message={}, valid=True, processed=False)
        CustomerUpdatedWebhook(event).process_webhook()
        self.assertTrue(SyncMock.called)


class CustomerSourceCreatedWebhookTest(TestCase):

    @patch("pinax.stripe.actions.sources.sync_payment_source_from_stripe_data")
    def test_process_webhook(self, SyncMock):
        event = Event.objects.create(kind=CustomerSourceCreatedWebhook.name, webhook_message={}, valid=True, processed=False)
        event.validated_message = dict(data=dict(object=dict()))
        CustomerSourceCreatedWebhook(event).process_webhook()
        self.assertTrue(SyncMock.called)


class CustomerSourceDeletedWebhookTest(TestCase):

    @patch("pinax.stripe.actions.sources.delete_card_object")
    def test_process_webhook(self, SyncMock):
        event = Event.objects.create(kind=CustomerSourceDeletedWebhook.name, webhook_message={}, valid=True, processed=False)
        event.validated_message = dict(data=dict(object=dict(id=1)))
        CustomerSourceDeletedWebhook(event).process_webhook()
        self.assertTrue(SyncMock.called)


class CustomerSubscriptionCreatedWebhookTest(TestCase):

    @patch("stripe.Customer.retrieve")
    @patch("pinax.stripe.actions.customers.sync_customer")
    def test_process_webhook(self, SyncMock, RetrieveMock):
        event = Event.objects.create(kind=CustomerSubscriptionCreatedWebhook.name, customer=Customer.objects.create(), webhook_message={}, valid=True, processed=False)
        CustomerSubscriptionCreatedWebhook(event).process_webhook()
        self.assertTrue(SyncMock.called)

    @patch("pinax.stripe.actions.customers.sync_customer")
    def test_process_webhook_no_customer(self, SyncMock):
        event = Event.objects.create(kind=CustomerSubscriptionCreatedWebhook.name, webhook_message={}, valid=True, processed=False)
        CustomerSubscriptionCreatedWebhook(event).process_webhook()
        self.assertFalse(SyncMock.called)


class InvoiceCreatedWebhookTest(TestCase):

    @patch("pinax.stripe.actions.invoices.sync_invoice_from_stripe_data")
    def test_process_webhook(self, SyncMock):
        event = Event.objects.create(kind=InvoiceCreatedWebhook.name, webhook_message={}, valid=True, processed=False)
        event.validated_message = dict(data=dict(object=dict(id=1)))
        InvoiceCreatedWebhook(event).process_webhook()
        self.assertTrue(SyncMock.called)


class TestTransferWebhooks(TestCase):

    @patch("stripe.Event.retrieve")
    @patch("stripe.Transfer.retrieve")
    def test_transfer_created(self, TransferMock, EventMock):
        ev = EventMock()
        ev.to_dict.return_value = TRANSFER_CREATED_TEST_DATA
        TransferMock.return_value = TRANSFER_CREATED_TEST_DATA['data']['object']
        event = Event.objects.create(
            stripe_id=TRANSFER_CREATED_TEST_DATA["id"],
            kind="transfer.created",
            livemode=True,
            webhook_message=TRANSFER_CREATED_TEST_DATA,
            validated_message=TRANSFER_CREATED_TEST_DATA,
            valid=True
        )
        registry.get(event.kind)(event).process()
        transfer = Transfer.objects.get(stripe_id="tr_XXXXXXXXXXXX")
        self.assertEquals(transfer.amount, decimal.Decimal("4.55"))
        self.assertEquals(transfer.status, "paid")

    @patch("stripe.Event.retrieve")
    @patch("stripe.Transfer.retrieve")
    def test_transfer_pending_create(self, TransferMock, EventMock):
        ev = EventMock()
        ev.to_dict.return_value = TRANSFER_PENDING_TEST_DATA
        TransferMock.return_value = TRANSFER_PENDING_TEST_DATA['data']['object']
        event = Event.objects.create(
            stripe_id=TRANSFER_PENDING_TEST_DATA["id"],
            kind="transfer.created",
            livemode=True,
            webhook_message=TRANSFER_PENDING_TEST_DATA,
            validated_message=TRANSFER_PENDING_TEST_DATA,
            valid=True
        )
        registry.get(event.kind)(event).process()
        transfer = Transfer.objects.get(stripe_id="tr_adlkj2l3kj23")
        self.assertEquals(transfer.amount, decimal.Decimal("9.41"))
        self.assertEquals(transfer.status, "pending")

    @patch("stripe.Event.retrieve")
    @patch("stripe.Transfer.retrieve")
    def test_transfer_paid_updates_existing_record(self, TransferMock, EventMock):
        ev = EventMock()
        ev.to_dict.return_value = TRANSFER_CREATED_TEST_DATA
        TransferMock.return_value = TRANSFER_CREATED_TEST_DATA['data']['object']
        event = Event.objects.create(
            stripe_id=TRANSFER_CREATED_TEST_DATA["id"],
            kind="transfer.created",
            livemode=True,
            webhook_message=TRANSFER_CREATED_TEST_DATA,
            validated_message=TRANSFER_CREATED_TEST_DATA,
            valid=True
        )
        registry.get(event.kind)(event).process()
        data = {
            "created": 1364658818,
            "data": {
                "object": {
                    "account": {
                        "bank_name": "BANK OF AMERICA, N.A.",
                        "country": "US",
                        "last4": "9999",
                        "object": "bank_account"
                    },
                    "amount": 455,
                    "currency": "usd",
                    "date": 1364601600,
                    "description": "STRIPE TRANSFER",
                    "fee": 0,
                    "fee_details": [],
                    "id": "tr_XXXXXXXXXXXX",
                    "livemode": True,
                    "object": "transfer",
                    "other_transfers": [],
                    "status": "paid",
                    "summary": {
                        "adjustment_count": 0,
                        "adjustment_fee_details": [],
                        "adjustment_fees": 0,
                        "adjustment_gross": 0,
                        "charge_count": 1,
                        "charge_fee_details": [{
                            "amount": 45,
                            "application": None,
                            "currency": "usd",
                            "description": None,
                            "type": "stripe_fee"
                        }],
                        "charge_fees": 45,
                        "charge_gross": 500,
                        "collected_fee_count": 0,
                        "collected_fee_gross": 0,
                        "collected_fee_refund_count": 0,
                        "collected_fee_refund_gross": 0,
                        "currency": "usd",
                        "net": 455,
                        "refund_count": 0,
                        "refund_fee_details": [],
                        "refund_fees": 0,
                        "refund_gross": 0,
                        "validation_count": 0,
                        "validation_fees": 0
                    },
                    "transactions": {
                        "count": 1,
                        "data": [{
                            "amount": 500,
                            "created": 1364064631,
                            "description": None,
                            "fee": 45,
                            "fee_details": [{
                                "amount": 45,
                                "application": None,
                                "currency": "usd",
                                "description": "Stripe processing fees",
                                "type": "stripe_fee"
                            }],
                            "id": "ch_XXXXXXXXXX",
                            "net": 455,
                            "type": "charge"
                        }],
                        "object": "list",
                        "url": "/v1/transfers/XX/transactions"
                    }
                }
            },
            "id": "evt_YYYYYYYYYYYY",
            "livemode": True,
            "object": "event",
            "pending_webhooks": 1,
            "type": "transfer.paid"
        }
        paid_event = Event.objects.create(
            stripe_id=data["id"],
            kind="transfer.paid",
            livemode=True,
            webhook_message=data,
            validated_message=data,
            valid=True
        )
        registry.get(paid_event.kind)(paid_event).process()
        transfer = Transfer.objects.get(stripe_id="tr_XXXXXXXXXXXX")
        self.assertEquals(transfer.status, "paid")
