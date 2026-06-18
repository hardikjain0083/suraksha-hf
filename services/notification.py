import logging
import asyncio

logger = logging.getLogger(__name__)

async def send_notification(user_id: str, subject: str, message: str, channel: str = "email"):
    """
    Mock notification service for the hackathon demo.
    In a real scenario, this would integrate with SMTP, SMS gateway, or internal messaging (e.g. Teams/Slack).
    """
    logger.info(f"[{channel.upper()}] Sending to {user_id} | Subject: {subject}")
    logger.debug(f"Message Body: {message}")
    
    # Simulate network delay
    await asyncio.sleep(0.5)
    
    # In a real environment, you'd insert into a notifications table or send to Kafka/RabbitMQ here.
    return {"status": "success", "channel": channel, "delivered_to": user_id}
