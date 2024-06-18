from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import orjson
import sentry_sdk
from slack_sdk.errors import SlackApiError

from sentry import features
from sentry.constants import METRIC_ALERTS_THREAD_DEFAULT, ObjectStatus
from sentry.incidents.charts import build_metric_alert_chart
from sentry.incidents.models.alert_rule import AlertRuleTriggerAction
from sentry.incidents.models.incident import Incident, IncidentStatus
from sentry.integrations.repository import get_default_metric_alert_repository
from sentry.integrations.repository.metric_alert import (
    MetricAlertNotificationMessageRepository,
    NewMetricAlertNotificationMessage,
)
from sentry.integrations.slack.client import SlackClient
from sentry.integrations.slack.message_builder.incidents import SlackIncidentsMessageBuilder
from sentry.integrations.slack.metrics import (
    SLACK_METRIC_ALERT_FAILURE_DATADOG_METRIC,
    SLACK_METRIC_ALERT_SUCCESS_DATADOG_METRIC,
)
from sentry.integrations.slack.sdk_client import SlackSdkClient
from sentry.models.integrations.integration import Integration
from sentry.models.options.organization_option import OrganizationOption
from sentry.services.hybrid_cloud.integration import integration_service
from sentry.shared_integrations.exceptions import ApiError
from sentry.utils import metrics

from . import logger


def send_incident_alert_notification(
    action: AlertRuleTriggerAction,
    incident: Incident,
    metric_value: float,
    new_status: IncidentStatus,
    notification_uuid: str | None = None,
) -> bool:
    # Make sure organization integration is still active:
    result = integration_service.organization_context(
        organization_id=incident.organization_id, integration_id=action.integration_id
    )
    integration = result.integration
    org_integration = result.organization_integration
    if org_integration is None or integration is None or integration.status != ObjectStatus.ACTIVE:
        # Integration removed, but rule is still active.
        return False

    organization = incident.organization
    chart_url = None
    if features.has("organizations:metric-alert-chartcuterie", incident.organization):
        try:
            chart_url = build_metric_alert_chart(
                organization=organization,
                alert_rule=incident.alert_rule,
                selected_incident=incident,
            )
        except Exception as e:
            sentry_sdk.capture_exception(e)

    channel = action.target_identifier
    attachment: Any = SlackIncidentsMessageBuilder(
        action, incident, new_status, metric_value, chart_url, notification_uuid
    ).build()
    text = str(attachment["text"])
    blocks = {"blocks": attachment["blocks"], "color": attachment["color"]}
    attachments = orjson.dumps([blocks]).decode()

    repository: MetricAlertNotificationMessageRepository = get_default_metric_alert_repository()
    parent_notification_message = None
    # Only grab the parent notification message for thread use if the feature is on
    # Otherwise, leave it empty, and it will not create a thread
    if OrganizationOption.objects.get_value(
        organization=organization,
        key="sentry:metric_alerts_thread_flag",
        default=METRIC_ALERTS_THREAD_DEFAULT,
    ):
        try:
            parent_notification_message = repository.get_parent_notification_message(
                alert_rule_id=incident.alert_rule_id,
                incident_id=incident.id,
                trigger_action_id=action.id,
            )
        except Exception:
            # if there's an error trying to grab a parent notification, don't let that error block this flow
            pass

    new_notification_message_object = NewMetricAlertNotificationMessage(
        incident_id=incident.id,
        trigger_action_id=action.id,
    )

    reply_broadcast = False
    thread_ts = None
    # If a parent notification exists for this rule and action, then we can reply in a thread
    if parent_notification_message is not None:
        # Make sure we track that this reply will be in relation to the parent row
        new_notification_message_object.parent_notification_message_id = (
            parent_notification_message.id
        )
        # To reply to a thread, use the specific key in the payload as referenced by the docs
        # https://api.slack.com/methods/chat.postMessage#arg_thread_ts
        thread_ts = parent_notification_message.message_identifier

        # If the incident is critical status, even if it's in a thread, send to main channel
        if incident.status == IncidentStatus.CRITICAL.value:
            reply_broadcast = True

    success = False
    try:
        client = SlackSdkClient(integration_id=integration.id)
        response = client.chat_postMessage(
            attachments=attachments,
            text=text,
            channel=str(channel),
            thread_ts=thread_ts,
            reply_broadcast=reply_broadcast,
            unfurl_links=False,
            unfurl_media=False,
        )
        metrics.incr(SLACK_METRIC_ALERT_SUCCESS_DATADOG_METRIC, sample_rate=1.0)
    except SlackApiError as e:
        # Record the error code and details from the exception
        new_notification_message_object.error_code = e.response.status_code
        new_notification_message_object.error_details = {
            "msg": str(e),
            "data": e.response.data,
            "url": e.response.api_url,
        }

        log_params = {
            "error": str(e),
            "incident_id": incident.id,
            "incident_status": new_status,
            "attachments": attachments,
        }
        logger.info("slack.metric_alert.error", exc_info=True, extra=log_params)
        metrics.incr(
            SLACK_METRIC_ALERT_FAILURE_DATADOG_METRIC,
            sample_rate=1.0,
            tags={"ok": e.response.get("ok", False), "status": e.response.status_code},
        )
    else:
        success = True
        ts = response.get("ts")

        new_notification_message_object.message_identifier = str(ts) if ts is not None else None

    # Save the notification message we just sent with the response id or error we received
    try:
        repository.create_notification_message(data=new_notification_message_object)
    except Exception:
        # If we had an unexpected error with saving a record to our datastore,
        # do not let the error bubble up, nor block this flow from finishing
        pass

    return success


def send_slack_response(
    integration: Integration, text: str, params: Mapping[str, str], command: str
) -> None:
    payload = {
        "replace_original": False,
        "response_type": "ephemeral",
        "text": text,
    }

    client = SlackClient(integration_id=integration.id)
    logger.info(
        "slack.send_slack_response",
        extra={
            "integration_id": integration.id,
            "response_url": params.get("response_url"),
            "payload": payload,
        },
    )

    if params["response_url"]:
        path = params["response_url"]

    else:
        # Command has been invoked in a DM, not as a slash command
        # we do not have a response URL in this case
        payload["channel"] = params["slack_id"]
        path = "/chat.postMessage"

    try:
        client.post(path, data=payload, json=True)
    except ApiError as e:
        message = str(e)
        # If the user took their time to link their slack account, we may no
        # longer be able to respond, and we're not guaranteed able to post into
        # the channel. Ignore Expired url errors.
        #
        # XXX(epurkhiser): Yes the error string has a space in it.
        if message != "Expired url":
            log_message = (
                "slack.link-notify.response-error"
                if command == "link"
                else "slack.unlink-notify.response-error"
            )
            logger.error(log_message, extra={"error": message})
