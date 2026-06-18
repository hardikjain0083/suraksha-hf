import logging
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
import certifi
from config import settings

logger = logging.getLogger(__name__)

class Database:
    client: AsyncIOMotorClient = None
    db = None

db = Database()

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type((ConnectionFailure, ServerSelectionTimeoutError)),
    before_sleep=lambda retry_state: logger.warning(
        f"Database connection failed. Retrying in {retry_state.next_action.sleep}s... "
        f"(Attempt {retry_state.attempt_number}/5)"
    )
)
async def connect_to_mongo():
    logger.info("Connecting to MongoDB Atlas...")
    db.client = AsyncIOMotorClient(
        settings.mongodb_uri, 
        serverSelectionTimeoutMS=5000, 
        tlsCAFile=certifi.where()
    )
    # Ping to verify connection
    await db.client.admin.command('ping')
    db.db = db.client.suraksha_maps
    logger.info("Successfully connected to MongoDB Atlas.")

async def close_mongo_connection():
    if db.client is not None:
        logger.info("Closing MongoDB connection...")
        db.client.close()
        logger.info("MongoDB connection closed.")

async def get_db():
    if db.db is None:
        await connect_to_mongo()
    return db.db
