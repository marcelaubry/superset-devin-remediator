from prometheus_client import Counter

webhook_requests_total = Counter(
    "webhook_requests_total",
    "GitHub webhook requests by outcome",
    ["result"],
)
