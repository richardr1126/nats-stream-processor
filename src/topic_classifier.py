import os
import asyncio
from typing import Dict, Optional
import time
import numpy as np

from transformers import pipeline, AutoTokenizer
from optimum.onnxruntime import ORTModelForSequenceClassification

from .config import settings
from .logging_setup import get_logger
from .metrics import (
    topic_predictions_total,
    topic_confidence,
    model_inference_duration_seconds,
    processing_errors_total,
)

logger = get_logger(__name__)


class TopicClassifier:
    """Multi-label topic classifier using tweet-topic-21-multi model with ONNX optimization.
    
    This classifier uses a RoBERTa-based model specifically trained on tweets for 
    multi-label topic classification across 19 predefined topic categories.
    """
    
    def __init__(self):
        self.classifier = None
        self.id2label = None  # Will be populated from model config
        self._model_loaded = False
        
    async def initialize(self) -> None:
        """Load the ONNX model using transformers pipeline."""
        try:
            logger.info("Initializing topic classifier", model=settings.TOPIC_MODEL_NAME)
            
            # Create model cache directory
            os.makedirs(settings.TOPIC_MODEL_CACHE_DIR, exist_ok=True)
            
            # Load the ONNX model using optimum and create pipeline
            logger.info("Loading ONNX model with pipeline")
            
            # Load model in a thread to avoid blocking
            def load_model():
                # Load ONNX model and tokenizer separately
                model = ORTModelForSequenceClassification.from_pretrained(
                    settings.TOPIC_MODEL_NAME,
                    cache_dir=settings.TOPIC_MODEL_CACHE_DIR,
                    file_name="model_quantized.onnx",
                )
                
                tokenizer = AutoTokenizer.from_pretrained(
                    settings.TOPIC_MODEL_NAME,
                    cache_dir=settings.TOPIC_MODEL_CACHE_DIR,
                )
                
                # Store id2label mapping from model config
                id2label = model.config.id2label
                
                # Create text-classification pipeline
                pipe = pipeline(
                    "text-classification",
                    model=model,
                    tokenizer=tokenizer,
                    device=-1,  # CPU inference
                    top_k=None,  # Return all labels with scores
                )
                
                return pipe, id2label
            
            # Run model loading in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            self.classifier, self.id2label = await loop.run_in_executor(None, load_model)
            
            self._model_loaded = True
            logger.info("Topic classifier initialized successfully", 
                       num_labels=len(self.id2label),
                       labels=list(self.id2label.values()))
            
        except Exception as e:
            logger.error("Failed to initialize topic classifier", error=str(e))
            processing_errors_total.labels(error_type="model_init").inc()
            raise
    
    async def classify_topics(self, text: str) -> Optional[Dict]:
        """Classify text into topics using multi-label classification.
        
        The model outputs sigmoid scores for each of the 19 topic categories.
        Topics with scores >= SIGMOID_THRESHOLD are included in the results.
        """
        if not self._model_loaded:
            raise RuntimeError("Topic classifier not initialized")
        
        if not text or not text.strip():
            return None
        
        try:
            start_time = time.time()
            
            # Run inference in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            
            # Run multi-label classification
            def run_classification():
                return self.classifier(text)
            
            # Result is a list of dicts with 'label' and 'score' for all labels
            results = await loop.run_in_executor(None, run_classification)
            
            inference_time = time.time() - start_time
            model_inference_duration_seconds.labels(model="topic").observe(inference_time)
            
            # Process results - filter by sigmoid threshold
            topics = []
            probabilities = {}
            
            # results is a list of label-score pairs for all 19 labels
            for result in results[0] if isinstance(results[0], list) else results:
                label = result['label']
                score = float(result['score'])
                probabilities[label] = score
                
                # Apply sigmoid threshold for multi-label classification
                if score >= settings.TOPIC_SIGMOID_THRESHOLD:
                    topics.append(label)
            
            # Get top topic and confidence
            sorted_results = sorted(probabilities.items(), key=lambda x: x[1], reverse=True)
            top_topic = sorted_results[0][0] if sorted_results else "unknown"
            top_confidence = sorted_results[0][1] if sorted_results else 0.0
            
            # Always include top_topic in topics array, even if below threshold
            # This ensures NATS routing matches what's displayed in UI
            if top_topic != "unknown" and top_topic not in topics:
                topics.append(top_topic)
            
            # Update metrics for each identified topic
            for topic in topics:
                topic_predictions_total.labels(topic=topic).inc()
            
            topic_confidence.observe(top_confidence)
            
            classification_result = {
                "topics": topics,
                "top_topic": top_topic,
                "top_confidence": top_confidence,
            }
            
            logger.debug("Topic classification complete", 
                        topics=topics, 
                        top_confidence=top_confidence)
            
            return classification_result
            
        except Exception as e:
            logger.error("Topic classification failed", 
                        error=str(e), 
                        error_type=type(e).__name__)
            processing_errors_total.labels(error_type="single_analysis").inc()
            raise


# Global topic classifier instance
topic_classifier = TopicClassifier()
