import asyncio
import json
from typing import Optional, Dict, Any, Callable

from nats.aio.client import Client as NATS
from nats.js import JetStreamContext, api
from nats.js.api import StreamConfig, RetentionPolicy, DiscardPolicy, StorageType, ConsumerConfig, DeliverPolicy, AckPolicy
from nats.errors import TimeoutError
from nats.aio.msg import Msg

from .logging_setup import get_logger
from .metrics import (
    nats_connected, 
    posts_processed_total, 
    posts_published_total, 
    processing_errors_total,
    message_queue_size
)
from .config import settings
from .types import RawPost, EnrichedPost, SentimentData

logger = get_logger(__name__)

class StreamProcessorNatsClient:
    """NATS client for consuming from input stream and publishing to output stream."""
    
    def __init__(self):
        self.url = settings.NATS_URL
        self.input_stream = settings.INPUT_STREAM
        self.output_stream = settings.OUTPUT_STREAM
        self.input_subject = settings.INPUT_SUBJECT
        self.output_subject = settings.OUTPUT_SUBJECT
        self.consumer_name = settings.CONSUMER_NAME
        # Queue group enables multiple pods to share one durable consumer
        self.queue_group = settings.QUEUE_GROUP
        self.max_retries = settings.MAX_RETRIES
        self.stream_num_replicas = settings.NUM_STREAM_REPLICAS
        self.ack_wait_seconds = settings.ACK_WAIT_SECONDS
        self.max_deliver = settings.MAX_DELIVER
        self.max_ack_pending = settings.MAX_ACK_PENDING
        self.duplicate_window = settings.DUPLICATE_WINDOW_SECONDS

        self.nc: Optional[NATS] = None
        self.js: Optional[JetStreamContext] = None
        self._message_handler: Optional[Callable] = None
        self._subscription = None

    async def connect(self):
        """Connect to NATS and set up JetStream."""
        try:
            logger.info("Connecting to NATS", url=self.url)
            self.nc = NATS()
            await self.nc.connect(servers=[self.url])
            nats_connected.set(1)
            self.js = self.nc.jetstream()

            # Ensure input stream exists (should already exist from ingest service)
            await self._ensure_input_stream()
            
            # Ensure output stream exists
            await self._ensure_output_stream()
            
            logger.info("NATS connection established")
            
        except Exception as e:
            logger.error("Failed to connect to NATS", error=str(e))
            nats_connected.set(0)
            processing_errors_total.labels(error_type="nats_connection").inc()
            raise

    async def _ensure_input_stream(self):
        """Ensure the input stream exists."""
        try:
            await self.js.stream_info(self.input_stream)
            logger.info("Input stream exists", stream=self.input_stream)
        except Exception as e:
            logger.warning("Input stream not found", stream=self.input_stream, error=str(e))
            # The input stream should be created by the ingest service
            # We'll just log a warning but not create it ourselves

    async def _ensure_output_stream(self):
        """Ensure the output stream exists, create if not."""
        try:
            await self.js.stream_info(self.output_stream)
            logger.info("Output stream exists", stream=self.output_stream)
        except Exception as e:
            logger.info("Output stream not found, creating", stream=self.output_stream, error=str(e))
            stream_config = StreamConfig(
                name=self.output_stream,
                subjects=[f"{self.output_subject}.>"],
                retention=RetentionPolicy.LIMITS,
                discard=DiscardPolicy.OLD,
                max_msgs_per_subject=-1,
                max_msgs=5_000_000,
                max_bytes=-1,
                max_age=0,
                storage=StorageType.FILE,
                num_replicas=self.stream_num_replicas,
                duplicate_window=self.duplicate_window,  # Enable de-duplication via Nats-Msg-Id
            )
            await self.js.add_stream(config=stream_config)
            logger.info("Output stream created", stream=self.output_stream)

    async def close(self):
        """Close NATS connection."""
        try:
            logger.info("Closing NATS connection")
            nats_connected.set(0)
            
            if self._subscription:
                await self._subscription.unsubscribe()
                self._subscription = None
                
            if self.nc and self.nc.is_connected:
                await self.nc.drain()
                await self.nc.close()
        except Exception as e:
            logger.error("Error closing NATS connection", error=str(e))
        finally:
            self.nc = None
            self.js = None

    async def subscribe_to_posts(self, message_handler: Callable[[Dict[str, Any]], None]):
        """Subscribe to the input stream and process messages.

        Simplified approach: attempt to bind/create durable queue consumer via high-level subscribe.
        If binding fails because consumer doesn't yet exist, explicitly create minimal consumer config
        (without custom deliver_subject) and then subscribe again.
        """
        if not self.js:
            raise RuntimeError("NATS client not connected")

        self._message_handler = message_handler

        subject = f"{self.input_subject}.>"
        logger.info(
            "Setting up subscription",
            stream=self.input_stream,
            consumer=self.consumer_name,
            queue_group=self.queue_group,
            ack_wait=self.ack_wait_seconds,
            max_deliver=self.max_deliver,
            max_ack_pending=self.max_ack_pending,
        )

        try:
            # Try direct bind/create via subscribe
            self._subscription = await self.js.subscribe(
                subject=subject,
                stream=self.input_stream,
                durable=self.consumer_name,
                queue=self.queue_group,
                cb=self._handle_message,
                manual_ack=True,
            )
            logger.info("Bound to durable consumer (existing or implicit)", consumer=self.consumer_name)
            return
        except Exception as first_err:
            logger.info("Initial subscribe failed, attempting explicit consumer creation", error=str(first_err))

        # Explicitly ensure consumer exists with updated ack/max_deliver/max_ack_pending tuning
        consumer_config = ConsumerConfig(
            durable_name=self.consumer_name,
            deliver_policy=DeliverPolicy.ALL,
            ack_policy=AckPolicy.EXPLICIT,
            max_deliver=self.max_deliver,
            ack_wait=self.ack_wait_seconds,
            max_ack_pending=self.max_ack_pending,  # Limit in-flight messages to prevent contention
            filter_subject=subject,
        )
        try:
            await self.js.add_consumer(self.input_stream, consumer_config)
            logger.info("Consumer created", consumer=self.consumer_name)
        except Exception as add_err:
            logger.info("Consumer create race or already exists", error=str(add_err))

        # Bind again
        try:
            self._subscription = await self.js.subscribe(
                subject=subject,
                stream=self.input_stream,
                durable=self.consumer_name,
                queue=self.queue_group,
                cb=self._handle_message,
                manual_ack=True,
            )
            logger.info(
                "Subscription established",
                stream=self.input_stream,
                consumer=self.consumer_name,
                queue_group=self.queue_group,
            )
        except Exception as e:
            logger.error("Failed to set up subscription after consumer ensure", error=str(e))
            processing_errors_total.labels(error_type="subscription_setup").inc()
            raise

    async def _handle_message(self, msg: Msg):
        """Handle incoming messages from the subscription."""
        try:
            # Redelivery visibility
            try:
                md = msg.metadata
                if md and md.num_delivered and md.num_delivered > 1:
                    logger.warning(
                        "redelivered_message",
                        deliveries=md.num_delivered,
                        stream_seq=md.sequence.stream if md.sequence else None,
                        pending=md.num_pending if hasattr(md, 'num_pending') else None,
                    )
            except Exception:
                pass

            # Check if message is empty
            if not msg.data or len(msg.data) == 0:
                logger.warning("Received empty message, skipping")
                await msg.ack()
                return
            
            # Parse the message
            try:
                message_text = msg.data.decode('utf-8').strip()
                if not message_text:
                    logger.warning("Received message with empty content, skipping")
                    await msg.ack()
                    return
                data = json.loads(message_text)
            except json.JSONDecodeError as e:
                logger.error("Failed to parse message JSON", 
                           error=str(e), 
                           message_content=msg.data.decode('utf-8', errors='replace')[:100])
                processing_errors_total.labels(error_type="json_parse").inc()
                await msg.ack()
                return
            
            # Call the message handler (it will increment posts_processed_total)
            if self._message_handler:
                await self._message_handler(data)
            
            # Acknowledge the message
            await msg.ack()
            
        except Exception as e:
            logger.error("Error handling message", error=str(e))
            processing_errors_total.labels(error_type="message_handling").inc()
            # Don't ack on error - let it retry

    async def publish_sentiment_result(self, original_post: RawPost, sentiment_data: SentimentData) -> None:
        """Publish sentiment analysis results to the output stream."""
        if not self.js:
            raise RuntimeError("NATS client not connected")
        
        try:
            # Create enriched message with original post + sentiment
            enriched_post: EnrichedPost = {
                **original_post,
                "sentiment": sentiment_data,
                "processed_at": asyncio.get_event_loop().time(),
                "processor": settings.SERVICE_NAME
            }
            
            payload = json.dumps(enriched_post, default=str).encode('utf-8')
            
            # Determine subject suffix (could be based on sentiment, author, etc.)
            subject_suffix = sentiment_data.get("sentiment", "unknown")
            subject = f"{self.output_subject}.{subject_suffix}"
            
            attempt = 0
            while attempt <= self.max_retries:
                try:
                    # Attempt idempotent publish using headers if supported.
                    # JetStream server will treat messages with same Nats-Msg-Id inside duplicate window as duplicates.
                    headers = None
                    post_uri = original_post.get("uri") if isinstance(original_post, dict) else None
                    post_cid = original_post.get("cid") if isinstance(original_post, dict) else None
                    if post_uri and post_cid:
                        # Use JetStream de-duplication header via enum (api.Header.MSG_ID)
                        headers = {api.Header.MSG_ID: f"{post_uri}:{post_cid}"}

                    ack = await self.js.publish(subject, payload, timeout=5.0, headers=headers)
                    if ack and ack.stream == self.output_stream:
                        # Check if JetStream detected this as a duplicate
                        if ack.duplicate:
                            logger.info("Duplicate detected by JetStream, not stored", 
                                       msg_id=headers.get(api.Header.MSG_ID) if headers else None,
                                       seq=ack.seq)
                        posts_published_total.inc()
                        logger.debug("Published sentiment result", 
                                   subject=subject, 
                                   sentiment=sentiment_data.get("sentiment"),
                                   duplicate=ack.duplicate)
                        return
                        
                except TimeoutError:
                    attempt += 1
                    if attempt > self.max_retries:
                        logger.error("Publish timeout exceeded", 
                                   subject=subject, 
                                   attempts=attempt)
                        processing_errors_total.labels(error_type="publish_timeout").inc()
                        raise
                    
                    await asyncio.sleep(settings.RETRY_DELAY * attempt)
                    logger.warning("Publish timeout, retrying", 
                                 subject=subject, 
                                 attempt=attempt)
                    
        except Exception as e:
            logger.error("Failed to publish sentiment result", error=str(e))
            processing_errors_total.labels(error_type="publish_failed").inc()
            raise

    async def get_pending_message_count(self) -> int:
        """Get the number of pending messages in our consumer."""
        try:
            if not self.js:
                return 0
                
            consumer_info = await self.js.consumer_info(self.input_stream, self.consumer_name)
            pending = consumer_info.num_pending
            message_queue_size.set(pending)
            return pending
            
        except Exception as e:
            logger.warning("Failed to get pending message count", error=str(e))
            return 0