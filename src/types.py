"""Type definitions for the stream processor service."""

from typing import TypedDict, Optional


class SentimentProbabilities(TypedDict):
    """Sentiment probability scores."""
    positive: float
    negative: float
    neutral: float


class SentimentData(TypedDict):
    """Sentiment analysis result."""
    sentiment: str  # "positive", "negative", or "neutral"
    confidence: float
    probabilities: SentimentProbabilities


class RawPost(TypedDict):
    """Raw post data structure from input stream."""
    uri: str
    cid: str
    author: str  # DID of the author
    text: str
    created_at: str  # ISO 8601 timestamp


class EnrichedPost(RawPost):
    """Enriched post with sentiment analysis results."""
    sentiment: SentimentData
    processed_at: float
    processor: str
