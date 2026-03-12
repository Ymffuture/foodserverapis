# services/cloudinary_service.py
import cloudinary
import cloudinary.uploader
import asyncio
from functools import partial
from config import CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET

cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET
)

async def upload_image(file) -> str:
    loop = asyncio.get_event_loop()
    contents = await file.read()
    
    upload_func = partial(
        cloudinary.uploader.upload,
        contents,
        folder="kotabites",
        resource_type="image"
    )
    
    result = await loop.run_in_executor(None, upload_func)
    return result.get("secure_url")
