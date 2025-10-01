from prometheus_client import Counter, Gauge, Histogram


# Processing metrics
posts_processed_total = Counter(
    "stream_processor_posts_processed_total",
    "Total posts processed for sentiment analysis",
)

posts_published_total = Counter(
    "stream_processor_posts_published_total",
    "Posts successfully published with sentiment",
)

processing_errors_total = Counter(
    "stream_processor_errors_total",
    "Total processing errors",
    ["error_type"],
)

# Sentiment classification metrics
sentiment_predictions_total = Counter(
    "stream_processor_sentiment_predictions_total",
    "Total sentiment predictions made",
    ["sentiment"],
)

sentiment_confidence = Histogram(
    "stream_processor_sentiment_confidence",
    "Sentiment prediction confidence scores",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)

# Performance metrics
processing_duration_seconds = Histogram(
    "stream_processor_processing_duration_seconds",
    "Time taken to process individual posts",
    buckets=[0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0],
)

model_inference_duration_seconds = Histogram(
    "stream_processor_model_inference_duration_seconds",
    "Time taken for model inference",
    buckets=[0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0],
)

# Connection status
nats_connected = Gauge(
    "stream_processor_nats_connected",
    "NATS connection status (1=connected, 0=disconnected)",
)

# Queue metrics
message_queue_size = Gauge(
    "stream_processor_message_queue_size",
    "Current message queue size",
)