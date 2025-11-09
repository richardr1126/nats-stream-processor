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
    topic_predictions_total,
    topic_confidence,
    model_inference_duration_seconds,
    processing_errors_total,
)

logger = get_logger(__name__)


class TopicClassifier:
    """Multi-label topic classifier using tweet-topic-21-multi model with pure ONNX Runtime.
    
    This classifier uses a RoBERTa-based model specifically trained on tweets for 
    multi-label topic classification across 19 predefined topic categories.
    """
    
    def __init__(self):
        self.session = None
        self.tokenizer = None
        self.id2label = None  # Will be populated from model config
        self._model_loaded = False
        
    async def initialize(self) -> None:
        """Load the ONNX model and tokenizer."""
        try:
            logger.info("Initializing topic classifier", model=settings.TOPIC_MODEL_NAME)
            
            # Create model cache directory
            os.makedirs(settings.TOPIC_MODEL_CACHE_DIR, exist_ok=True)
            
            # Load model in a thread to avoid blocking
            def load_model():
                # Load tokenizer
                tokenizer = AutoTokenizer.from_pretrained(
                    settings.TOPIC_MODEL_NAME,
                    cache_dir=settings.TOPIC_MODEL_CACHE_DIR,
                )
                
                # Download model files to cache
                from huggingface_hub import hf_hub_download
                model_path = hf_hub_download(
                    repo_id=settings.TOPIC_MODEL_NAME,
                    filename="model_quantized.onnx",
                    cache_dir=settings.TOPIC_MODEL_CACHE_DIR,
                )
                
                # Load model config to get label mappings
                from transformers import AutoConfig
                config = AutoConfig.from_pretrained(
                    settings.TOPIC_MODEL_NAME,
                    cache_dir=settings.TOPIC_MODEL_CACHE_DIR,
                )
                id2label = config.id2label
                
                # Create ONNX Runtime session with CPU execution provider
                sess_options = ort.SessionOptions()
                sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                # Use all available CPU threads for better performance
                # intra_op_num_threads controls parallelism within ops (default uses all cores)
                sess_options.inter_op_num_threads = 0  # 0 means use all available
                
                session = ort.InferenceSession(
                    model_path,
                    sess_options=sess_options,
                    providers=['CPUExecutionProvider']
                )
                
                return session, tokenizer, id2label
            
            # Run model loading in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            self.session, self.tokenizer, self.id2label = await loop.run_in_executor(None, load_model)
            
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
            result = await loop.run_in_executor(None, self._run_inference, text)
            
            inference_time = time.time() - start_time
            model_inference_duration_seconds.labels(model="topic").observe(inference_time)
            
            return result
            
        except Exception as e:
            logger.error("Topic classification failed", 
                        error=str(e), 
                        error_type=type(e).__name__)
            processing_errors_total.labels(error_type="single_analysis").inc()
            raise
    
    def _run_inference(self, text: str) -> Dict:
        """Run ONNX inference on text (synchronous method for thread pool)."""
        # Tokenize input
        inputs = self.tokenizer(
            text,
            truncation=True,
            padding="max_length",
            max_length=512,  # Standard max length for RoBERTa
            return_tensors="np"
        )
        
        # Prepare ONNX inputs
        onnx_inputs = {
            "input_ids": inputs["input_ids"].astype(np.int64),
            "attention_mask": inputs["attention_mask"].astype(np.int64),
        }
        
        # Run inference
        outputs = self.session.run(None, onnx_inputs)
        logits = outputs[0][0]  # Shape: [num_labels]
        
        # Apply sigmoid for multi-label classification
        sigmoid_scores = 1 / (1 + np.exp(-logits))
        
        # Process results - filter by sigmoid threshold
        topics = []
        probabilities = {}
        
        for label_id, score in enumerate(sigmoid_scores):
            label = self.id2label[label_id]
            score_float = float(score)
            probabilities[label] = score_float
            
            # Apply sigmoid threshold for multi-label classification
            if score_float >= settings.TOPIC_SIGMOID_THRESHOLD:
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


# Global topic classifier instance
topic_classifier = TopicClassifier()
