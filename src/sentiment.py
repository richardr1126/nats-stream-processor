import os
import asyncio
from typing import Dict, Optional
import time
import numpy as np

from transformers import AutoTokenizer
import onnxruntime as ort

from .config import settings
from .logging_setup import get_logger
from .metrics import (
    sentiment_predictions_total,
    sentiment_confidence,
    model_inference_duration_seconds,
    processing_errors_total,
)

logger = get_logger(__name__)


def _softmax(logits: np.ndarray) -> np.ndarray:
    m = np.max(logits)
    exps = np.exp(logits - m)
    return exps / exps.sum()


class SentimentAnalyzer:
    """Twitter RoBERTa-based sentiment analyzer using pure ONNX Runtime inference."""
    
    def __init__(self):
        self.session = None
        self.tokenizer = None
        # Twitter RoBERTa model uses these labels: LABEL_0=negative, LABEL_1=neutral, LABEL_2=positive
        self.label_mapping = {0: "negative", 1: "neutral", 2: "positive"}
        self._model_loaded = False
        
    async def initialize(self) -> None:
        """Load the ONNX model and tokenizer."""
        try:
            logger.info("Initializing sentiment analyzer", model=settings.SENTIMENT_MODEL_NAME)
            
            # Create model cache directory
            os.makedirs(settings.SENTIMENT_MODEL_CACHE_DIR, exist_ok=True)
            
            # Load model in a thread to avoid blocking
            def load_model():
                # Load tokenizer
                tokenizer = AutoTokenizer.from_pretrained(
                    settings.SENTIMENT_MODEL_NAME,
                    cache_dir=settings.SENTIMENT_MODEL_CACHE_DIR,
                )
                
                # Download model files to cache
                from huggingface_hub import hf_hub_download
                model_path = hf_hub_download(
                    repo_id=settings.SENTIMENT_MODEL_NAME,
                    filename="onnx/model_int8.onnx",
                    cache_dir=settings.SENTIMENT_MODEL_CACHE_DIR,
                )
                
                # Create ONNX Runtime session with CPU execution provider
                sess_options = ort.SessionOptions()
                sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                # Use all available CPU threads for better performance
                # intra_op_num_threads controls parallelism within ops (default uses all cores)
                sess_options.intra_op_num_threads = 0  # 0 = use all available
                # Allow parallel execution across ops if runtime decides
                sess_options.inter_op_num_threads = 0
                
                session = ort.InferenceSession(
                    model_path,
                    sess_options=sess_options,
                    providers=['CPUExecutionProvider']
                )
                
                return session, tokenizer
            
            # Run model loading in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            self.session, self.tokenizer = await loop.run_in_executor(None, load_model)
            
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
            
            # Run inference directly (ONNX Runtime releases the GIL for heavy ops)
            result = self._run_inference(text)
            
            inference_time = time.time() - start_time
            model_inference_duration_seconds.labels(model="sentiment").observe(inference_time)
            
            # Only return high-confidence predictions
            if result["confidence"] >= settings.SENTIMENT_CONFIDENCE_THRESHOLD:
                return result
            else:
                logger.debug("Low confidence prediction filtered out", 
                            confidence=result["confidence"], 
                            threshold=settings.SENTIMENT_CONFIDENCE_THRESHOLD)
                return None
            
        except Exception as e:
            logger.error("Sentiment analysis failed", 
                        error=str(e), 
                        error_type=type(e).__name__)
            processing_errors_total.labels(error_type="single_analysis").inc()
            raise
    
    def _run_inference(self, text: str) -> Dict:
        """Run ONNX inference on text."""
        # Tokenize input
        inputs = self.tokenizer(
            text,
            truncation=True,
            max_length=settings.SENTIMENT_MAX_SEQUENCE_LENGTH,
            padding=False,
            return_tensors="np"
        )
        
        # Prepare ONNX inputs
        input_ids = inputs["input_ids"]
        attn = inputs["attention_mask"]
        if input_ids.dtype != np.int64:
            input_ids = input_ids.astype(np.int64, copy=False)
        if attn.dtype != np.int64:
            attn = attn.astype(np.int64, copy=False)

        onnx_inputs = {
            "input_ids": input_ids,
            "attention_mask": attn,
        }
        
        # Run inference
        outputs = self.session.run(None, onnx_inputs)
        logits = outputs[0][0]  # Shape: [num_labels]
        
        # Apply softmax to get probabilities
        probabilities_array = _softmax(logits)
        
        # Get prediction
        predicted_class = int(np.argmax(probabilities_array))
        confidence = float(probabilities_array[predicted_class])
        sentiment = self.label_mapping[predicted_class]
        
        # Create probabilities dict for all classes
        probabilities = {
            self.label_mapping[i]: float(probabilities_array[i])
            for i in range(len(probabilities_array))
        }
        
        # Update metrics
        sentiment_predictions_total.labels(sentiment=sentiment).inc()
        sentiment_confidence.observe(confidence)
        
        return {
            "sentiment": sentiment,
            "confidence": confidence,
            "probabilities": probabilities
        }


# Global sentiment analyzer instance
sentiment_analyzer = SentimentAnalyzer()