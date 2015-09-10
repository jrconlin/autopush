"""WebPush Style Autopush Router

This router handles notifications that should be dispatched to an Autopush
node, or stores each individual message, along with its data, in a Message
table for retrieval by the client.

"""
import json
import time
from StringIO import StringIO

from twisted.internet.defer import (
    inlineCallbacks,
    returnValue,
)
from twisted.internet.threads import deferToThread
from twisted.web.client import FileBodyProducer
from cryptography.fernet import InvalidToken

from autopush.protocol import IgnoreBody
from autopush.router.interface import (
    RouterException,
    RouterResponse,
    Notification,
)
from autopush.router.simple import SimpleRouter


class WebPushRouter(SimpleRouter):
    """SimpleRouter subclass to store individual messages appropriately"""

    def delivered_response(self, notification):
        location = "%s/m/%s" % (self.ap_settings.endpoint_url,
                                notification.version)
        return RouterResponse(status_code=201, response_body="",
                              headers={"Location": location})
    stored_response = delivered_response

    def _crypto_headers(self, notification):
        """Creates a dict of the crypto headers for this request."""
        headers = notification.headers
        data = dict(
            encoding=headers["content-encoding"],
            encryption=headers["encryption"],
        )
        # AWS cannot store empty strings, so we only add the encryption-key if
        # its present to avoid empty strings.
        if "encryption-key" in headers:
            data["encryption_key"] = headers["encryption-key"]
        return data

    def _verify_channel(self, result, channel_id):
        if channel_id not in result:
            raise RouterException("No such subscription", status_code=404,
                                  log_exception=False)

    @inlineCallbacks
    def preflight_check(self, uaid, notification):
        """Verifies this routing call can be done successfully"""
        receipt = notification.headers.get("push-receipt", "").strip()
        if receipt:
            try:
                receipt_uaid, chid, receipt_id = yield deferToThread(
                    self.ap_settings.parse_push_receipt, receipt)
            except (InvalidToken, ValueError):
                # The token or endpoint URL is invalid.
                raise self.invalid_endpoint_response()

            if receipt_uaid != uaid or chid != notification.channel_id:
                # The endpoint is valid, but belongs to a different device or
                # channel ID. Pretend we've never heard of it.
                raise self.invalid_endpoint_response()

            # Valid receipt endpoint. Append the receipt ID to the version;
            # this will be used to route the receipt to the correct receipt
            # node once the client acks the message.
            notification = Notification(
                version="%s:%s" % (notification.version, receipt_id),
                data=notification.data,
                channel_id=notification.channel_id,
                headers=notification.headers,
                ttl=notification.ttl,
            )

        result = yield deferToThread(self.ap_settings.message.all_channels,
                                     uaid=uaid)
        if notification.channel_id not in result:
            raise RouterException("No such subscription", status_code=404)

        returnValue(notification)

    def invalid_endpoint_response(self):
        return RouterException("Invalid receipt endpoint", status_code=400)

    def _send_notification(self, uaid, node_id, notification):
        """Send a notification to a specific node_id

        This version of the overriden method includes the necessary crypto
        headers for the notification.

        """
        payload = {"channelID": notification.channel_id,
                   "version": notification.version,
                   "ttl": notification.ttl+int(time.time()),
                   }
        if notification.data:
            payload["headers"] = self._crypto_headers(notification)
            payload["data"] = notification.data
        url = node_id + "/push/" + uaid
        d = self.ap_settings.agent.request(
            "PUT",
            url.encode("utf8"),
            bodyProducer=FileBodyProducer(StringIO(json.dumps(payload))),
        )
        d.addCallback(IgnoreBody.ignore)
        return d

    def _save_notification(self, uaid, notification):
        """Saves a notification, returns a deferred.

        This version of the overridden method saves each individual message
        to the message table along with relevant request headers if
        available.

        """
        if notification.ttl == 0:
            raise RouterException("Finished Routing", status_code=201,
                                  log_exception=False)
        headers = None
        if notification.data:
            headers = self._crypto_headers(notification)
        return deferToThread(
            self.ap_settings.message.store_message,
            uaid=uaid,
            channel_id=notification.channel_id,
            data=notification.data,
            headers=headers,
            message_id=notification.version,
            ttl=notification.ttl+int(time.time()),
        )
