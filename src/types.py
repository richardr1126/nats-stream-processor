"""Type definitions for the stream processor service."""

from typing import TypedDict, Optional, Dict, List


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


class TopicData(TypedDict):
    """Topic classification result."""
    topics: List[str]  # List of identified topics
    probabilities: Dict[str, float]  # All topic probabilities
    top_topic: str  # Topic with highest confidence
    top_confidence: float


class RawPost(TypedDict):
    """Raw post data structure from input stream."""
    uri: str
    cid: str
    author: str  # DID of the author
    text: str
    created_at: str  # ISO 8601 timestamp


class EnrichedPost(RawPost):
    """Enriched post with both sentiment and topic analysis results."""
    sentiment: SentimentData
    topics: TopicData
    processed_at: float
    processor: str
