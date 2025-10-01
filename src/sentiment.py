import os
import asyncio
from typing import Dict, List, Optional
import time

from transformers import pipeline, AutoTokenizer
from optimum.onnxruntime import ORTModelForSequenceClassification

from .config import settings
from .logging_setup import get_logger
from .metrics import (
    sentiment_predictions_total,
    sentiment_confidence,
    model_inference_duration_seconds,
    processing_errors_total,
)

logger = get_logger(__name__)


class SentimentAnalyzer:
    """Twitter RoBERTa-based sentiment analyzer using Transformers pipeline with ONNX optimization."""
    
    def __init__(self):
        self.classifier = None
        # Twitter RoBERTa model uses these labels: LABEL_0=negative, LABEL_1=neutral, LABEL_2=positive
        self.label_mapping = {"LABEL_0": "negative", "LABEL_1": "neutral", "LABEL_2": "positive"}
        self._model_loaded = False
        
    async def initialize(self) -> None:
        """Load the ONNX model using transformers pipeline."""
        try:
            logger.info("Initializing sentiment analyzer", model=settings.MODEL_NAME)
            
            # Create model cache directory
            os.makedirs(settings.MODEL_CACHE_DIR, exist_ok=True)
            
            # Load the ONNX model using optimum and create pipeline
            logger.info("Loading ONNX model with pipeline")
            
            # Load model in a thread to avoid blocking
            def load_model():
                # Load ONNX model and tokenizer separately to ensure we use the ONNX version
                model = ORTModelForSequenceClassification.from_pretrained(
                    settings.MODEL_NAME,
                    cache_dir=settings.MODEL_CACHE_DIR,
                    subfolder="onnx",
                    file_name="model_int8.onnx",
                )
                
                tokenizer = AutoTokenizer.from_pretrained(
                    settings.MODEL_NAME,
                    cache_dir=settings.MODEL_CACHE_DIR,
                )
                
                # Fix the model config to have the correct label mappings
                # Twitter RoBERTa sentiment model should have these labels
                model.config.id2label = {0: "LABEL_0", 1: "LABEL_1", 2: "LABEL_2"}
                model.config.label2id = {"LABEL_0": 0, "LABEL_1": 1, "LABEL_2": 2}
                
                return pipeline(
                    "text-classification",
                    model=model,
                    tokenizer=tokenizer,
                    device=-1,  # CPU inference
                    top_k=None,  # Get probabilities for all classes (replaces deprecated return_all_scores)
                    truncation=True,
                    max_length=settings.MAX_SEQUENCE_LENGTH,
                )
            
            # Run model loading in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            self.classifier = await loop.run_in_executor(None, load_model)
            
            self._model_loaded = True
            logger.info("Sentiment analyzer initialized successfully")
            
        except Exception as e:
            logger.error("Failed to initialize sentiment analyzer", error=str(e))
            processing_errors_total.labels(error_type="model_init").inc()
            raise
    
    async def analyze_sentiment(self, text: str) -> Optional[Dict]:
        """Analyze sentiment for a single text."""
        if not self._model_loaded:
            raise RuntimeError("Sentiment analyzer not initialized")
        
        if not text or not text.strip():
            return None
        
        try:
            start_time = time.time()
            
            # Run inference in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            raw_results = await loop.run_in_executor(None, self.classifier, [text])
            
            inference_time = time.time() - start_time
            model_inference_duration_seconds.observe(inference_time)
            
            # Process result - the classifier returns a list for batch input
            # Since we passed [text], we get back a list with one element
            if isinstance(raw_results, list) and len(raw_results) > 0:
                text_results = raw_results[0]  # Get the results for our single text
            else:
                raise ValueError(f"Unexpected classifier output format: {type(raw_results)}")
            
            # Find the prediction with highest score
            best_prediction = max(text_results, key=lambda x: x['score'])
            sentiment = self.label_mapping.get(best_prediction['label'], best_prediction['label'])
            confidence = best_prediction['score']
            confidence = best_prediction['score']
            
            # Create probabilities dict for all classes
            probabilities = {}
            for pred in text_results:
                label_name = self.label_mapping.get(pred['label'], pred['label'])
                probabilities[label_name] = pred['score']
            
            # Ensure all three classes are present
            for class_name in ['negative', 'neutral', 'positive']:
                if class_name not in probabilities:
                    probabilities[class_name] = 0.0
            
            # Update metrics
            sentiment_predictions_total.labels(sentiment=sentiment).inc()
            sentiment_confidence.observe(confidence)
            
            result = {
                "text": text[:100] + "..." if len(text) > 100 else text,  # Truncate for logging
                "sentiment": sentiment,
                "confidence": confidence,
                "probabilities": probabilities
            }
            
            # Only return high-confidence predictions
            if confidence >= settings.CONFIDENCE_THRESHOLD:
                return result
            else:
                logger.debug("Low confidence prediction filtered out", 
                            confidence=confidence, threshold=settings.CONFIDENCE_THRESHOLD)
                return None
            
        except Exception as e:
            logger.error("Sentiment analysis failed", 
                        error=str(e), 
                        error_type=type(e).__name__)
            processing_errors_total.labels(error_type="single_analysis").inc()
            raise


# Global sentiment analyzer instance
sentiment_analyzer = SentimentAnalyzer()