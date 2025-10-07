import asyncio
import signal
import time
from typing import Any, Dict, List
from collections import deque
import random

import uvicorn

from .config import settings
from .logging_setup import get_logger
from .metrics import (
    posts_processed_total,
    posts_published_total,
    processing_duration_seconds,
    message_queue_size,
    sentiment_predictions_total,
)
from .nats_client import StreamProcessorNatsClient
from .sentiment import sentiment_analyzer
from .health import create_health_api

logger = get_logger(__name__)

class StreamProcessorService:
    """Main service that orchestrates the stream processing pipeline."""
    
    def __init__(self):
        logger.info("Initializing stream processor service", service=settings.SERVICE_NAME)
        
        # NATS client for stream processing
        self.nats_client = StreamProcessorNatsClient()
        
        # Web server for health checks
        app = create_health_api()
        config = uvicorn.Config(
            app, 
            host="0.0.0.0", 
            port=settings.HEALTH_CHECK_PORT, 
            log_level="info"
        )
        self.server = uvicorn.Server(config)
        
        # Lifecycle management
        self.stop_event = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self._tasks: List[asyncio.Task] = []

    async def start(self):
        """Start the service components."""
        try:
            logger.info("Starting stream processor service")
            
            # Initialize sentiment analyzer
            logger.info("Initializing sentiment analyzer")
            await sentiment_analyzer.initialize()
            
            # Connect to NATS
            logger.info("Connecting to NATS")
            await self.nats_client.connect()
            
            # Set up signal handlers
            for sig in (signal.SIGINT, signal.SIGTERM):
                self.loop.add_signal_handler(sig, self._handle_signal)
            
            # Start background tasks
            self._tasks = [
                asyncio.create_task(self._run_server()),
                asyncio.create_task(self._periodic_stats_logger()),
            ]
            
            # Start subscribing to posts
            logger.info("Starting subscription to posts")
            await self.nats_client.subscribe_to_posts(self._process_message)
            
            logger.info("Stream processor service started successfully")
            
        except Exception as e:
            logger.error("Failed to start service", error=str(e))
            raise

    def _handle_signal(self):
        """Handle shutdown signals."""
        logger.info("Received shutdown signal")
        self.stop_event.set()

    async def _run_server(self):
        """Run the health check server."""
        try:
            await self.server.serve()
        except Exception as e:
            logger.error("Health server error", error=str(e))

    async def _process_message(self, post_data: Dict[str, Any]):
        """Process a single message for sentiment analysis."""
        start_time = time.time()
        
        try:
            # Count every message we attempt to process
            posts_processed_total.inc()
            logger.debug("Processing message")
            
            # Extract text for sentiment analysis
            text = self._extract_text_from_post(post_data)
            if not text or len(text.strip()) == 0:
                logger.debug("No valid text found in message")
                return
            
            # Perform sentiment analysis
            sentiment_result = await sentiment_analyzer.analyze_sentiment(text)
            
            if sentiment_result:
                # Publish result
                try:
                    await self.nats_client.publish_sentiment_result(
                        post_data, sentiment_result
                    )
                    
                    # Update metrics
                    processing_time = time.time() - start_time
                    processing_duration_seconds.observe(processing_time)
                    
                    logger.debug("Message processed", 
                                sentiment=sentiment_result["sentiment"],
                                confidence=sentiment_result["confidence"])
                    
                except Exception as e:
                    logger.error("Failed to publish sentiment result", 
                               error=str(e), post_uri=post_data.get("uri"))
            else:
                logger.debug("No sentiment result (low confidence)")
            
        except Exception as e:
            logger.error("Error processing message", error=str(e))

    def _extract_text_from_post(self, post: Dict[str, Any]) -> str:
        """Extract text content from a post message."""
        # Handle different post structures
        text = post.get("text")
        if text:
            return text
        
        # Try to extract from record field (if it's structured like ATProto)
        record = post.get("record")
        if isinstance(record, dict):
            text = record.get("text")
            if text:
                return text
        
        # Try other common fields
        content = post.get("content") or post.get("body") or post.get("message")
        if content:
            return content
        
        logger.debug("No text found in post", post_keys=list(post.keys()))
        return ""

    async def _periodic_stats_logger(self):
        """Log periodic statistics."""
        last_processed_count = 0
        last_published_count = 0
        last_pending_count = 0
        
        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(20 + random.uniform(0, 2))  # Log every 20 seconds with slight jitter for replicas

                # Get pending message count
                pending_count = await self.nats_client.get_pending_message_count()
                
                # Calculate processing rate since last log
                current_processed = posts_processed_total._value.get()
                current_published = posts_published_total._value.get()
                
                messages_per_20s = current_processed - last_processed_count
                messages_per_second = messages_per_20s / 20.0
                
                published_per_20s = current_published - last_published_count
                published_per_second = published_per_20s / 20.0
                
                # Calculate backlog change
                backlog_change = pending_count - last_pending_count if last_pending_count > 0 else 0
                
                # Get sentiment distribution
                positive_count = sentiment_predictions_total.labels(sentiment="positive")._value.get()
                negative_count = sentiment_predictions_total.labels(sentiment="negative")._value.get()
                neutral_count = sentiment_predictions_total.labels(sentiment="neutral")._value.get()
                
                # Calculate publish rate (percentage of processed that were published)
                publish_rate = (published_per_20s / messages_per_20s * 100) if messages_per_20s > 0 else 0
                
                # Format stats with multi-line output
                stats_msg = (
                    "\n" + "="*30 +
                    "\n  Stream Processor Statistics" +
                    "\n" + "="*30 +
                    "\n  Processing Rates:" +
                    f"\n    Processed/sec:     {round(messages_per_second, 2)}" +
                    f"\n    Published/sec:     {round(published_per_second, 2)}" +
                    f"\n    Publish rate:      {round(publish_rate, 1)}%" +
                    "\n  Backlog Status:" +
                    f"\n    Pending messages:  {pending_count}" +
                    f"\n    Backlog change:    {backlog_change:+d}" +
                    "\n  Cumulative Totals:" +
                    f"\n    Total processed:   {int(current_processed)}" +
                    f"\n    Total published:   {int(current_published)}" +
                    "\n  Sentiment Distribution:" +
                    f"\n    Positive:          {int(positive_count)}" +
                    f"\n    Negative:          {int(negative_count)}" +
                    f"\n    Neutral:           {int(neutral_count)}" +
                    "\n" + "="*30
                )
                logger.info(stats_msg)
                
                # Update for next iteration
                last_processed_count = current_processed
                last_published_count = current_published
                last_pending_count = pending_count
                
            except Exception as e:
                logger.warning("Failed to log stats", error=str(e))

    async def run(self):
        """Start the service and wait for shutdown signal."""
        try:
            await self.start()
            await self.stop_event.wait()
        finally:
            await self._shutdown()

    async def _shutdown(self):
        """Graceful shutdown."""
        logger.info("Shutting down stream processor service")
        
        try:
            # First, gracefully shut down the uvicorn server
            logger.debug("Shutting down health check server")
            self.server.should_exit = True
            
            # Give the server a moment to stop gracefully
            await asyncio.sleep(0.5)
            
            # Cancel remaining background tasks
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            
            # Wait for tasks to complete with timeout
            if self._tasks:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=10.0
                )
            
        except asyncio.TimeoutError:
            logger.warning("Task cancellation timeout")
        except Exception as e:
            logger.error("Error during shutdown", error=str(e))
        finally:
            # Close NATS connection
            await self.nats_client.close()
            logger.info("Stream processor service shutdown complete")