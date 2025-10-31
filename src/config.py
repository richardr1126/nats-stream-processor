import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # NATS
    NATS_URL: str = os.getenv("NATS_URL", "nats://nats.nats.svc.cluster.local:4222")
    INPUT_STREAM: str = os.getenv("INPUT_STREAM", "bluesky-posts")
    OUTPUT_STREAM: str = os.getenv("OUTPUT_STREAM", "bluesky-posts-enriched")
    INPUT_SUBJECT: str = os.getenv("INPUT_SUBJECT", "bluesky.posts")
    OUTPUT_SUBJECT: str = os.getenv("OUTPUT_SUBJECT", "bluesky.enriched")
    CONSUMER_NAME: str = os.getenv("CONSUMER_NAME", "unified-processor")
    # Queue group for load-balanced consumption across replicas. Defaults to CONSUMER_NAME
    QUEUE_GROUP: str = os.getenv("QUEUE_GROUP", os.getenv("CONSUMER_NAME", "unified-processor"))
    NUM_STREAM_REPLICAS: int = int(os.getenv("NUM_STREAM_REPLICAS", 1))

    # Service
    SERVICE_NAME: str = os.getenv("SERVICE_NAME", "nats-stream-processor")

    # Consumer tuning
    ACK_WAIT_SECONDS: int = int(os.getenv("ACK_WAIT_SECONDS", 30))  # How long JetStream waits before redelivery
    MAX_DELIVER: int = int(os.getenv("MAX_DELIVER", 3))  # Max redeliver attempts
    MAX_ACK_PENDING: int = int(os.getenv("MAX_ACK_PENDING", 100))  # Max unacked messages in-flight per consumer
    
    # Output stream de-duplication window (in seconds)
    DUPLICATE_WINDOW_SECONDS: int = int(os.getenv("DUPLICATE_WINDOW_SECONDS", 600))  # 10 minutes

    # Sentiment Model
    SENTIMENT_MODEL_NAME: str = os.getenv("SENTIMENT_MODEL_NAME", "onnx-community/twitter-roberta-base-sentiment-ONNX")
    SENTIMENT_MODEL_CACHE_DIR: str = os.getenv("SENTIMENT_MODEL_CACHE_DIR", "/var/cache/models/sentiment")
    SENTIMENT_MAX_SEQUENCE_LENGTH: int = int(os.getenv("SENTIMENT_MAX_SEQUENCE_LENGTH", 512))
    SENTIMENT_CONFIDENCE_THRESHOLD: float = float(os.getenv("SENTIMENT_CONFIDENCE_THRESHOLD", 0.4))
    
    # Topic Classification Model
    TOPIC_MODEL_NAME: str = os.getenv("TOPIC_MODEL_NAME", "richardr1126/tweet-topic-21-multi-ONNX")
    TOPIC_MODEL_CACHE_DIR: str = os.getenv("TOPIC_MODEL_CACHE_DIR", "/var/cache/models/topics")
    TOPIC_MAX_SEQUENCE_LENGTH: int = int(os.getenv("TOPIC_MAX_SEQUENCE_LENGTH", 512))
    # Sigmoid threshold for multi-label classification (predictions >= threshold are included)
    TOPIC_SIGMOID_THRESHOLD: float = float(os.getenv("TOPIC_SIGMOID_THRESHOLD", 0.5))

    # Performance
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", 3))
    RETRY_DELAY: float = float(os.getenv("RETRY_DELAY", 1.0))

    # Health/metrics
    HEALTH_CHECK_PORT: int = int(os.getenv("HEALTH_CHECK_PORT", 8080))
    METRICS_ENABLED: bool = os.getenv("METRICS_ENABLED", "true").lower() == "true"

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT: str = os.getenv("LOG_FORMAT", "json")


settings = Settings()