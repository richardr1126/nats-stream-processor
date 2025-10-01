import asyncio
import signal
import time
from typing import Any, Dict, List
from collections import deque

import uvicorn

from .config import settings
from .logging_setup import get_logger
from .metrics import (
    posts_processed_total,
    processing_duration_seconds,
    message_queue_size,
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
                    posts_processed_total.inc()
                    
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
        
        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(30)  # Log every 30 seconds
                
                # Get pending message count
                pending_count = await self.nats_client.get_pending_message_count()
                
                # Calculate processing rate since last log
                current_processed = posts_processed_total._value.get()
                messages_per_30s = current_processed - last_processed_count
                messages_per_second = messages_per_30s / 30.0
                last_processed_count = current_processed
                
                logger.info("processor stats",
                           pending_messages=pending_count,
                           total_processed=current_processed,
                           messages_per_second=round(messages_per_second, 2))
                
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
            # Cancel background tasks
            for task in self._tasks:
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