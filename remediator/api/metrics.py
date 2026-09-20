from prometheus_client import Counter

webhook_requests_total = Counter(
    "webhook_requests_total",
    "GitHub webhook requests by outcome",
    ["result"],
)

slack_action_requests_total = Counter(
    "slack_action_requests_total",
    "Slack interaction requests by outcome",
    ["result"],
)

outbox_deliveries_total = Counter(
    "outbox_deliveries_total",
    "Outbox dispatch attempts by channel and outcome",
    ["channel", "result"],
)

worker_transient_db_errors_total = Counter(
    "worker_transient_db_errors_total",
    "Worker jobs released for retry after a deadlock, serialization or connection error",
)
