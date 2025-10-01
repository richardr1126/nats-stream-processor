import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # NATS
    NATS_URL: str = os.getenv("NATS_URL", "nats://nats.nats.svc.cluster.local:4222")
    INPUT_STREAM: str = os.getenv("INPUT_STREAM", "bluesky-posts")
    OUTPUT_STREAM: str = os.getenv("OUTPUT_STREAM", "bluesky-posts-sentiment")
    INPUT_SUBJECT: str = os.getenv("INPUT_SUBJECT", "bluesky.posts")
    OUTPUT_SUBJECT: str = os.getenv("OUTPUT_SUBJECT", "bluesky.posts.sentiment")
    CONSUMER_NAME: str = os.getenv("CONSUMER_NAME", "sentiment-processor")
    # Queue group for load-balanced consumption across replicas. Defaults to CONSUMER_NAME
    QUEUE_GROUP: str = os.getenv("QUEUE_GROUP", os.getenv("CONSUMER_NAME", "sentiment-processor"))
    NUM_STREAM_REPLICAS: int = int(os.getenv("NUM_STREAM_REPLICAS", 1))

    # Service
    SERVICE_NAME: str = os.getenv("SERVICE_NAME", "nats-stream-processor")
    PROCESSING_TIMEOUT: int = int(os.getenv("PROCESSING_TIMEOUT", 30))

    # Sentiment Model
    MODEL_NAME: str = os.getenv("MODEL_NAME", "onnx-community/twitter-roberta-base-sentiment-ONNX")
    MODEL_CACHE_DIR: str = os.getenv("MODEL_CACHE_DIR", "/var/cache/models")
    MAX_SEQUENCE_LENGTH: int = int(os.getenv("MAX_SEQUENCE_LENGTH", 512))
    CONFIDENCE_THRESHOLD: float = float(os.getenv("CONFIDENCE_THRESHOLD", 0.4))

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