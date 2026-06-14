# backend/models/social_interaction.py

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field
from beanie import Document
from bson import ObjectId


# ─────────────────────────────
# Request / DTO Schemas (FIXED)
# ─────────────────────────────

class LikeToggle(BaseModel):
    item_id: str
    item_type: str


class CommentCreate(BaseModel):
    item_id: str
    item_type: str
    content: str
    parent_comment_id: Optional[str] = None


class CommentEdit(BaseModel):
    content: str


class ShareRecordCreate(BaseModel):
    item_id: str
    item_type: str
    platform: str


class BookmarkToggle(BaseModel):
    item_id: str
    item_type: str


# ─────────────────────────────
# Embedded Models
# ─────────────────────────────

class CommentReply(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    user_id: str
    user_name: str
    user_avatar_url: Optional[str] = None
    content: str
    likes: int = 0
    liked_by: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_edited: bool = False
    edited_at: Optional[datetime] = None


class Comment(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    user_id: str
    user_name: str
    user_avatar_url: Optional[str] = None
    content: str
    likes: int = 0
    liked_by: List[str] = Field(default_factory=list)
    replies: List[CommentReply] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_edited: bool = False
    edited_at: Optional[datetime] = None
    is_deleted: bool = False
    deleted_at: Optional[datetime] = None


class ShareRecord(BaseModel):
    user_id: Optional[str] = None
    platform: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────
# MAIN DOCUMENT
# ─────────────────────────────

class SocialInteraction(Document):
    item_id: str
    item_type: str

    likes: int = 0
    liked_by: List[str] = Field(default_factory=list)

    comments: List[Comment] = Field(default_factory=list)
    comment_count: int = 0

    shares: List[ShareRecord] = Field(default_factory=list)
    share_count: int = 0

    bookmarks: List[str] = Field(default_factory=list)
    bookmark_count: int = 0

    total_engagement_score: int = 0

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "social_interactions"

    # ───────────── helpers ─────────────

    async def _recalc(self):
        active_comments = len([c for c in self.comments if not c.is_deleted])
        active_replies = sum(len(c.replies) for c in self.comments)

        self.total_engagement_score = (
            self.likes +
            active_comments * 3 +
            active_replies * 2 +
            self.share_count * 5
        )
        self.updated_at = datetime.utcnow()

    @classmethod
    async def get_or_create(cls, item_id, item_type):
        obj = await cls.find_one({"item_id": item_id, "item_type": item_type})
        if not obj:
            obj = cls(item_id=item_id, item_type=item_type)
            await obj.insert()
        return obj
