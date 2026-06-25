from motor.motor_asyncio import AsyncIOMotorGridFSBucket
from database import db
from bson import ObjectId

async def upload_file_to_gridfs(filename: str, content_type: str, file_bytes: bytes) -> str:
    fs = AsyncIOMotorGridFSBucket(db.client.suraksha_maps, bucket_name="circulars")
    file_id = await fs.upload_from_stream(
        filename,
        file_bytes,
        metadata={"contentType": content_type}
    )
    return str(file_id)

async def download_file_from_gridfs(file_id: str):
    fs = AsyncIOMotorGridFSBucket(db.client.suraksha_maps, bucket_name="circulars")
    grid_out = await fs.open_download_stream(ObjectId(file_id))
    content = await grid_out.read()
    metadata = grid_out.metadata or {}
    return content, grid_out.filename, metadata.get("contentType", "application/octet-stream")

async def delete_file_from_gridfs(file_id: str):
    fs = AsyncIOMotorGridFSBucket(db.client.suraksha_maps, bucket_name="circulars")
    await fs.delete(ObjectId(file_id))
